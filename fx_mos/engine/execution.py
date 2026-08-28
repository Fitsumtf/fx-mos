"""Execution: what happens when work actually gets done.

Three verbs cover the floor:

    start(unit)          the unit enters the first station
    run_step(...)        an operator or robot completes one step
    advance(unit)        the unit moves to the next station, if the gate allows

Everything else — traceability, interlocking, ERP messages — falls out of these.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..erp import warp
from ..models import (
    FlowStep,
    Layout,
    Line,
    PartConsumption,
    ProcessDatum,
    Severity,
    Station,
    StationEvent,
    StationState,
    StepStatus,
    Unit,
    UnitStatus,
    UnitStepRecord,
    Zone,
    utcnow,
)
from . import gating, nc, routing


class ExecutionError(Exception):
    pass


@dataclass
class StepResult:
    record: UnitStepRecord
    passed: bool
    measurements: list[ProcessDatum] = field(default_factory=list)
    nc_code: str | None = None
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "step_record_id": self.record.id,
            "status": self.record.status.value,
            "passed": self.passed,
            "nc": self.nc_code,
            "messages": self.messages,
            "measurements": [
                {
                    "code": m.check_code,
                    "value": m.value,
                    "uom": m.uom,
                    "in_spec": m.in_spec,
                    "lsl": m.lsl,
                    "usl": m.usl,
                }
                for m in self.measurements
            ],
        }


# --------------------------------------------------------------------------


def start(session: Session, unit: Unit) -> Unit:
    """Put a queued unit onto the first station of its line."""
    if unit.status is not UnitStatus.QUEUED:
        raise ExecutionError(f"{unit.serial} is {unit.status.value}, not queued")

    first = session.scalars(
        select(Station).where(Station.line_id == unit.line_id).order_by(Station.sequence)
    ).first()
    if first is None:
        raise ExecutionError("line has no stations")

    unit.current_station_id = first.id
    unit.status = UnitStatus.IN_PROCESS
    unit.started_at = utcnow()
    session.flush()

    warp.emit(
        session,
        topic="mos.unit.started",
        aggregate=unit.serial,
        payload={
            "serial": unit.serial,
            "model": unit.model_code,
            "station": first.code,
            "started_at": unit.started_at.isoformat(),
        },
    )
    return unit


def _evaluate_check(spec: dict, value: float) -> bool:
    lsl, usl = spec.get("lsl"), spec.get("usl")
    if lsl is not None and value < lsl:
        return False
    if usl is not None and value > usl:
        return False
    return True


def run_step(
    session: Session,
    *,
    unit: Unit,
    step: FlowStep,
    operator: str,
    measurements: dict[str, float] | None = None,
    parts: list[dict] | None = None,
    cycle_seconds: float | None = None,
) -> StepResult:
    """Record one completed step. Returns pass/fail and opens an NC on failure."""
    measurements = measurements or {}
    parts = parts or []
    messages: list[str] = []

    if unit.status in (UnitStatus.COMPLETE, UnitStatus.SCRAPPED):
        raise ExecutionError(f"{unit.serial} is {unit.status.value}")

    if step.station_id is None:
        # Parallel layout: the step runs in whichever bay the unit occupies,
        # provided that bay is equipped for it.
        if unit.current_station_id is None:
            raise ExecutionError(f"{unit.serial} is not assigned to a bay")
        station_id = unit.current_station_id
        if step.required_capability:
            bay = session.get(Station, station_id)
            if step.required_capability not in (bay.capabilities or []):
                raise ExecutionError(
                    f"{bay.code} has no {step.required_capability.replace('_', ' ').lower()}; "
                    f"{step.code} cannot be done here"
                )
    elif unit.current_station_id != step.station_id:
        station = session.get(Station, step.station_id)
        raise ExecutionError(
            f"{unit.serial} is not at {station.code if station else step.station_id}"
        )
    else:
        station_id = step.station_id

    prior = session.scalars(
        select(UnitStepRecord)
        .where(UnitStepRecord.unit_id == unit.id, UnitStepRecord.flow_step_id == step.id)
        .order_by(UnitStepRecord.attempt.desc())
    ).first()
    if prior and prior.status is StepStatus.COMPLETE:
        raise ExecutionError(f"{step.code} is already complete for {unit.serial}")

    record = UnitStepRecord(
        unit_id=unit.id,
        flow_step_id=step.id,
        station_id=station_id,
        attempt=(prior.attempt + 1) if prior else 1,
        operator=operator,
        status=StepStatus.IN_PROGRESS,
        cycle_seconds=cycle_seconds,
    )
    session.add(record)
    session.flush()

    # --- Parts -----------------------------------------------------------
    expected = {p["part_number"]: p for p in (step.expected_parts or [])}
    for part in parts:
        pn = part["part_number"]
        session.add(
            PartConsumption(
                unit_id=unit.id,
                step_record_id=record.id,
                station_id=station_id,
                part_number=pn,
                serial_or_lot=part.get("serial_or_lot", ""),
                quantity=float(part.get("quantity", 1)),
                supplier=part.get("supplier", ""),
            )
        )
        spec = expected.get(pn)
        if spec and spec.get("serialized") and not part.get("serial_or_lot"):
            messages.append(f"{pn} is serialised but was scanned without a serial.")
        warp.emit(
            session,
            topic="mos.part.consumed",
            aggregate=unit.serial,
            payload={
                "serial": unit.serial,
                "part_number": pn,
                "component_serial": part.get("serial_or_lot", ""),
                "quantity": float(part.get("quantity", 1)),
                "station": session.get(Station, station_id).code,
            },
        )

    missing_parts = [
        pn for pn, spec in expected.items()
        if sum(
            p.quantity for p in session.scalars(
                select(PartConsumption).where(
                    PartConsumption.unit_id == unit.id, PartConsumption.part_number == pn
                )
            )
        ) + 1e-9 < float(spec.get("quantity", 1))
    ]

    # --- Measurements ----------------------------------------------------
    recorded: list[ProcessDatum] = []
    out_of_spec: list[ProcessDatum] = []
    missing_checks: list[str] = []

    for spec in step.checks or []:
        code = spec["code"]
        if code not in measurements:
            if step.mandatory:
                missing_checks.append(code)
            continue
        value = float(measurements[code])
        ok = _evaluate_check(spec, value)
        datum = ProcessDatum(
            unit_id=unit.id,
            station_id=station_id,
            step_record_id=record.id,
            check_code=code,
            name=spec.get("name", code),
            value=value,
            uom=spec.get("uom", ""),
            lsl=spec.get("lsl"),
            usl=spec.get("usl"),
            in_spec=ok,
            interlock=bool(spec.get("interlock", step.interlock)),
        )
        session.add(datum)
        recorded.append(datum)
        if not ok:
            out_of_spec.append(datum)

    session.flush()

    # --- Verdict ---------------------------------------------------------
    blocking_failures = [d for d in out_of_spec if d.interlock]
    hard_fail = bool(blocking_failures) or bool(missing_checks) or bool(missing_parts)

    record.completed_at = utcnow()
    if cycle_seconds is None:
        record.cycle_seconds = (record.completed_at - record.started_at).total_seconds()

    if hard_fail:
        record.status = StepStatus.FAILED
        reasons = []
        for d in blocking_failures:
            reasons.append(
                f"{d.name} = {d.value:g}{d.uom} "
                f"(limit {gating._format_window(d.lsl, d.usl)})"
            )
        for c in missing_checks:
            reasons.append(f"{c} was not measured")
        for pn in missing_parts:
            reasons.append(f"{pn} was not scanned")
        record.detail = {"reasons": reasons}
        messages.extend(reasons)

        severity = Severity.CRITICAL if step.interlock else Severity.MAJOR
        created = nc.open_nc(
            session,
            unit=unit,
            title=f"{step.name} failed at {session.get(Station, station_id).code}",
            severity=severity,
            blocking=step.interlock,
            station_id=station_id,
            flow_step_id=step.id,
            detail={"reasons": reasons, "operator": operator},
            opened_by="MOS",
        )
        warp.emit(
            session,
            topic="mos.unit.held",
            aggregate=unit.serial,
            payload={
                "serial": unit.serial,
                "station": session.get(Station, station_id).code,
                "nc": created.code,
                "severity": severity.value,
                "reasons": reasons,
            },
        )
        session.flush()
        return StepResult(record, False, recorded, created.code, messages)

    record.status = StepStatus.COMPLETE
    if unit.status is UnitStatus.REWORK and not gating.open_blocking_ncs(session, unit):
        unit.status = UnitStatus.IN_PROCESS
    session.flush()
    return StepResult(record, True, recorded, None, messages)


# --------------------------------------------------------------------------


def assign_bay(session: Session, unit: Unit, bay: Station) -> dict:
    """Put a unit into a bay in a PARALLEL shop.

    Unlike advancing along a line, this can be undone: a vehicle can be moved
    to another bay mid-job if a lift is needed, and the record follows it.
    """
    decision = gating.evaluate(session, unit, bay)
    if not decision.allowed:
        return {"assigned": False, "gate": decision.as_dict()}

    unit.current_station_id = bay.id
    if unit.status is UnitStatus.QUEUED:
        unit.status = UnitStatus.IN_PROCESS
        unit.started_at = unit.started_at or utcnow()
    session.flush()

    warp.emit(
        session,
        topic="mos.unit.assigned",
        aggregate=unit.serial,
        payload={"serial": unit.serial, "bay": bay.code},
    )
    return {"assigned": True, "bay": bay.code, "gate": decision.as_dict()}


def release(session: Session, unit: Unit) -> dict:
    """Hand the unit back to the customer, if everything checks out.

    This is the gate that carries the liability. If it refuses, the reasons are
    exactly what the service advisor needs to tell the customer why the car is
    not ready.
    """
    decision = gating.release_check(session, unit)
    if not decision.allowed:
        return {"released": False, "gate": decision.as_dict()}

    unit.status = UnitStatus.COMPLETE
    unit.completed_at = utcnow()
    previous = session.get(Station, unit.current_station_id) if unit.current_station_id else None
    unit.current_station_id = None
    session.flush()

    if previous:
        session.add(
            StationEvent(
                station_id=previous.id,
                state=StationState.RUN,
                started_at=unit.started_at or utcnow(),
                ended_at=unit.completed_at,
                duration_seconds=(
                    unit.completed_at - (unit.started_at or unit.completed_at)
                ).total_seconds(),
                reason=f"job complete {unit.serial}",
            )
        )
        session.flush()

    warp.emit(
        session,
        topic="mos.unit.completed",
        aggregate=unit.serial,
        payload=build_handshake(session, unit),
    )
    return {"released": True, "gate": decision.as_dict()}


def advance(session: Session, unit: Unit, *, force_station: Station | None = None) -> dict:
    """Try to move the unit downstream. Returns the gate decision either way."""
    target = force_station or gating.next_station(session, unit)

    if target is None:
        return _sign_off(session, unit)

    decision = gating.evaluate(session, unit, target)
    if not decision.allowed:
        return {"moved": False, "gate": decision.as_dict()}

    previous = session.get(Station, unit.current_station_id) if unit.current_station_id else None
    unit.current_station_id = target.id
    if unit.status is UnitStatus.QUEUED:
        unit.status = UnitStatus.IN_PROCESS
    session.flush()

    if previous:
        session.add(
            StationEvent(
                station_id=previous.id,
                state=StationState.RUN,
                started_at=utcnow(),
                ended_at=utcnow(),
                duration_seconds=previous.ideal_cycle_seconds,
                reason=f"cycle complete {unit.serial}",
            )
        )
        session.flush()

    return {"moved": True, "station": target.code, "gate": decision.as_dict()}


def _sign_off(session: Session, unit: Unit) -> dict:
    """End of line. Every mandatory step must be complete and no NC may be open."""
    route = routing.route_for_unit(session, unit)
    records = {
        r.flow_step_id: r
        for r in session.scalars(
            select(UnitStepRecord).where(UnitStepRecord.unit_id == unit.id)
        )
    }
    outstanding = [
        s.code
        for s in route
        if s.mandatory
        and (s.id not in records or records[s.id].status is not StepStatus.COMPLETE)
    ]
    open_ncs = [n.code for n in gating.open_blocking_ncs(session, unit)]

    if outstanding or open_ncs:
        return {
            "moved": False,
            "gate": {
                "allowed": False,
                "unit": unit.serial,
                "target_station": "SIGN_OFF",
                "blockers": (
                    [
                        {"code": "STEP_INCOMPLETE", "message": f"{c} not complete", "reference": c}
                        for c in outstanding
                    ]
                    + [
                        {"code": "NC_OPEN", "message": f"{c} still open", "reference": c}
                        for c in open_ncs
                    ]
                ),
            },
        }

    unit.status = UnitStatus.COMPLETE
    unit.completed_at = utcnow()
    session.flush()

    warp.emit(
        session,
        topic="mos.unit.completed",
        aggregate=unit.serial,
        payload=build_handshake(session, unit),
    )
    return {"moved": True, "station": "SIGN_OFF", "signed_off": True}


def build_handshake(session: Session, unit: Unit) -> dict:
    """The as-built profile pushed back to the ERP when a unit is signed off."""
    from .genealogy import birth_certificate

    cert = birth_certificate(session, unit)
    return {
        "serial": unit.serial,
        "model": unit.model_code,
        "completed_at": unit.completed_at.isoformat() if unit.completed_at else None,
        "components": [
            {"part_number": c["part_number"], "serial": c["serial_or_lot"]}
            for c in cert["components"]
        ],
        "critical_measurements": [
            m for m in cert["measurements"] if m["interlock"]
        ],
        "flow": cert["flow"],
    }


def zone_of(session: Session, unit: Unit) -> Zone | None:
    if not unit.current_station_id:
        return None
    station = session.get(Station, unit.current_station_id)
    return station.zone if station else None
