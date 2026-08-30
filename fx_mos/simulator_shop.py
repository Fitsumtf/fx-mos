"""A day in the shop.

Eighty vehicles, ten bays, eight technicians. The point is not the throughput
number — it is that the interlocks fire under load. Under-torqued wheels, missed
tyre scans and low oil fills all appear at realistic rates, and the release gate
has to catch every one of them.

    python -m fx_mos.simulator_shop --vehicles 80
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
    Line,
    Station,
    StationEvent,
    StationState,
    Unit,
    UnitStatus,
    utcnow,
)
from .seed_shop import SHOP_CODE, seed_shop

# What comes through the door on a normal day.
JOB_MIX = [("OILSVC", 0.55), ("TYRSVC", 0.28), ("BRKSVC", 0.17)]
TECHS = [f"tech{n:02d}" for n in range(1, 9)]
SUPPLIERS = ["Nordfilter", "Meridian Parts", "Castoline", "Apex Tyre"]


def _pick_job(rng: random.Random) -> str:
    roll, running = rng.random(), 0.0
    for code, weight in JOB_MIX:
        running += weight
        if roll <= running:
            return code
    return JOB_MIX[-1][0]


def _value(spec: dict, rng: random.Random, bad: bool) -> float:
    lsl, usl = spec.get("lsl"), spec.get("usl")
    if lsl is not None and usl is not None:
        centre, sigma = (lsl + usl) / 2, (usl - lsl) / 8
        if bad:
            return round(rng.choice([lsl - sigma * 3, usl + sigma * 2]), 2)
        return round(rng.gauss(centre, sigma), 2)
    if usl is not None:
        return round(usl + rng.uniform(1, 3), 2) if bad else round(usl * rng.uniform(0, 0.5), 2)
    if lsl is not None:
        return round(lsl * 0.6, 2) if bad else round(lsl * rng.uniform(1.3, 2.2), 2)
    return round(rng.uniform(0, 1), 2)


def _parts_for(step, rng: random.Random, short: bool = False) -> list[dict]:
    out = []
    for spec in step.expected_parts or []:
        pn = spec["part_number"]
        quantity = int(spec.get("quantity", 1))
        if spec.get("serialized"):
            count = quantity - 1 if short and quantity > 1 else quantity
            for _ in range(count):
                out.append(
                    {
                        "part_number": pn,
                        "serial_or_lot": f"DOT-{rng.randint(1, 52):02d}{rng.randint(23, 26)}"
                        f"-{rng.randint(1000, 9999)}",
                        "quantity": 1,
                        "supplier": rng.choice(SUPPLIERS),
                    }
                )
        else:
            out.append(
                {
                    "part_number": pn,
                    "serial_or_lot": f"LOT-{rng.randint(100, 999)}",
                    "quantity": quantity * (0.8 if short else 1),
                    "supplier": rng.choice(SUPPLIERS),
                }
            )
    return out


def run(
    session: Session,
    *,
    vehicles: int = 80,
    defect_rate: float = 0.07,
    seed_value: int = 5,
    leave_in_bays: int = 0,
) -> dict:
    """Run a service day.

    ``leave_in_bays`` stops that many of the last vehicles partway through
    their job, so the shop looks like a shop at two in the afternoon rather
    than an empty yard at closing time. A board with nothing on it tells you
    nothing about whether the system works.
    """
    rng = random.Random(seed_value)
    seed_shop(session)

    shop = session.scalars(select(Line).where(Line.code == SHOP_CODE)).first()
    bays = list(shop.stations)

    # Anchor the day so it *ends* about now. Anchoring to 08:00 wall clock
    # meant that if you ran the demo at one in the morning, every bay event
    # landed outside the eight-hour reporting window and the utilisation panel
    # drew nothing — the data was there, the question was asked about the
    # wrong hours.
    day_minutes = max(vehicles * 8, 60)
    clock = utcnow() - dt.timedelta(minutes=day_minutes)

    free_at = {bay.id: clock for bay in bays}
    released = held = caught = in_bays = 0
    torque_catches = 0

    for index in range(1, vehicles + 1):
        plan = _pick_job(rng)
        order = warp.inject_order(
            session,
            erp_order_id=f"RO-{clock:%Y%m%d}-{index:04d}",
            model_code=plan,
            quantity=1,
            line=shop,
            bom={"job": plan},
        )
        vehicle = session.scalars(
            select(Unit).where(Unit.serial == order["serials"][0])
        ).first()

        steps = routing.route_for_unit(session, vehicle)
        needed = {s.required_capability for s in steps if s.required_capability}

        capable = [b for b in bays if needed <= set(b.capabilities or [])]
        if not capable:
            continue
        bay = min(capable, key=lambda b: free_at[b.id])
        start = max(free_at[bay.id], clock)

        if not execution.assign_bay(session, vehicle, bay)["assigned"]:
            continue

        # The last few vehicles are deliberately left mid-job.
        stop_after = None
        if leave_in_bays and index > vehicles - leave_in_bays:
            stop_after = rng.randint(0, max(len(steps) - 2, 0))

        elapsed = 0.0
        for step_no, step in enumerate(steps):
            if stop_after is not None and step_no > stop_after:
                break
            faulty = rng.random() < defect_rate
            short_parts = faulty and rng.random() < 0.25
            measurements = {}
            for check in step.checks or []:
                bad = faulty and not short_parts and rng.random() < 0.6
                measurements[check["code"]] = _value(check, rng, bad)
                if bad:
                    faulty = False

            minutes = step.standard_seconds / 60 * rng.gauss(1.05, 0.18)
            elapsed += max(minutes, 1)

            outcome = execution.run_step(
                session,
                unit=vehicle,
                step=step,
                operator=rng.choice(TECHS),
                measurements=measurements,
                parts=_parts_for(step, rng, short=short_parts),
                cycle_seconds=max(minutes, 1) * 60,
            )
            if not outcome.passed:
                caught += 1
                if any("torque" in m.lower() for m in outcome.messages):
                    torque_catches += 1

                items = nc.open_for_unit(session, vehicle.id)
                for item in items:
                    nc.disposition(
                        session,
                        item,
                        decision=Disposition.REWORK,
                        closed_by="foreman",
                        resolution="Corrected on the spot and re-verified.",
                    )
                # Do it again, properly.
                redo = {c["code"]: _value(c, rng, False) for c in (step.checks or [])}
                execution.run_step(
                    session,
                    unit=vehicle,
                    step=step,
                    operator="foreman",
                    measurements=redo,
                    parts=_parts_for(step, rng) if short_parts else [],
                    cycle_seconds=minutes * 60 * 0.5,
                )
                elapsed += minutes * 0.5

        session.add(
            StationEvent(
                station_id=bay.id,
                state=StationState.RUN,
                started_at=start,
                ended_at=start + dt.timedelta(minutes=elapsed),
                duration_seconds=elapsed * 60,
                reason=f"{plan} {vehicle.serial}",
            )
        )

        if stop_after is not None:
            # Still on the ramp. Not finished, not held — just mid-job.
            in_bays += 1
        else:
            result = execution.release(session, vehicle)
            if result["released"]:
                released += 1
            else:
                held += 1

        free_at[bay.id] = start + dt.timedelta(minutes=elapsed + rng.uniform(3, 9))
        session.commit()

    warp.drain(session)
    session.commit()

    return {
        "vehicles in": vehicles,
        "released": released,
        "still in bays": in_bays,
        "still held": held,
        "faults caught": caught,
        "torque faults caught": torque_catches,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a service day.")
    parser.add_argument("--vehicles", type=int, default=80)
    parser.add_argument("--defect-rate", type=float, default=0.07)
    parser.add_argument("--seed", type=int, default=5)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    init_db(drop=args.reset)
    with session_scope() as session:
        summary = run(
            session,
            vehicles=args.vehicles,
            defect_rate=args.defect_rate,
            seed_value=args.seed,
        )

    width = max(len(k) for k in summary)
    print("\nService day complete")
    print("-" * (width + 10))
    for key, value in summary.items():
        print(f"{key:<{width}}  {value}")
    print(
        "\nEvery fault above was caught before the vehicle left the bay.\n"
        "Without the gate, each one leaves as a comeback or a claim.\n"
    )


if __name__ == "__main__":
    main()
i did s
