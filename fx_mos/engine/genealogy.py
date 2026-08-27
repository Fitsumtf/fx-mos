"""Traceability, in both directions.

Forward: given a unit, what went into it and what was measured.
Reverse: given a suspect lot, which units contain it — the query you run at
2 a.m. when a supplier calls, and the reason the containment is 40 cars instead
of 40,000.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Flow,
    NonConformance,
    PartConsumption,
    ProcessDatum,
    Station,
    Unit,
    UnitStepRecord,
)


def birth_certificate(session: Session, unit: Unit) -> dict:
    """The complete, auditable record of one unit."""
    flow = session.get(Flow, unit.flow_id)
    stations = {s.id: s for s in session.scalars(select(Station))}

    parts = session.scalars(
        select(PartConsumption)
        .where(PartConsumption.unit_id == unit.id)
        .order_by(PartConsumption.consumed_at)
    ).all()

    data = session.scalars(
        select(ProcessDatum)
        .where(ProcessDatum.unit_id == unit.id)
        .order_by(ProcessDatum.recorded_at)
    ).all()

    records = session.scalars(
        select(UnitStepRecord)
        .where(UnitStepRecord.unit_id == unit.id)
        .order_by(UnitStepRecord.started_at)
    ).all()

    ncs = session.scalars(
        select(NonConformance)
        .where(NonConformance.unit_id == unit.id)
        .order_by(NonConformance.opened_at)
    ).all()

    return {
        "serial": unit.serial,
        "model": unit.model_code,
        "status": unit.status.value,
        "flow": {
            "code": flow.code if flow else None,
            "version": flow.version if flow else None,
            "released_at": flow.released_at.isoformat() if flow and flow.released_at else None,
        },
        "started_at": unit.started_at.isoformat() if unit.started_at else None,
        "completed_at": unit.completed_at.isoformat() if unit.completed_at else None,
        "current_station": (
            stations[unit.current_station_id].code
            if unit.current_station_id in stations
            else None
        ),
        "components": [
            {
                "part_number": p.part_number,
                "serial_or_lot": p.serial_or_lot,
                "quantity": p.quantity,
                "supplier": p.supplier,
                "station": stations[p.station_id].code if p.station_id in stations else "",
                "consumed_at": p.consumed_at.isoformat(),
            }
            for p in parts
        ],
        "measurements": [
            {
                "check": d.check_code,
                "name": d.name,
                "value": d.value,
                "uom": d.uom,
                "lsl": d.lsl,
                "usl": d.usl,
                "in_spec": d.in_spec,
                "interlock": d.interlock,
                "station": stations[d.station_id].code if d.station_id in stations else "",
                "recorded_at": d.recorded_at.isoformat(),
            }
            for d in data
        ],
        "steps": [
            {
                "step": r.flow_step.code if r.flow_step else None,
                "name": r.flow_step.name if r.flow_step else None,
                "station": stations[r.station_id].code if r.station_id in stations else "",
                "status": r.status.value,
                "attempt": r.attempt,
                "operator": r.operator,
                "cycle_seconds": r.cycle_seconds,
                "started_at": r.started_at.isoformat(),
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "detail": r.detail or {},
            }
            for r in records
        ],
        "non_conformances": [
            {
                "code": n.code,
                "title": n.title,
                "severity": n.severity.value,
                "status": n.status.value,
                "disposition": n.disposition.value if n.disposition else None,
                "opened_at": n.opened_at.isoformat(),
                "closed_at": n.closed_at.isoformat() if n.closed_at else None,
                "resolution": n.resolution,
            }
            for n in ncs
        ],
    }


def where_used(session: Session, *, serial_or_lot: str) -> list[dict]:
    """Every unit that consumed a given component serial or lot."""
    rows = session.scalars(
        select(PartConsumption)
        .where(PartConsumption.serial_or_lot == serial_or_lot)
        .order_by(PartConsumption.consumed_at)
    ).all()

    out: list[dict] = []
    for row in rows:
        unit = session.get(Unit, row.unit_id)
        if unit is None:
            continue
        out.append(
            {
                "serial": unit.serial,
                "model": unit.model_code,
                "status": unit.status.value,
                "part_number": row.part_number,
                "consumed_at": row.consumed_at.isoformat(),
            }
        )
    return out


def containment(session: Session, *, part_number: str, lots: list[str]) -> dict:
    """Scope a recall: which units are affected and how far along they are."""
    rows = session.scalars(
        select(PartConsumption).where(
            PartConsumption.part_number == part_number,
            PartConsumption.serial_or_lot.in_(lots),
        )
    ).all()

    affected: dict[int, dict] = {}
    for row in rows:
        unit = session.get(Unit, row.unit_id)
        if unit is None:
            continue
        affected[unit.id] = {
            "serial": unit.serial,
            "status": unit.status.value,
            "lot": row.serial_or_lot,
            "shipped": unit.status.value == "COMPLETE",
        }

    units = list(affected.values())
    return {
        "part_number": part_number,
        "lots": lots,
        "affected_count": len(units),
        "still_in_plant": sum(1 for u in units if not u["shipped"]),
        "already_signed_off": sum(1 for u in units if u["shipped"]),
        "units": units,
    }
