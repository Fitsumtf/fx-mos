"""The ERP bridge.

Named after the pattern rather than any one vendor: an order system upstream,
a factory downstream, and a loop between them that has to survive the network
being down.

Two rules hold the design together:

1. MOS never makes a blocking HTTP call while a unit is on a fixture. Events go
   into an outbox table inside the same transaction as the floor data, and a
   worker drains it. If the ERP is unreachable the line keeps running.
2. Inbound orders are idempotent. The same order arriving twice produces one
   work order, because retries are normal and duplicate VINs are not.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Flow,
    Line,
    OutboxEvent,
    OutboxStatus,
    Unit,
    UnitStatus,
    WorkOrder,
    utcnow,
)
from ..vin import allocate

ERP_ENDPOINT = os.getenv("FX_MOS_ERP_ENDPOINT", "")
MAX_ATTEMPTS = int(os.getenv("FX_MOS_ERP_MAX_ATTEMPTS", "5"))


class ERPError(Exception):
    pass


# --------------------------------------------------------------------------
# Outbound
# --------------------------------------------------------------------------


def emit(session: Session, *, topic: str, aggregate: str, payload: dict) -> OutboxEvent:
    """Queue an event for the ERP. Commits with the caller's transaction."""
    event = OutboxEvent(topic=topic, aggregate=aggregate, payload=payload)
    session.add(event)
    session.flush()
    return event


def pending(session: Session, limit: int = 100) -> list[OutboxEvent]:
    return list(
        session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.status == OutboxStatus.PENDING,
                OutboxEvent.attempts < MAX_ATTEMPTS,
            )
            .order_by(OutboxEvent.id)
            .limit(limit)
        )
    )


def _post(url: str, body: dict, timeout: float = 5.0) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status >= 300:
            raise ERPError(f"ERP returned {response.status}")


def drain(session: Session, *, endpoint: str | None = None, limit: int = 100) -> dict:
    """Publish queued events. Safe to call on a timer; failures stay queued."""
    endpoint = endpoint if endpoint is not None else ERP_ENDPOINT
    events = pending(session, limit)
    published, failed = 0, 0

    for event in events:
        event.attempts += 1
        body = {
            "topic": event.topic,
            "aggregate": event.aggregate,
            "payload": event.payload,
            "emitted_at": event.created_at.isoformat(),
        }
        try:
            if endpoint:
                _post(endpoint, body)
            # With no endpoint configured the bridge runs in loopback mode: the
            # event is marked published so the demo shows a complete loop.
            event.status = OutboxStatus.PUBLISHED
            event.published_at = utcnow()
            event.last_error = ""
            published += 1
        except (urllib.error.URLError, ERPError, OSError) as exc:
            event.last_error = str(exc)[:500]
            if event.attempts >= MAX_ATTEMPTS:
                event.status = OutboxStatus.FAILED
            failed += 1

    session.flush()
    return {"published": published, "failed": failed, "considered": len(events)}


# --------------------------------------------------------------------------
# Inbound
# --------------------------------------------------------------------------


def inject_order(
    session: Session,
    *,
    erp_order_id: str,
    model_code: str,
    quantity: int,
    line: Line,
    bom: dict | None = None,
    due_at=None,
) -> dict:
    """Take an order from the ERP, allocate serials, and queue units to build.

    Idempotent on ``erp_order_id``.
    """
    from ..engine.routing import active_flow

    existing = session.scalars(
        select(WorkOrder).where(WorkOrder.erp_order_id == erp_order_id)
    ).first()
    if existing:
        units = session.scalars(select(Unit).where(Unit.work_order_id == existing.id)).all()
        return {
            "work_order_id": existing.id,
            "duplicate": True,
            "serials": [u.serial for u in units],
        }

    flow = active_flow(session, line_id=line.id, model_code=model_code)
    if flow is None:
        raise ERPError(
            f"no released flow for model {model_code} on line {line.code}; "
            "release a routing before injecting orders"
        )

    order = WorkOrder(
        erp_order_id=erp_order_id,
        model_code=model_code,
        quantity=quantity,
        line_id=line.id,
        bom=bom or {},
        due_at=due_at,
    )
    session.add(order)
    session.flush()

    built_so_far = session.scalar(select(Unit.id).order_by(Unit.id.desc())) or 0
    serials: list[str] = []
    for offset in range(quantity):
        serial = allocate(
            sequence=built_so_far + offset + 1,
            model_code=model_code,
            plant_code=line.plant_code,
        )
        session.add(
            Unit(
                serial=serial,
                work_order_id=order.id,
                flow_id=flow.id,
                line_id=line.id,
                model_code=model_code,
                bom=bom or {},
                status=UnitStatus.QUEUED,
            )
        )
        serials.append(serial)

    session.flush()

    emit(
        session,
        topic="mos.order.accepted",
        aggregate=erp_order_id,
        payload={
            "erp_order_id": erp_order_id,
            "line": line.code,
            "flow": f"{flow.code} v{flow.version}",
            "serials": serials,
        },
    )
    return {"work_order_id": order.id, "duplicate": False, "serials": serials}


def outbox_summary(session: Session) -> dict:
    rows = session.scalars(select(OutboxEvent).order_by(OutboxEvent.id.desc()).limit(50)).all()
    by_status: dict[str, int] = {}
    for status in OutboxStatus:
        count = len(
            session.scalars(select(OutboxEvent.id).where(OutboxEvent.status == status)).all()
        )
        by_status[status.value] = count
    return {
        "counts": by_status,
        "recent": [
            {
                "id": e.id,
                "topic": e.topic,
                "aggregate": e.aggregate,
                "status": e.status.value,
                "attempts": e.attempts,
                "created_at": e.created_at.isoformat(),
                "error": e.last_error,
            }
            for e in rows
        ],
    }
