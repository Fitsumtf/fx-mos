"""Seed a plausible line so the system has something to run on first boot.

The layout is the standard vehicle progression: sub-frame subassembly, the
pre-marriage build-up of body and battery, the marriage itself where they are
bolted together, post-marriage closures and fill, and end of line where the unit
is tested and signed off.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .engine import routing
from .models import Line, Station, Zone

LINE_CODE = "GA-1"
MODEL_CODE = "FXE1"
FLOW_CODE = "FXE1-GA"

STATIONS = [
    # (code, name, zone, sequence, ideal cycle seconds)
    ("SF-10", "Sub-frame build", Zone.SUBFRAME, 10, 82),
    ("SF-20", "Suspension load", Zone.SUBFRAME, 20, 88),
    ("PM-10", "Battery pack stage", Zone.PRE_MARRIAGE, 30, 90),
    ("PM-20", "Body drop prep", Zone.PRE_MARRIAGE, 40, 86),
    ("MR-10", "Marriage", Zone.MARRIAGE, 50, 96),
    ("PO-10", "Closures and harness", Zone.POST_MARRIAGE, 60, 90),
    ("PO-20", "Fluid fill", Zone.POST_MARRIAGE, 70, 78),
    ("EOL-10", "Electrical and roll test", Zone.END_OF_LINE, 80, 110),
]


def _steps(stations: dict[str, Station]) -> list[dict]:
    """The routing. Limits here are illustrative, not an engineering release."""
    return [
        {
            "station": stations["SF-10"],
            "code": "SF10-010",
            "name": "Load front sub-frame",
            "work_instruction": (
                "Scan the sub-frame casting. Confirm the casting lot on the label "
                "matches the scan before releasing the hoist."
            ),
            "expected_parts": [
                {"part_number": "FX-SUBF-FR", "quantity": 1, "serialized": True}
            ],
            "standard_seconds": 40,
        },
        {
            "station": stations["SF-10"],
            "code": "SF10-020",
            "name": "Torque sub-frame bolts",
            "work_instruction": (
                "Four bolts, star pattern. The tool reports angle and torque; both "
                "must land inside the window or the station will not release."
            ),
            "checks": [
                {"code": "SF_TORQUE_LH", "name": "Sub-frame bolt LH",
                 "uom": "Nm", "lsl": 108, "usl": 132, "interlock": True},
                {"code": "SF_TORQUE_RH", "name": "Sub-frame bolt RH",
                 "uom": "Nm", "lsl": 108, "usl": 132, "interlock": True},
                {"code": "SF_ANGLE", "name": "Final turn angle",
                 "uom": "deg", "lsl": 28, "usl": 42, "interlock": True},
            ],
            "standard_seconds": 46,
        },
        {
            "station": stations["SF-20"],
            "code": "SF20-010",
            "name": "Install suspension corners",
            "expected_parts": [
                {"part_number": "FX-SUSP-FL", "quantity": 1, "serialized": True},
                {"part_number": "FX-SUSP-FR", "quantity": 1, "serialized": True},
            ],
            "checks": [
                {"code": "SUSP_TORQUE", "name": "Knuckle bolt torque",
                 "uom": "Nm", "lsl": 150, "usl": 185, "interlock": True},
            ],
            "standard_seconds": 84,
        },
        {
            "station": stations["PM-10"],
            "code": "PM10-010",
            "name": "Stage battery pack",
            "work_instruction": (
                "Scan the pack serial. The pack carries its own cell genealogy; "
                "this scan is what links it to the vehicle."
            ),
            "expected_parts": [
                {"part_number": "FX-BATT-100", "quantity": 1, "serialized": True}
            ],
            "checks": [
                {"code": "PACK_ISO", "name": "Pack isolation resistance",
                 "uom": "MOhm", "lsl": 10, "usl": None, "interlock": True},
                {"code": "PACK_SOC", "name": "State of charge at stage",
                 "uom": "%", "lsl": 25, "usl": 45, "interlock": False},
            ],
            "standard_seconds": 86,
        },
        {
            "station": stations["PM-20"],
            "code": "PM20-010",
            "name": "Body drop preparation",
            "checks": [
                {"code": "BODY_GAP_L", "name": "Body datum gap LH",
                 "uom": "mm", "lsl": 3.2, "usl": 5.4, "interlock": True},
                {"code": "BODY_GAP_R", "name": "Body datum gap RH",
                 "uom": "mm", "lsl": 3.2, "usl": 5.4, "interlock": True},
            ],
            "standard_seconds": 80,
        },
        {
            "station": stations["MR-10"],
            "code": "MR10-010",
            "name": "Marriage — join body to platform",
            "work_instruction": (
                "The lift will not release until all six fasteners report inside "
                "the window. Do not bypass; a bypass here is a field failure."
            ),
            "checks": [
                {"code": "MAR_BOLT_1", "name": "Marriage bolt 1",
                 "uom": "Nm", "lsl": 190, "usl": 230, "interlock": True},
                {"code": "MAR_BOLT_2", "name": "Marriage bolt 2",
                 "uom": "Nm", "lsl": 190, "usl": 230, "interlock": True},
                {"code": "MAR_BOLT_3", "name": "Marriage bolt 3",
                 "uom": "Nm", "lsl": 190, "usl": 230, "interlock": True},
                {"code": "MAR_BOLT_4", "name": "Marriage bolt 4",
                 "uom": "Nm", "lsl": 190, "usl": 230, "interlock": True},
                {"code": "MAR_HEIGHT", "name": "Ride height after join",
                 "uom": "mm", "lsl": 138, "usl": 152, "interlock": True},
            ],
            "standard_seconds": 94,
        },
        {
            "station": stations["PO-10"],
            "code": "PO10-010",
            "name": "Fit closures and route harness",
            "expected_parts": [
                {"part_number": "FX-HARN-MAIN", "quantity": 1, "serialized": True},
            ],
            "checks": [
                {"code": "DOOR_GAP_FL", "name": "Front left door gap",
                 "uom": "mm", "lsl": 3.0, "usl": 4.6, "interlock": False},
                {"code": "HARN_CONT", "name": "Harness continuity",
                 "uom": "Ohm", "lsl": None, "usl": 0.6, "interlock": True},
            ],
            "standard_seconds": 88,
        },
        {
            "station": stations["PO-20"],
            "code": "PO20-010",
            "name": "Coolant and brake fill",
            "checks": [
                {"code": "COOL_VOL", "name": "Coolant volume",
                 "uom": "L", "lsl": 8.4, "usl": 9.2, "interlock": True},
                {"code": "BRAKE_VAC", "name": "Brake circuit vacuum decay",
                 "uom": "mbar", "lsl": None, "usl": 12, "interlock": True},
            ],
            "standard_seconds": 74,
        },
        {
            "station": stations["EOL-10"],
            "code": "EOL10-010",
            "name": "Electrical checkout",
            "expected_parts": [
                {"part_number": "FX-ECU-VCU", "quantity": 1, "serialized": True},
            ],
            "checks": [
                {"code": "HV_ISO", "name": "High voltage isolation",
                 "uom": "MOhm", "lsl": 20, "usl": None, "interlock": True},
                {"code": "DTC_COUNT", "name": "Stored fault codes",
                 "uom": "", "lsl": None, "usl": 0, "interlock": True},
            ],
            "standard_seconds": 60,
        },
        {
            "station": stations["EOL-10"],
            "code": "EOL10-020",
            "name": "Roll test and sign-off",
            "checks": [
                {"code": "ROLL_SPEED", "name": "Roll test peak speed",
                 "uom": "kph", "lsl": 55, "usl": 75, "interlock": True},
                {"code": "BRAKE_BAL", "name": "Brake balance delta",
                 "uom": "%", "lsl": None, "usl": 8, "interlock": True},
            ],
            "standard_seconds": 50,
        },
    ]


def seed(session: Session, *, released_by: str = "process.engineering") -> dict:
    """Create the line, stations and a released flow. Idempotent."""
    line = session.scalars(select(Line).where(Line.code == LINE_CODE)).first()
    if line is None:
        line = Line(code=LINE_CODE, name="General Assembly 1", plant_code="A", takt_seconds=92)
        session.add(line)
        session.flush()

    stations: dict[str, Station] = {}
    for code, name, zone, sequence, cycle in STATIONS:
        station = session.scalars(
            select(Station).where(Station.line_id == line.id, Station.code == code)
        ).first()
        if station is None:
            station = Station(
                line_id=line.id,
                code=code,
                name=name,
                zone=zone,
                sequence=sequence,
                ideal_cycle_seconds=cycle,
                capacity=1,
            )
            session.add(station)
            session.flush()
        stations[code] = station

    existing = routing.active_flow(session, line_id=line.id, model_code=MODEL_CODE)
    if existing:
        return {"line": line.code, "flow": f"{existing.code} v{existing.version}", "created": False}

    flow = routing.create_flow(
        session,
        code=FLOW_CODE,
        name="FXE1 general assembly",
        line=line,
        model_code=MODEL_CODE,
        notes="Initial release.",
    )
    for index, spec in enumerate(_steps(stations), start=1):
        routing.add_step(
            session,
            flow,
            station=spec["station"],
            code=spec["code"],
            name=spec["name"],
            sequence=index * 10,
            work_instruction=spec.get("work_instruction", ""),
            checks=spec.get("checks", []),
            expected_parts=spec.get("expected_parts", []),
            standard_seconds=spec.get("standard_seconds", 30),
        )

    routing.release(session, flow, released_by=released_by)
    return {"line": line.code, "flow": f"{flow.code} v{flow.version}", "created": True}
