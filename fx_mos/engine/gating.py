"""Process interlocking.

One question, asked constantly: may this unit move to that station right now?

The answer is always a list of reasons, never a bare boolean, because the person
standing at the conveyor needs to know what to fix. Every reason carries a code
so the andon board and the ERP can act on it without parsing English.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Layout,
    Line,
    NCStatus,
    NonConformance,
    ProcessDatum,
    PartConsumption,
    Station,
    StepStatus,
    Unit,
    UnitStatus,
    UnitStepRecord,
)
from . import routing


@dataclass
class Blocker:
    code: str
    message: str
    reference: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "reference": self.reference}


@dataclass
class GateDecision:
    allowed: bool
    unit_serial: str
    target_station: str
    blockers: list[Blocker] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "unit": self.unit_serial,
            "target_station": self.target_station,
            "blockers": [b.as_dict() for b in self.blockers],
        }


# --------------------------------------------------------------------------


def _records_by_step(session: Session, unit: Unit) -> dict[int, UnitStepRecord]:
    """Latest attempt per step."""
    rows = session.scalars(
        select(UnitStepRecord)
        .where(UnitStepRecord.unit_id == unit.id)
        .order_by(UnitStepRecord.attempt)
    ).all()
    return {r.flow_step_id: r for r in rows}


def open_blocking_ncs(session: Session, unit: Unit) -> list[NonConformance]:
    return list(
        session.scalars(
            select(NonConformance).where(
                NonConformance.unit_id == unit.id,
                NonConformance.blocking.is_(True),
                NonConformance.status != NCStatus.CLOSED,
            )
        )
    )


def evaluate(session: Session, unit: Unit, target_station: Station) -> GateDecision:
    """Decide whether ``unit`` may enter ``target_station``.

    On a SEQUENTIAL line this is the advance gate: is the upstream work done.
    On a PARALLEL shop it is the assignment gate: is that bay free and capable.
    """
    line = session.get(Line, unit.line_id)
    if line is not None and line.layout is Layout.PARALLEL:
        return _evaluate_assignment(session, unit, target_station)
    return _evaluate_advance(session, unit, target_station)


def _evaluate_assignment(
    session: Session, unit: Unit, bay: Station
) -> GateDecision:
    """Can this unit be put in this bay right now."""
    blockers: list[Blocker] = []

    if unit.status is UnitStatus.SCRAPPED:
        blockers.append(Blocker("UNIT_SCRAPPED", "Unit is written off."))
    if unit.status is UnitStatus.COMPLETE:
        blockers.append(Blocker("UNIT_COMPLETE", "Unit is already released."))
    if bay.line_id != unit.line_id:
        blockers.append(Blocker("WRONG_SHOP", f"{bay.code} is not in this shop.", bay.code))
        return GateDecision(False, unit.serial, bay.code, blockers)

    occupancy = session.scalars(
        select(Unit).where(
            Unit.current_station_id == bay.id,
            Unit.status.in_([UnitStatus.IN_PROCESS, UnitStatus.HELD, UnitStatus.REWORK]),
            Unit.id != unit.id,
        )
    ).all()
    if len(occupancy) >= bay.capacity:
        blockers.append(
            Blocker(
                "BAY_OCCUPIED",
                f"{bay.code} is occupied.",
                ", ".join(u.serial for u in occupancy),
            )
        )

    # Every step this unit needs must be doable in this bay.
    have = set(bay.capabilities or [])
    missing = sorted(
        {
            step.required_capability
            for step in routing.route_for_unit(session, unit)
            if step.required_capability and step.required_capability not in have
        }
    )
    for capability in missing:
        blockers.append(
            Blocker(
                "BAY_NOT_CAPABLE",
                f"{bay.code} has no {capability.replace('_', ' ').lower()}.",
                capability,
            )
        )

    return GateDecision(not blockers, unit.serial, bay.code, blockers)


def release_check(session: Session, unit: Unit) -> GateDecision:
    """May this unit go back to the customer.

    The gate that carries the liability. Everything mandatory must be done,
    every declared part scanned, every interlocking measurement inside its
    window, and no quality hold left open.
    """
    blockers: list[Blocker] = []
    route = routing.route_for_unit(session, unit)
    records = _records_by_step(session, unit)

    for step in route:
        record = records.get(step.id)
        if step.mandatory and (record is None or record.status is not StepStatus.COMPLETE):
            state = record.status.value.lower() if record else "not started"
            blockers.append(
                Blocker("STEP_INCOMPLETE", f"{step.name} is {state}.", step.code)
            )
            continue
        for expected in step.expected_parts or []:
            got = sum(
                p.quantity
                for p in session.scalars(
                    select(PartConsumption).where(
                        PartConsumption.unit_id == unit.id,
                        PartConsumption.part_number == expected["part_number"],
                    )
                )
            )
            needed = float(expected.get("quantity", 1))
            if got + 1e-9 < needed:
                blockers.append(
                    Blocker(
                        "PART_MISSING",
                        f"{expected['part_number']}: {needed:g} required, {got:g} recorded.",
                        step.code,
                    )
                )

    for datum in _latest_interlock_data(session, unit).values():
        if not datum.in_spec:
            blockers.append(
                Blocker(
                    "OUT_OF_SPEC",
                    f"{datum.name or datum.check_code} recorded {datum.value:g}{datum.uom} "
                    f"against {_format_window(datum.lsl, datum.usl)}.",
                    datum.check_code,
                )
            )

    for item in open_blocking_ncs(session, unit):
        blockers.append(Blocker("NC_OPEN", f"{item.code}: {item.title}", item.code))

    return GateDecision(not blockers, unit.serial, "RELEASE", blockers)


def _latest_interlock_data(session: Session, unit: Unit) -> dict:
    """Most recent interlocking value per station and check code."""
    history = session.scalars(
        select(ProcessDatum)
        .where(ProcessDatum.unit_id == unit.id, ProcessDatum.interlock.is_(True))
        .order_by(ProcessDatum.recorded_at, ProcessDatum.id)
    ).all()
    latest: dict[tuple[int, str], ProcessDatum] = {}
    for datum in history:
        latest[(datum.station_id, datum.check_code)] = datum
    return latest


def _evaluate_advance(session: Session, unit: Unit, target_station: Station) -> GateDecision:
    blockers: list[Blocker] = []

    if unit.status is UnitStatus.SCRAPPED:
        blockers.append(Blocker("UNIT_SCRAPPED", "Unit is scrapped and cannot be moved."))
    if unit.status is UnitStatus.COMPLETE:
        blockers.append(Blocker("UNIT_COMPLETE", "Unit is already signed off."))

    if target_station.line_id != unit.line_id:
        blockers.append(
            Blocker(
                "WRONG_LINE",
                f"{target_station.code} is not on this unit's line.",
                target_station.code,
            )
        )
        return GateDecision(False, unit.serial, target_station.code, blockers)

    # --- Sequence: no skipping ahead -------------------------------------
    stations = list(
        session.scalars(
            select(Station)
            .where(Station.line_id == unit.line_id)
            .order_by(Station.sequence)
        )
    )
    current_seq = -1
    if unit.current_station_id:
        current = session.get(Station, unit.current_station_id)
        current_seq = current.sequence if current else -1

    upstream = [s for s in stations if s.sequence < target_station.sequence]

    if target_station.sequence < current_seq:
        blockers.append(
            Blocker(
                "BACKWARD_MOVE",
                "Moving upstream requires a rework order, not a normal advance.",
                target_station.code,
            )
        )

    # --- Capacity --------------------------------------------------------
    occupancy = session.scalars(
        select(Unit).where(
            Unit.current_station_id == target_station.id,
            Unit.status.in_([UnitStatus.IN_PROCESS, UnitStatus.HELD, UnitStatus.REWORK]),
            Unit.id != unit.id,
        )
    ).all()
    if len(occupancy) >= target_station.capacity:
        blockers.append(
            Blocker(
                "STATION_FULL",
                f"{target_station.code} is at capacity ({target_station.capacity}).",
                ", ".join(u.serial for u in occupancy),
            )
        )

    # --- Upstream work must be finished ----------------------------------
    route = routing.route_for_unit(session, unit)
    records = _records_by_step(session, unit)
    upstream_ids = {s.id for s in upstream}

    for step in route:
        if step.station_id not in upstream_ids:
            continue
        record = records.get(step.id)
        if step.mandatory and (record is None or record.status is not StepStatus.COMPLETE):
            state = record.status.value if record else "not started"
            blockers.append(
                Blocker(
                    "STEP_INCOMPLETE",
                    f"{step.name} at {step.station.code} is {state.lower()}.",
                    step.code,
                )
            )

        # Parts declared for the step must actually have been scanned.
        if record is not None and record.status is StepStatus.COMPLETE:
            for expected in step.expected_parts or []:
                scanned = session.scalars(
                    select(PartConsumption).where(
                        PartConsumption.unit_id == unit.id,
                        PartConsumption.part_number == expected["part_number"],
                    )
                ).all()
                needed = float(expected.get("quantity", 1))
                got = sum(p.quantity for p in scanned)
                if got + 1e-9 < needed:
                    blockers.append(
                        Blocker(
                            "PART_MISSING",
                            f"{expected['part_number']} needs {needed:g}, "
                            f"{got:g} scanned at {step.station.code}.",
                            step.code,
                        )
                    )

    # --- Interlocking measurements ---------------------------------------
    # Only the most recent value for each check counts. A failed reading that
    # was reworked to spec stays in the birth certificate forever, but it must
    # not hold the unit a second time — otherwise rework is a one-way door and
    # the line plugs behind it.
    for datum in _latest_interlock_data(session, unit).values():
        if datum.in_spec:
            continue
        if datum.station_id in upstream_ids or datum.station_id == unit.current_station_id:
            window = _format_window(datum.lsl, datum.usl)
            blockers.append(
                Blocker(
                    "OUT_OF_SPEC",
                    f"{datum.name or datum.check_code} measured "
                    f"{datum.value:g}{datum.uom} against {window}.",
                    datum.check_code,
                )
            )

    # --- Open quality holds ----------------------------------------------
    for nc in open_blocking_ncs(session, unit):
        blockers.append(
            Blocker("NC_OPEN", f"{nc.code}: {nc.title}", nc.code)
        )

    # Deduplicate while preserving order.
    seen: set[tuple[str, str]] = set()
    unique: list[Blocker] = []
    for blocker in blockers:
        key = (blocker.code, blocker.reference or blocker.message)
        if key not in seen:
            seen.add(key)
            unique.append(blocker)

    return GateDecision(not unique, unit.serial, target_station.code, unique)


def _format_window(lsl: float | None, usl: float | None) -> str:
    if lsl is not None and usl is not None:
        return f"{lsl:g}–{usl:g}"
    if lsl is not None:
        return f"min {lsl:g}"
    if usl is not None:
        return f"max {usl:g}"
    return "no limit"


def next_station(session: Session, unit: Unit) -> Station | None:
    """The station this unit should go to next if nothing blocks it.

    Returns None in a PARALLEL shop: there is no "next" bay. A unit stays where
    it was assigned until it is released.
    """
    line = session.get(Line, unit.line_id)
    if line is not None and line.layout is Layout.PARALLEL:
        return None
    stations = list(
        session.scalars(
            select(Station)
            .where(Station.line_id == unit.line_id)
            .order_by(Station.sequence)
        )
    )
    if not stations:
        return None
    if unit.current_station_id is None:
        return stations[0]
    current = session.get(Station, unit.current_station_id)
    later = [s for s in stations if s.sequence > (current.sequence if current else -1)]
    return later[0] if later else None
