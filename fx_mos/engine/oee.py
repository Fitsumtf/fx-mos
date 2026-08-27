"""Overall Equipment Effectiveness.

OEE = Availability x Performance x Quality.

The number itself is nearly useless without the loss breakdown underneath it,
so this module always returns where the time went. A station at 62% because it
is starved needs a different fix than one at 62% because it is jamming.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    NonConformance,
    Station,
    StationEvent,
    StationState,
    StepStatus,
    UnitStepRecord,
    utcnow,
)

# Time in these states is not held against the station.
EXCLUDED_FROM_PLANNED = {StationState.PLANNED_STOP}

# Time in these states counts as downtime against availability.
DOWNTIME_STATES = {
    StationState.DOWN,
    StationState.IDLE,
    StationState.BLOCKED,
    StationState.STARVED,
    StationState.CHANGEOVER,
}


@dataclass
class OEEResult:
    station_code: str
    station_name: str
    zone: str
    window_start: dt.datetime
    window_end: dt.datetime
    planned_seconds: float
    run_seconds: float
    availability: float
    performance: float
    quality: float
    oee: float
    total_count: int
    good_count: int
    reject_count: int
    ideal_cycle_seconds: float
    losses: dict = field(default_factory=dict)
    top_loss: str | None = None

    def as_dict(self) -> dict:
        return {
            "station": self.station_code,
            "name": self.station_name,
            "zone": self.zone,
            "window": {
                "from": self.window_start.isoformat(),
                "to": self.window_end.isoformat(),
            },
            "planned_seconds": round(self.planned_seconds, 1),
            "run_seconds": round(self.run_seconds, 1),
            "availability": round(self.availability, 4),
            "performance": round(self.performance, 4),
            "quality": round(self.quality, 4),
            "oee": round(self.oee, 4),
            "counts": {
                "total": self.total_count,
                "good": self.good_count,
                "reject": self.reject_count,
            },
            "ideal_cycle_seconds": self.ideal_cycle_seconds,
            "losses": {k: round(v, 1) for k, v in self.losses.items()},
            "top_loss": self.top_loss,
        }


def _duration(event: StationEvent, start: dt.datetime, end: dt.datetime) -> float:
    """Seconds of this event that fall inside the window."""
    ev_start = max(event.started_at, start)
    ev_end = min(event.ended_at or end, end)
    return max((ev_end - ev_start).total_seconds(), 0.0)


def for_station(
    session: Session,
    station: Station,
    *,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> OEEResult:
    until = until or utcnow()
    since = since or (until - dt.timedelta(hours=8))

    events = session.scalars(
        select(StationEvent).where(
            StationEvent.station_id == station.id,
            StationEvent.started_at < until,
        )
    ).all()
    events = [e for e in events if (e.ended_at or until) > since]

    buckets: dict[str, float] = {}
    for event in events:
        seconds = _duration(event, since, until)
        if seconds <= 0:
            continue
        buckets[event.state.value] = buckets.get(event.state.value, 0.0) + seconds

    excluded = sum(buckets.get(s.value, 0.0) for s in EXCLUDED_FROM_PLANNED)
    planned = sum(buckets.values()) - excluded
    run = buckets.get(StationState.RUN.value, 0.0)

    # Counts: completed step attempts at this station.
    records = session.scalars(
        select(UnitStepRecord).where(
            UnitStepRecord.station_id == station.id,
            UnitStepRecord.completed_at.is_not(None),
            UnitStepRecord.completed_at >= since,
            UnitStepRecord.completed_at <= until,
        )
    ).all()

    units_seen = {r.unit_id for r in records}
    failed_units = {r.unit_id for r in records if r.status is StepStatus.FAILED}

    # A unit that was reworked to good still counts as a first-pass reject.
    ncs = session.scalars(
        select(NonConformance).where(
            NonConformance.station_id == station.id,
            NonConformance.opened_at >= since,
            NonConformance.opened_at <= until,
        )
    ).all()
    failed_units |= {n.unit_id for n in ncs}

    total = len(units_seen)
    reject = len(failed_units & units_seen)
    good = total - reject

    ideal = station.ideal_cycle_seconds or 1.0

    availability = (run / planned) if planned > 0 else 0.0
    performance = ((ideal * total) / run) if run > 0 else 0.0
    performance = min(performance, 1.0)  # a cycle faster than ideal is a bad ideal
    quality = (good / total) if total > 0 else 0.0

    losses = {
        state: buckets.get(state, 0.0)
        for state in (s.value for s in DOWNTIME_STATES)
        if buckets.get(state, 0.0) > 0
    }
    # Speed loss only means something once there is throughput to compare
    # against. A station that ran and produced nothing has an availability or
    # quality problem; calling it "slow" would send the fix in the wrong direction.
    speed_loss = max(run - ideal * total, 0.0) if total > 0 else 0.0
    if speed_loss > 0:
        losses["SPEED"] = speed_loss
    if reject > 0:
        losses["QUALITY"] = reject * ideal

    top_loss = max(losses, key=losses.get) if losses else None

    return OEEResult(
        station_code=station.code,
        station_name=station.name,
        zone=station.zone.value,
        window_start=since,
        window_end=until,
        planned_seconds=planned,
        run_seconds=run,
        availability=availability,
        performance=performance,
        quality=quality,
        oee=availability * performance * quality,
        total_count=total,
        good_count=good,
        reject_count=reject,
        ideal_cycle_seconds=ideal,
        losses=losses,
        top_loss=top_loss,
    )


def for_line(
    session: Session,
    line_id: int,
    *,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> dict:
    stations = list(
        session.scalars(
            select(Station).where(Station.line_id == line_id).order_by(Station.sequence)
        )
    )
    results = [for_station(session, s, since=since, until=until) for s in stations]
    measured = [r for r in results if r.planned_seconds > 0]

    # A line runs at the pace of its worst station, not its average.
    bottleneck = min(measured, key=lambda r: r.oee) if measured else None

    return {
        "line_id": line_id,
        "line_oee": round(bottleneck.oee, 4) if bottleneck else 0.0,
        "bottleneck": bottleneck.station_code if bottleneck else None,
        "bottleneck_reason": bottleneck.top_loss if bottleneck else None,
        "average_oee": (
            round(sum(r.oee for r in measured) / len(measured), 4) if measured else 0.0
        ),
        "stations": [r.as_dict() for r in results],
    }
