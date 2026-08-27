"""Non-conformances.

An NC is what the MOS writes when reality disagrees with the flow. A blocking NC
holds the unit where it stands; closing one requires a disposition, so nothing
leaves the line on a shrug.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Disposition,
    NCStatus,
    NonConformance,
    Severity,
    StepStatus,
    Unit,
    UnitStatus,
    UnitStepRecord,
    utcnow,
)


class NCError(Exception):
    pass


def _next_code(session: Session) -> str:
    count = session.scalar(select(func.count()).select_from(NonConformance)) or 0
    return f"NC-{count + 1:06d}"


def open_nc(
    session: Session,
    *,
    unit: Unit,
    title: str,
    severity: Severity = Severity.MAJOR,
    blocking: bool = True,
    station_id: int | None = None,
    flow_step_id: int | None = None,
    detail: dict | None = None,
    opened_by: str = "MOS",
) -> NonConformance:
    nc = NonConformance(
        code=_next_code(session),
        unit_id=unit.id,
        station_id=station_id or unit.current_station_id,
        flow_step_id=flow_step_id,
        severity=severity,
        blocking=blocking,
        title=title,
        detail=detail or {},
        opened_by=opened_by,
    )
    session.add(nc)
    if blocking and unit.status not in (UnitStatus.SCRAPPED, UnitStatus.COMPLETE):
        unit.status = UnitStatus.HELD
    session.flush()
    return nc


def disposition(
    session: Session,
    nc: NonConformance,
    *,
    decision: Disposition,
    closed_by: str,
    resolution: str = "",
) -> NonConformance:
    """Close an NC with a decision. This is the only way a held unit is released."""
    if nc.status is NCStatus.CLOSED:
        raise NCError(f"{nc.code} is already closed")
    if not resolution.strip():
        raise NCError("a disposition needs a written resolution")

    nc.status = NCStatus.CLOSED
    nc.disposition = decision
    nc.closed_by = closed_by
    nc.closed_at = utcnow()
    nc.resolution = resolution.strip()
    session.flush()

    unit = session.get(Unit, nc.unit_id)

    if decision is Disposition.SCRAP:
        unit.status = UnitStatus.SCRAPPED
        unit.completed_at = utcnow()
        session.flush()
        return nc

    if decision is Disposition.REWORK:
        # Reopen the failed step so the operator can run it again. The failed
        # measurements stay on the record; the new attempt is a new row.
        if nc.flow_step_id:
            record = session.scalars(
                select(UnitStepRecord)
                .where(
                    UnitStepRecord.unit_id == unit.id,
                    UnitStepRecord.flow_step_id == nc.flow_step_id,
                )
                .order_by(UnitStepRecord.attempt.desc())
            ).first()
            if record and record.status is StepStatus.FAILED:
                record.detail = {**(record.detail or {}), "rework_of": nc.code}
        unit.status = UnitStatus.REWORK
    else:
        # USE_AS_IS and DEVIATION both let the unit continue as built.
        unit.status = UnitStatus.IN_PROCESS

    still_held = session.scalars(
        select(NonConformance).where(
            NonConformance.unit_id == unit.id,
            NonConformance.blocking.is_(True),
            NonConformance.status != NCStatus.CLOSED,
        )
    ).first()
    if still_held:
        unit.status = UnitStatus.HELD

    session.flush()
    return nc


def open_for_unit(session: Session, unit_id: int) -> list[NonConformance]:
    return list(
        session.scalars(
            select(NonConformance)
            .where(NonConformance.unit_id == unit_id, NonConformance.status != NCStatus.CLOSED)
            .order_by(NonConformance.opened_at)
        )
    )
