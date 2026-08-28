"""An auto service shop: ten bays, parallel, roughly eighty vehicles a day.

This is the same engine as the assembly line, configured differently. The bays
do not form a sequence — a vehicle goes into bay 4 and comes out of bay 4 — so
the shop runs a PARALLEL layout and the only gate that matters is the release
gate: may this car go back to its owner.

The interlocks here are chosen for one reason. When a customer comes back a
month later saying the work was not done, or claiming a wheel came loose, the
shop needs a record with a number on it. Torque values, filter lot codes, tyre
DOT codes, pad thickness. That record is the difference between eating a claim
and closing it in ninety seconds.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .engine import routing
from .models import Layout, Line, Station, Zone

SHOP_CODE = "SVC-1"

# capability tags
LIFT = "LIFT"
TYRE_MACHINE = "TYRE_MACHINE"
ALIGNMENT_RACK = "ALIGNMENT_RACK"
SCAN_TOOL = "SCAN_TOOL"
BRAKE_LATHE = "BRAKE_LATHE"

BAYS = [
    # (code, name, zone, capabilities, typical job minutes)
    ("BAY-01", "Quick lube 1", Zone.QUICK_SERVICE, [LIFT], 22),
    ("BAY-02", "Quick lube 2", Zone.QUICK_SERVICE, [LIFT], 22),
    ("BAY-03", "Tyre bay 1", Zone.QUICK_SERVICE, [LIFT, TYRE_MACHINE], 35),
    ("BAY-04", "Tyre bay 2", Zone.QUICK_SERVICE, [LIFT, TYRE_MACHINE], 35),
    ("BAY-05", "General repair 1", Zone.GENERAL_REPAIR, [LIFT, BRAKE_LATHE], 75),
    ("BAY-06", "General repair 2", Zone.GENERAL_REPAIR, [LIFT, BRAKE_LATHE], 75),
    ("BAY-07", "General repair 3", Zone.GENERAL_REPAIR, [LIFT], 75),
    ("BAY-08", "General repair 4", Zone.GENERAL_REPAIR, [LIFT], 75),
    ("BAY-09", "Alignment", Zone.ALIGNMENT, [LIFT, ALIGNMENT_RACK, TYRE_MACHINE], 55),
    ("BAY-10", "Diagnostics", Zone.DIAGNOSTIC, [LIFT, SCAN_TOOL], 60),
]


def _oil_service() -> list[dict]:
    return [
        {
            "code": "OIL-010",
            "name": "Check in and record odometer",
            "capability": LIFT,
            "instruction": (
                "Photograph the odometer and record the reading. This is the "
                "number the customer will quote back at you."
            ),
            "checks": [
                {"code": "ODOMETER", "name": "Odometer", "uom": "km",
                 "lsl": 0, "usl": 1000000, "interlock": False},
            ],
            "minutes": 3,
        },
        {
            "code": "OIL-020",
            "name": "Drain, replace filter, refill",
            "capability": LIFT,
            "instruction": (
                "Scan the filter box before fitting. The lot code on that box is "
                "what a manufacturer will ask for if the engine fails."
            ),
            "expected_parts": [
                {"part_number": "OIL-FILTER", "quantity": 1, "serialized": False},
                {"part_number": "ENGINE-OIL-5W30", "quantity": 5, "serialized": False},
            ],
            "checks": [
                {"code": "OIL_FILL", "name": "Oil quantity filled", "uom": "L",
                 "lsl": 4.2, "usl": 5.6, "interlock": True},
                {"code": "DRAIN_TORQUE", "name": "Drain plug torque", "uom": "Nm",
                 "lsl": 25, "usl": 35, "interlock": True},
            ],
            "minutes": 12,
        },
        {
            "code": "OIL-030",
            "name": "Leak check and reset service light",
            "capability": LIFT,
            "checks": [
                {"code": "LEAK_CHECK", "name": "Leaks found at drain and filter",
                 "uom": "", "lsl": None, "usl": 0, "interlock": True},
            ],
            "minutes": 5,
        },
    ]


def _tyre_service() -> list[dict]:
    return [
        {
            "code": "TYR-010",
            "name": "Fit tyres and record DOT codes",
            "capability": TYRE_MACHINE,
            "instruction": (
                "Scan each tyre. DOT codes matter: a recalled batch is traced by "
                "them, and so is a customer claiming you fitted old stock."
            ),
            "expected_parts": [
                {"part_number": "TYRE-225-45R17", "quantity": 4, "serialized": True},
            ],
            "minutes": 30,
        },
        {
            "code": "TYR-020",
            "name": "Torque wheel nuts to specification",
            "capability": TYRE_MACHINE,
            "instruction": (
                "Torque wrench only. No impact gun for final torque. Every wheel "
                "is recorded separately — this is the single check most likely to "
                "be disputed, and the only one that can kill somebody."
            ),
            "checks": [
                {"code": "WHEEL_TQ_FL", "name": "Wheel torque front left", "uom": "Nm",
                 "lsl": 100, "usl": 130, "interlock": True},
                {"code": "WHEEL_TQ_FR", "name": "Wheel torque front right", "uom": "Nm",
                 "lsl": 100, "usl": 130, "interlock": True},
                {"code": "WHEEL_TQ_RL", "name": "Wheel torque rear left", "uom": "Nm",
                 "lsl": 100, "usl": 130, "interlock": True},
                {"code": "WHEEL_TQ_RR", "name": "Wheel torque rear right", "uom": "Nm",
                 "lsl": 100, "usl": 130, "interlock": True},
            ],
            "minutes": 8,
        },
        {
            "code": "TYR-030",
            "name": "Set pressures",
            "capability": TYRE_MACHINE,
            "checks": [
                {"code": "TYRE_PSI", "name": "Cold pressure all round", "uom": "psi",
                 "lsl": 32, "usl": 38, "interlock": True},
            ],
            "minutes": 4,
        },
    ]


def _brake_service() -> list[dict]:
    return [
        {
            "code": "BRK-010",
            "name": "Measure discs and pads before work",
            "capability": LIFT,
            "instruction": (
                "Record the measurements you took, not the ones you expected. "
                "This is the evidence that the work was needed."
            ),
            "checks": [
                {"code": "PAD_MM_BEFORE", "name": "Pad thickness before", "uom": "mm",
                 "lsl": 0, "usl": 12, "interlock": False},
                {"code": "DISC_MM", "name": "Disc thickness", "uom": "mm",
                 "lsl": 22.0, "usl": 26.0, "interlock": True},
            ],
            "minutes": 10,
        },
        {
            "code": "BRK-020",
            "name": "Replace pads",
            "capability": LIFT,
            "expected_parts": [
                {"part_number": "BRAKE-PAD-FRONT", "quantity": 1, "serialized": False},
            ],
            "checks": [
                {"code": "CALIPER_TQ", "name": "Caliper bracket torque", "uom": "Nm",
                 "lsl": 95, "usl": 125, "interlock": True},
                {"code": "PAD_MM_AFTER", "name": "Pad thickness fitted", "uom": "mm",
                 "lsl": 9.0, "usl": 13.0, "interlock": True},
            ],
            "minutes": 35,
        },
        {
            "code": "BRK-030",
            "name": "Bed in and road test",
            "capability": LIFT,
            "checks": [
                {"code": "PEDAL_TRAVEL", "name": "Pedal travel", "uom": "mm",
                 "lsl": None, "usl": 60, "interlock": True},
                {"code": "BRAKE_IMBALANCE", "name": "Brake imbalance", "uom": "%",
                 "lsl": None, "usl": 20, "interlock": True},
            ],
            "minutes": 15,
        },
    ]


SERVICE_PLANS = {
    "OILSVC": ("Oil and filter service", _oil_service),
    "TYRSVC": ("Tyre replacement", _tyre_service),
    "BRKSVC": ("Front brake service", _brake_service),
}


def seed_shop(session: Session, *, released_by: str = "service.manager") -> dict:
    """Create the shop, its bays, and a released plan per service type."""
    shop = session.scalars(select(Line).where(Line.code == SHOP_CODE)).first()
    if shop is None:
        shop = Line(
            code=SHOP_CODE,
            name="FitEx Auto Service",
            plant_code="S",
            takt_seconds=0,
            layout=Layout.PARALLEL,
        )
        session.add(shop)
        session.flush()

    for index, (code, name, zone, capabilities, minutes) in enumerate(BAYS, start=1):
        existing = session.scalars(
            select(Station).where(Station.line_id == shop.id, Station.code == code)
        ).first()
        if existing is None:
            session.add(
                Station(
                    line_id=shop.id,
                    code=code,
                    name=name,
                    zone=zone,
                    sequence=index * 10,
                    ideal_cycle_seconds=minutes * 60,
                    capacity=1,
                    capabilities=capabilities,
                )
            )
    session.flush()

    created = []
    for model_code, (title, builder) in SERVICE_PLANS.items():
        if routing.active_flow(session, line_id=shop.id, model_code=model_code):
            continue
        flow = routing.create_flow(
            session,
            code=f"{model_code}-PLAN",
            name=title,
            line=shop,
            model_code=model_code,
            notes="Standard job. Limits are illustrative — set them from the "
            "vehicle manufacturer's data before use on real vehicles.",
        )
        for order, spec in enumerate(builder(), start=1):
            routing.add_step(
                session,
                flow,
                station=None,  # any capable bay
                required_capability=spec["capability"],
                code=spec["code"],
                name=spec["name"],
                sequence=order * 10,
                work_instruction=spec.get("instruction", ""),
                checks=spec.get("checks", []),
                expected_parts=spec.get("expected_parts", []),
                standard_seconds=spec["minutes"] * 60,
            )
        routing.release(session, flow, released_by=released_by)
        created.append(f"{flow.code} v{flow.version}")

    return {"shop": shop.code, "bays": len(BAYS), "plans": created}
