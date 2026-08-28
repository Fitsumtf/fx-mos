"""HTTP surface for the MOS.

Deliberately thin: every route is a few lines that resolve arguments and call
the engine. The rules live in ``fx_mos.engine``, not here, so a PLC gateway or
an MQTT bridge can drive the same logic without going through HTTP.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__
from ..db import get_session, init_db, session_scope
from ..engine import execution, gating, genealogy, nc, oee, routing
from ..erp import warp
from ..models import (
    Disposition,
    Layout,
    Flow,
    FlowStatus,
    FlowStep,
    Line,
    NCStatus,
    NonConformance,
    Severity,
    Station,
    Unit,
    UnitStatus,
    UnitStepRecord,
)
from ..seed import seed
from ..seed_shop import seed_shop

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(
    title="FX MOS",
    version=__version__,
    description="Manufacturing Operating System — production control, traceability, "
    "interlocking and OEE.",
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    with session_scope() as session:
        seed(session)
        seed_shop(session)


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------


class OrderIn(BaseModel):
    erp_order_id: str
    model_code: str = "FXE1"
    quantity: int = Field(1, ge=1, le=500)
    line_code: str = "GA-1"
    bom: dict = Field(default_factory=dict)


class PartIn(BaseModel):
    part_number: str
    serial_or_lot: str = ""
    quantity: float = 1.0
    supplier: str = ""


class StepIn(BaseModel):
    step_code: str
    operator: str = "operator"
    measurements: dict[str, float] = Field(default_factory=dict)
    parts: list[PartIn] = Field(default_factory=list)
    cycle_seconds: float | None = None


class DispositionIn(BaseModel):
    decision: Disposition
    closed_by: str
    resolution: str


class FlowStepIn(BaseModel):
    station_code: str
    code: str
    name: str
    work_instruction: str = ""
    mandatory: bool = True
    interlock: bool = True
    checks: list[dict] = Field(default_factory=list)
    expected_parts: list[dict] = Field(default_factory=list)
    standard_seconds: float = 30.0


class ReleaseIn(BaseModel):
    released_by: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _line(session: Session, code: str) -> Line:
    line = session.scalars(select(Line).where(Line.code == code)).first()
    if line is None:
        raise HTTPException(404, f"line {code} not found")
    return line


def _unit(session: Session, serial: str) -> Unit:
    unit = session.scalars(select(Unit).where(Unit.serial == serial)).first()
    if unit is None:
        raise HTTPException(404, f"unit {serial} not found")
    return unit


def _station(session: Session, line_id: int, code: str) -> Station:
    station = session.scalars(
        select(Station).where(Station.line_id == line_id, Station.code == code)
    ).first()
    if station is None:
        raise HTTPException(404, f"station {code} not found on this line")
    return station


def _unit_view(session: Session, unit: Unit) -> dict:
    station = session.get(Station, unit.current_station_id) if unit.current_station_id else None
    open_ncs = nc.open_for_unit(session, unit.id)
    return {
        "serial": unit.serial,
        "model": unit.model_code,
        "status": unit.status.value,
        "station": station.code if station else None,
        "station_name": station.name if station else None,
        "zone": station.zone.value if station else None,
        "sequence": station.sequence if station else None,
        "open_ncs": [{"code": n.code, "title": n.title, "severity": n.severity.value}
                     for n in open_ncs],
        "started_at": unit.started_at.isoformat() if unit.started_at else None,
        "completed_at": unit.completed_at.isoformat() if unit.completed_at else None,
    }


# --------------------------------------------------------------------------
# Line and flow
# --------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/api/lines")
def list_lines(session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "code": line.code,
            "name": line.name,
            "takt_seconds": line.takt_seconds,
            "stations": [
                {
                    "code": s.code,
                    "name": s.name,
                    "zone": s.zone.value,
                    "sequence": s.sequence,
                    "ideal_cycle_seconds": s.ideal_cycle_seconds,
                    "capacity": s.capacity,
                }
                for s in line.stations
            ],
        }
        for line in session.scalars(select(Line).order_by(Line.code))
    ]


@app.get("/api/flows")
def list_flows(session: Session = Depends(get_session)) -> list[dict]:
    out = []
    for flow in session.scalars(select(Flow).order_by(Flow.code, Flow.version.desc())):
        out.append(
            {
                "id": flow.id,
                "code": flow.code,
                "name": flow.name,
                "version": flow.version,
                "model_code": flow.model_code,
                "status": flow.status.value,
                "released_at": flow.released_at.isoformat() if flow.released_at else None,
                "released_by": flow.released_by,
                "step_count": len(flow.steps),
            }
        )
    return out


@app.get("/api/flows/{flow_id}")
def get_flow(flow_id: int, session: Session = Depends(get_session)) -> dict:
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(404, "flow not found")
    return {
        "id": flow.id,
        "code": flow.code,
        "version": flow.version,
        "status": flow.status.value,
        "model_code": flow.model_code,
        "notes": flow.notes,
        "validation": routing.validate(session, flow).as_dict(),
        "steps": [
            {
                "id": s.id,
                "code": s.code,
                "name": s.name,
                "station": s.station.code if s.station else None,
                "zone": s.station.zone.value if s.station else None,
                "sequence": s.sequence,
                "mandatory": s.mandatory,
                "interlock": s.interlock,
                "work_instruction": s.work_instruction,
                "checks": s.checks,
                "expected_parts": s.expected_parts,
                "standard_seconds": s.standard_seconds,
            }
            for s in sorted(flow.steps, key=lambda x: x.sequence)
        ],
    }


@app.post("/api/flows/{flow_id}/draft")
def draft_from(flow_id: int, session: Session = Depends(get_session)) -> dict:
    """Clone a released flow into a new editable draft."""
    source = session.get(Flow, flow_id)
    if source is None:
        raise HTTPException(404, "flow not found")
    draft = routing.clone_flow(session, source)
    session.commit()
    return {"id": draft.id, "code": draft.code, "version": draft.version}


@app.post("/api/flows/{flow_id}/steps")
def add_flow_step(
    flow_id: int, body: FlowStepIn, session: Session = Depends(get_session)
) -> dict:
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(404, "flow not found")
    station = _station(session, flow.line_id, body.station_code)
    try:
        step = routing.add_step(
            session,
            flow,
            station=station,
            code=body.code,
            name=body.name,
            work_instruction=body.work_instruction,
            mandatory=body.mandatory,
            interlock=body.interlock,
            checks=body.checks,
            expected_parts=body.expected_parts,
            standard_seconds=body.standard_seconds,
        )
    except routing.FlowError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return {"id": step.id, "code": step.code, "sequence": step.sequence}


@app.post("/api/flows/{flow_id}/release")
def release_flow(
    flow_id: int, body: ReleaseIn, session: Session = Depends(get_session)
) -> dict:
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(404, "flow not found")
    report = routing.validate(session, flow)
    if not report.ok:
        raise HTTPException(422, {"message": "flow did not validate", **report.as_dict()})
    routing.release(session, flow, released_by=body.released_by)
    session.commit()
    return {
        "code": flow.code,
        "version": flow.version,
        "status": flow.status.value,
        "warnings": report.warnings,
    }


# --------------------------------------------------------------------------
# Orders and units
# --------------------------------------------------------------------------


@app.post("/api/orders")
def create_order(body: OrderIn, session: Session = Depends(get_session)) -> dict:
    line = _line(session, body.line_code)
    try:
        result = warp.inject_order(
            session,
            erp_order_id=body.erp_order_id,
            model_code=body.model_code,
            quantity=body.quantity,
            line=line,
            bom=body.bom,
        )
    except warp.ERPError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return result


@app.get("/api/units")
def list_units(
    status: str | None = None,
    limit: int = Query(100, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(Unit).order_by(Unit.id.desc()).limit(limit)
    if status:
        stmt = select(Unit).where(Unit.status == UnitStatus(status)).order_by(Unit.id.desc())
    return [_unit_view(session, u) for u in session.scalars(stmt)]


@app.get("/api/units/{serial}")
def get_unit(serial: str, session: Session = Depends(get_session)) -> dict:
    unit = _unit(session, serial)
    view = _unit_view(session, unit)
    view["next_steps"] = _pending_steps(session, unit)
    return view


def _pending_steps(session: Session, unit: Unit) -> list[dict]:
    """Steps the operator at the current station still has to run."""
    if not unit.current_station_id:
        return []
    done = {
        r.flow_step_id
        for r in session.scalars(
            select(UnitStepRecord).where(UnitStepRecord.unit_id == unit.id)
        )
        if r.status.value == "COMPLETE"
    }
    route = routing.route_for_unit(session, unit)
    return [
        {
            "code": s.code,
            "name": s.name,
            "work_instruction": s.work_instruction,
            "checks": s.checks,
            "expected_parts": s.expected_parts,
            "interlock": s.interlock,
        }
        for s in route
        if s.station_id == unit.current_station_id and s.id not in done
    ]


@app.post("/api/units/{serial}/start")
def start_unit(serial: str, session: Session = Depends(get_session)) -> dict:
    unit = _unit(session, serial)
    try:
        execution.start(session, unit)
    except execution.ExecutionError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return _unit_view(session, unit)


@app.post("/api/units/{serial}/steps")
def run_step(serial: str, body: StepIn, session: Session = Depends(get_session)) -> dict:
    unit = _unit(session, serial)
    step = session.scalars(
        select(FlowStep).where(FlowStep.flow_id == unit.flow_id, FlowStep.code == body.step_code)
    ).first()
    if step is None:
        raise HTTPException(404, f"step {body.step_code} is not in this unit's flow")
    try:
        result = execution.run_step(
            session,
            unit=unit,
            step=step,
            operator=body.operator,
            measurements=body.measurements,
            parts=[p.model_dump() for p in body.parts],
            cycle_seconds=body.cycle_seconds,
        )
    except execution.ExecutionError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return result.as_dict()


@app.get("/api/units/{serial}/gate")
def check_gate(
    serial: str, to: str | None = None, session: Session = Depends(get_session)
) -> dict:
    unit = _unit(session, serial)
    target = (
        _station(session, unit.line_id, to) if to else gating.next_station(session, unit)
    )
    if target is None:
        return {"allowed": True, "unit": serial, "target_station": "SIGN_OFF", "blockers": []}
    return gating.evaluate(session, unit, target).as_dict()


@app.post("/api/units/{serial}/advance")
def advance_unit(
    serial: str, to: str | None = None, session: Session = Depends(get_session)
) -> dict:
    unit = _unit(session, serial)
    target = _station(session, unit.line_id, to) if to else None
    result = execution.advance(session, unit, force_station=target)
    session.commit()
    return result


@app.post("/api/units/{serial}/assign")
def assign_bay(serial: str, bay: str, session: Session = Depends(get_session)) -> dict:
    """Put a vehicle into a bay. Parallel shops only."""
    unit = _unit(session, serial)
    target = _station(session, unit.line_id, bay)
    result = execution.assign_bay(session, unit, target)
    session.commit()
    return result


@app.get("/api/units/{serial}/release-check")
def release_check(serial: str, session: Session = Depends(get_session)) -> dict:
    """May this unit go back to the customer, and if not, why."""
    return gating.release_check(session, _unit(session, serial)).as_dict()


@app.post("/api/units/{serial}/release")
def release_unit(serial: str, session: Session = Depends(get_session)) -> dict:
    unit = _unit(session, serial)
    result = execution.release(session, unit)
    session.commit()
    return result


@app.get("/api/units/{serial}/birth-certificate")
def birth_certificate(serial: str, session: Session = Depends(get_session)) -> dict:
    return genealogy.birth_certificate(session, _unit(session, serial))


# --------------------------------------------------------------------------
# Quality
# --------------------------------------------------------------------------


@app.get("/api/ncs")
def list_ncs(
    open_only: bool = True, session: Session = Depends(get_session)
) -> list[dict]:
    stmt = select(NonConformance).order_by(NonConformance.opened_at.desc())
    if open_only:
        stmt = stmt.where(NonConformance.status != NCStatus.CLOSED)
    out = []
    for item in session.scalars(stmt.limit(200)):
        unit = session.get(Unit, item.unit_id)
        station = session.get(Station, item.station_id) if item.station_id else None
        out.append(
            {
                "code": item.code,
                "unit": unit.serial if unit else None,
                "station": station.code if station else None,
                "zone": station.zone.value if station else None,
                "severity": item.severity.value,
                "blocking": item.blocking,
                "status": item.status.value,
                "title": item.title,
                "detail": item.detail,
                "opened_at": item.opened_at.isoformat(),
                "disposition": item.disposition.value if item.disposition else None,
            }
        )
    return out


@app.post("/api/ncs/{code}/disposition")
def disposition_nc(
    code: str, body: DispositionIn, session: Session = Depends(get_session)
) -> dict:
    item = session.scalars(select(NonConformance).where(NonConformance.code == code)).first()
    if item is None:
        raise HTTPException(404, f"{code} not found")
    try:
        nc.disposition(
            session,
            item,
            decision=body.decision,
            closed_by=body.closed_by,
            resolution=body.resolution,
        )
    except nc.NCError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    unit = session.get(Unit, item.unit_id)
    return {
        "code": item.code,
        "status": item.status.value,
        "disposition": item.disposition.value if item.disposition else None,
        "unit_status": unit.status.value if unit else None,
    }


@app.get("/api/trace/where-used")
def where_used(serial_or_lot: str, session: Session = Depends(get_session)) -> list[dict]:
    return genealogy.where_used(session, serial_or_lot=serial_or_lot)


@app.get("/api/trace/containment")
def containment(
    part_number: str, lots: str, session: Session = Depends(get_session)
) -> dict:
    return genealogy.containment(
        session, part_number=part_number, lots=[x.strip() for x in lots.split(",") if x.strip()]
    )


# --------------------------------------------------------------------------
# Performance and integration
# --------------------------------------------------------------------------


@app.get("/api/oee")
def line_oee(
    line_code: str = "GA-1", hours: float = 8.0, session: Session = Depends(get_session)
) -> dict:
    line = _line(session, line_code)
    until = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return oee.for_line(session, line.id, since=until - dt.timedelta(hours=hours), until=until)


class SimulateIn(BaseModel):
    units: int = Field(8, ge=1, le=100)
    defect_rate: float = Field(0.08, ge=0.0, le=1.0)
    seed: int | None = None


@app.post("/api/simulate")
def simulate(body: SimulateIn, session: Session = Depends(get_session)) -> dict:
    """Push a batch of units through the virtual line. Demo and load-test hook."""
    import random as _random

    from ..simulator import run as run_sim

    result = run_sim(
        session,
        units=body.units,
        defect_rate=body.defect_rate,
        seed_value=body.seed if body.seed is not None else _random.randint(1, 10**6),
    )
    session.commit()
    return result


@app.get("/api/erp/outbox")
def outbox(session: Session = Depends(get_session)) -> dict:
    return warp.outbox_summary(session)


@app.post("/api/erp/drain")
def drain_outbox(session: Session = Depends(get_session)) -> dict:
    result = warp.drain(session)
    session.commit()
    return result


@app.get("/api/dashboard")
def dashboard(line_code: str = "GA-1", session: Session = Depends(get_session)) -> dict:
    """Everything the floor display needs, in one round trip."""
    line = _line(session, line_code)
    units = list(
        session.scalars(
            select(Unit).where(
                Unit.line_id == line.id,
                Unit.status.in_(
                    [UnitStatus.QUEUED, UnitStatus.IN_PROCESS, UnitStatus.HELD, UnitStatus.REWORK]
                ),
            )
        )
    )
    by_station: dict[str, list[dict]] = {}
    for unit in units:
        view = _unit_view(session, unit)
        by_station.setdefault(view["station"] or "QUEUE", []).append(view)

    completed = session.scalar(
        select(func.count()).select_from(Unit).where(Unit.status == UnitStatus.COMPLETE)
    ) or 0
    held = sum(1 for u in units if u.status is UnitStatus.HELD)

    return {
        "line": {
            "code": line.code,
            "name": line.name,
            "takt_seconds": line.takt_seconds,
            "layout": line.layout.value,
        },
        "stations": [
            {
                "code": s.code,
                "name": s.name,
                "zone": s.zone.value,
                "sequence": s.sequence,
                "ideal_cycle_seconds": s.ideal_cycle_seconds,
                "capabilities": s.capabilities or [],
                "units": by_station.get(s.code, []),
            }
            for s in line.stations
        ],
        "queue": by_station.get("QUEUE", []),
        "totals": {
            "in_process": len(units),
            "held": held,
            "completed": completed,
            "open_ncs": session.scalar(
                select(func.count())
                .select_from(NonConformance)
                .where(NonConformance.status != NCStatus.CLOSED)
            ) or 0,
        },
        "oee": oee.for_line(session, line.id),
        "erp": warp.outbox_summary(session)["counts"],
    }


# --------------------------------------------------------------------------
# Floor display
# --------------------------------------------------------------------------

class SimulateShopIn(BaseModel):
    vehicles: int = Field(40, ge=1, le=200)
    fault_rate: float = Field(0.07, ge=0.0, le=1.0)
    leave_in_bays: int = Field(6, ge=0, le=20)
    seed: int | None = None


@app.post("/api/simulate-shop")
def simulate_shop(body: SimulateShopIn, session: Session = Depends(get_session)) -> dict:
    """Run a service day through the bays. Demo hook for the shop board."""
    import random as _random

    from ..simulator_shop import run as run_shop

    result = run_shop(
        session,
        vehicles=body.vehicles,
        defect_rate=body.fault_rate,
        leave_in_bays=body.leave_in_bays,
        seed_value=body.seed if body.seed is not None else _random.randint(1, 10**6),
    )
    session.commit()
    return result


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/shop", include_in_schema=False)
    def shop_board() -> FileResponse:
        return FileResponse(STATIC_DIR / "shop.html")
