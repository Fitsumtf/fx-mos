"""A virtual line, so the system has something to control.

Real measurements are not random inside the spec window — they cluster around a
process mean with drift, and a fraction wander out. The simulator models that,
which means the interlocks, the NC queue and the OEE numbers all exercise the
real code paths rather than a happy path.

    python -m fx_mos.simulator --units 25 --defect-rate 0.06
"""

from __future__ import annotations

import argparse
import datetime as dt
import random

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import init_db, session_scope
from .engine import execution, gating, nc, routing
from .erp import warp
from .models import (
    Disposition,
    Flow,
    Line,
    NCStatus,
    NonConformance,
    Station,
    StationEvent,
    StationState,
    Unit,
    UnitStatus,
    utcnow,
)
from .seed import LINE_CODE, MODEL_CODE, seed

SUPPLIERS = ["Hanwa Forge", "Delta Cells", "Kroon Systems", "Bright Harness"]


def _measure(spec: dict, rng: random.Random, force_bad: bool) -> float:
    """Produce a value for a check: centred and capable, or deliberately out."""
    lsl, usl = spec.get("lsl"), spec.get("usl")

    if lsl is not None and usl is not None:
        centre = (lsl + usl) / 2
        # Cpk ~1.33 when in control: six sigma spans the window.
        sigma = (usl - lsl) / 8
        if force_bad:
            return round(rng.choice([lsl - sigma * 2, usl + sigma * 2]), 3)
        return round(rng.gauss(centre, sigma), 3)

    if usl is not None:  # max-only, e.g. fault codes or resistance
        if force_bad:
            return round(usl + max(usl * 0.5, 1.0), 3)
        return round(max(usl * rng.uniform(0.15, 0.7), 0), 3)

    if lsl is not None:  # min-only, e.g. isolation resistance
        if force_bad:
            return round(lsl * rng.uniform(0.3, 0.85), 3)
        return round(lsl * rng.uniform(1.4, 3.0), 3)

    return round(rng.uniform(0, 1), 3)


def _lot(rng: random.Random, part_number: str) -> str:
    return f"{part_number}-L{rng.randint(1, 6):02d}"


def _serial(rng: random.Random, part_number: str) -> str:
    return f"{part_number}-{rng.randint(100000, 999999)}"


def _log_time(
    session: Session,
    station: Station,
    state: StationState,
    seconds: float,
    *,
    at: dt.datetime,
    reason: str = "",
) -> None:
    session.add(
        StationEvent(
            station_id=station.id,
            state=state,
            started_at=at,
            ended_at=at + dt.timedelta(seconds=seconds),
            duration_seconds=seconds,
            reason=reason,
        )
    )


def run(
    session: Session,
    *,
    units: int = 20,
    defect_rate: float = 0.05,
    seed_value: int = 7,
    auto_disposition: bool = True,
) -> dict:
    rng = random.Random(seed_value)
    seed(session)

    line = session.scalars(select(Line).where(Line.code == LINE_CODE)).first()
    flow = routing.active_flow(session, line_id=line.id, model_code=MODEL_CODE)
    stations = {s.id: s for s in line.stations}

    order_id = f"SIM-{utcnow():%Y%m%d%H%M%S}-{rng.randint(100, 999)}"
    result = warp.inject_order(
        session,
        erp_order_id=order_id,
        model_code=MODEL_CODE,
        quantity=units,
        line=line,
        bom={"trim": "Long Range", "paint": "Deep Blue"},
    )
    session.commit()

    clock = utcnow() - dt.timedelta(hours=4)
    built, held, scrapped = 0, 0, 0

    for serial in result["serials"]:
        unit = session.scalars(select(Unit).where(Unit.serial == serial)).first()
        execution.start(session, unit)

        route = routing.route_for_unit(session, unit)
        by_station: dict[int, list] = {}
        for step in route:
            by_station.setdefault(step.station_id, []).append(step)

        for station in line.stations:
            if unit.status in (UnitStatus.SCRAPPED, UnitStatus.COMPLETE):
                break
            if unit.current_station_id != station.id:
                decision = gating.evaluate(session, unit, station)
                if not decision.allowed:
                    break
                execution.advance(session, unit, force_station=station)

            # Occasional real-world time losses.
            roll = rng.random()
            if roll < 0.05:
                downtime = rng.uniform(120, 600)
                _log_time(session, station, StationState.DOWN, downtime, at=clock,
                          reason=rng.choice(["tool fault", "fixture jam", "sensor fault"]))
                clock += dt.timedelta(seconds=downtime)
            elif roll < 0.11:
                starve = rng.uniform(30, 180)
                _log_time(session, station, StationState.STARVED, starve, at=clock,
                          reason="upstream empty")
                clock += dt.timedelta(seconds=starve)

            for step in by_station.get(station.id, []):
                force_bad = rng.random() < defect_rate

                measurements = {}
                for check in step.checks or []:
                    bad_here = force_bad and rng.random() < 0.5
                    measurements[check["code"]] = _measure(check, rng, bad_here)
                    if bad_here:
                        force_bad = False  # one defect per step is enough

                parts = []
                for expected in step.expected_parts or []:
                    pn = expected["part_number"]
                    parts.append(
                        {
                            "part_number": pn,
                            "serial_or_lot": (
                                _serial(rng, pn) if expected.get("serialized") else _lot(rng, pn)
                            ),
                            "quantity": expected.get("quantity", 1),
                            "supplier": rng.choice(SUPPLIERS),
                        }
                    )

                cycle = max(step.standard_seconds * rng.gauss(1.06, 0.12), 5)
                outcome = execution.run_step(
                    session,
                    unit=unit,
                    step=step,
                    operator=f"op{rng.randint(1, 9):02d}",
                    measurements=measurements,
                    parts=parts,
                    cycle_seconds=cycle,
                )
                _log_time(session, station, StationState.RUN, cycle, at=clock,
                          reason=f"{step.code} {unit.serial}")
                clock += dt.timedelta(seconds=cycle)

                if not outcome.passed:
                    break

            if unit.status is UnitStatus.HELD:
                if not auto_disposition:
                    break
                # Quality reviews the hold. Most are reworked, a few scrapped.
                open_items = nc.open_for_unit(session, unit.id)
                for item in open_items:
                    decision = (
                        Disposition.SCRAP if rng.random() < 0.12 else Disposition.REWORK
                    )
                    nc.disposition(
                        session,
                        item,
                        decision=decision,
                        closed_by="quality.lead",
                        resolution=(
                            "Fastener replaced and re-torqued to spec."
                            if decision is Disposition.REWORK
                            else "Damage outside repair limits."
                        ),
                    )
                if unit.status is UnitStatus.SCRAPPED:
                    scrapped += 1
                    break
                # Re-run the failed steps at this station.
                for step in by_station.get(station.id, []):
                    if unit.status in (UnitStatus.SCRAPPED, UnitStatus.COMPLETE):
                        break
                    already = session.scalars(
                        select(execution.UnitStepRecord).where(
                            execution.UnitStepRecord.unit_id == unit.id,
                            execution.UnitStepRecord.flow_step_id == step.id,
                        )
                    ).all()
                    if any(r.status.value == "COMPLETE" for r in already):
                        continue
                    measurements = {
                        c["code"]: _measure(c, rng, False) for c in (step.checks or [])
                    }
                    parts = [
                        {
                            "part_number": e["part_number"],
                            "serial_or_lot": (
                                _serial(rng, e["part_number"])
                                if e.get("serialized")
                                else _lot(rng, e["part_number"])
                            ),
                            "quantity": e.get("quantity", 1),
                            "supplier": rng.choice(SUPPLIERS),
                        }
                        for e in (step.expected_parts or [])
                        if not _already_scanned(session, unit, e["part_number"])
                    ]
                    rework_cycle = step.standard_seconds * rng.uniform(1.3, 2.2)
                    execution.run_step(
                        session,
                        unit=unit,
                        step=step,
                        operator="rework01",
                        measurements=measurements,
                        parts=parts,
                        cycle_seconds=rework_cycle,
                    )
                    _log_time(session, station, StationState.CHANGEOVER, rework_cycle,
                              at=clock, reason=f"rework {step.code}")
                    clock += dt.timedelta(seconds=rework_cycle)

        if unit.status not in (UnitStatus.SCRAPPED, UnitStatus.COMPLETE):
            execution.advance(session, unit)
        if unit.status is UnitStatus.COMPLETE:
            built += 1
        elif unit.status is UnitStatus.HELD:
            held += 1

        session.commit()

    warp.drain(session)
    session.commit()

    open_ncs = session.scalars(
        select(NonConformance).where(NonConformance.status != NCStatus.CLOSED)
    ).all()

    return {
        "order": order_id,
        "released": units,
        "signed_off": built,
        "held": held,
        "scrapped": scrapped,
        "open_ncs": len(open_ncs),
    }


def _already_scanned(session: Session, unit: Unit, part_number: str) -> bool:
    from .models import PartConsumption

    return bool(
        session.scalars(
            select(PartConsumption).where(
                PartConsumption.unit_id == unit.id,
                PartConsumption.part_number == part_number,
            )
        ).first()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Drive units through the virtual line.")
    parser.add_argument("--units", type=int, default=20)
    parser.add_argument("--defect-rate", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--reset", action="store_true", help="drop and recreate the database")
    args = parser.parse_args()

    init_db(drop=args.reset)
    with session_scope() as session:
        summary = run(
            session,
            units=args.units,
            defect_rate=args.defect_rate,
            seed_value=args.seed,
        )

    width = max(len(k) for k in summary)
    print("\nSimulation complete")
    print("-" * (width + 12))
    for key, value in summary.items():
        print(f"{key.replace('_', ' '):<{width}}  {value}")
    print()


if __name__ == "__main__":
    main()
