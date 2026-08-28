"""Tests for the PARALLEL layout — an auto service shop.

The line tests guard sequence. These guard the two things a shop gets wrong:
sending a job to a bay that cannot do it, and letting a car out with a
safety-critical fastener unverified.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from fx_mos.engine import execution, gating, genealogy, nc, routing
from fx_mos.erp import warp
from fx_mos.models import Base, Disposition, Line, Station, Unit, UnitStatus
from fx_mos.seed_shop import SHOP_CODE, seed_shop

TYRES = [
    {"part_number": "TYRE-225-45R17", "serial_or_lot": f"DOT-3624-{n}", "quantity": 1}
    for n in range(1, 5)
]
GOOD_TORQUE = {
    "WHEEL_TQ_FL": 118, "WHEEL_TQ_FR": 115, "WHEEL_TQ_RL": 121, "WHEEL_TQ_RR": 117,
}


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as s:
        seed_shop(s)
        s.commit()
        yield s


@pytest.fixture()
def shop(session):
    return session.scalars(select(Line).where(Line.code == SHOP_CODE)).first()


@pytest.fixture()
def bays(shop):
    return {b.code: b for b in shop.stations}


def _vehicle(session, shop, plan="TYRSVC", ro="RO-1"):
    result = warp.inject_order(
        session, erp_order_id=ro, model_code=plan, quantity=1, line=shop
    )
    session.commit()
    return session.scalars(select(Unit).where(Unit.serial == result["serials"][0])).first()


def _steps(session, vehicle):
    return {s.code: s for s in routing.route_for_unit(session, vehicle)}


# --------------------------------------------------------------------------
# Bay assignment
# --------------------------------------------------------------------------


def test_a_tyre_job_cannot_go_to_a_bay_without_a_tyre_machine(session, shop, bays):
    vehicle = _vehicle(session, shop)
    result = execution.assign_bay(session, vehicle, bays["BAY-01"])
    assert result["assigned"] is False
    codes = {b["code"] for b in result["gate"]["blockers"]}
    assert "BAY_NOT_CAPABLE" in codes


def test_a_capable_bay_accepts_the_job(session, shop, bays):
    vehicle = _vehicle(session, shop)
    assert execution.assign_bay(session, vehicle, bays["BAY-03"])["assigned"] is True
    session.commit()
    assert vehicle.status is UnitStatus.IN_PROCESS


def test_a_bay_holds_one_vehicle_at_a_time(session, shop, bays):
    first = _vehicle(session, shop, ro="RO-1")
    second = _vehicle(session, shop, ro="RO-2")
    execution.assign_bay(session, first, bays["BAY-03"])
    session.commit()
    result = execution.assign_bay(session, second, bays["BAY-03"])
    assert result["assigned"] is False
    assert {b["code"] for b in result["gate"]["blockers"]} == {"BAY_OCCUPIED"}


def test_a_vehicle_can_be_moved_to_another_bay_mid_job(session, shop, bays):
    """Unlike a line, a shop can shuffle. The record follows the car."""
    vehicle = _vehicle(session, shop)
    execution.assign_bay(session, vehicle, bays["BAY-03"])
    session.commit()
    assert execution.assign_bay(session, vehicle, bays["BAY-09"])["assigned"] is True
    session.commit()
    assert vehicle.current_station_id == bays["BAY-09"].id


def test_there_is_no_next_bay_in_a_shop(session, shop, bays):
    vehicle = _vehicle(session, shop)
    execution.assign_bay(session, vehicle, bays["BAY-03"])
    session.commit()
    assert gating.next_station(session, vehicle) is None


# --------------------------------------------------------------------------
# The release gate — the one that carries the liability
# --------------------------------------------------------------------------


def test_a_car_cannot_be_released_before_the_work_is_done(session, shop, bays):
    vehicle = _vehicle(session, shop)
    execution.assign_bay(session, vehicle, bays["BAY-03"])
    session.commit()
    result = execution.release(session, vehicle)
    assert result["released"] is False
    assert any(b["code"] == "STEP_INCOMPLETE" for b in result["gate"]["blockers"])


def test_an_under_torqued_wheel_holds_the_car_in_the_bay(session, shop, bays):
    vehicle = _vehicle(session, shop)
    execution.assign_bay(session, vehicle, bays["BAY-03"])
    steps = _steps(session, vehicle)
    execution.run_step(
        session, unit=vehicle, step=steps["TYR-010"], operator="tech04", parts=TYRES
    )
    outcome = execution.run_step(
        session,
        unit=vehicle,
        step=steps["TYR-020"],
        operator="tech04",
        measurements={**GOOD_TORQUE, "WHEEL_TQ_RR": 62},
    )
    session.commit()

    assert outcome.passed is False
    assert vehicle.status is UnitStatus.HELD
    assert outcome.nc_code is not None

    blockers = {b.code for b in gating.release_check(session, vehicle).blockers}
    assert "OUT_OF_SPEC" in blockers
    assert "NC_OPEN" in blockers


def test_a_missing_tyre_scan_blocks_release(session, shop, bays):
    vehicle = _vehicle(session, shop)
    execution.assign_bay(session, vehicle, bays["BAY-03"])
    steps = _steps(session, vehicle)
    # Only three of the four tyres scanned.
    outcome = execution.run_step(
        session, unit=vehicle, step=steps["TYR-010"], operator="tech04", parts=TYRES[:3]
    )
    session.commit()
    assert outcome.passed is False
    assert any("was not scanned" in m for m in outcome.messages)


def test_a_clean_job_releases_and_leaves_the_bay(session, shop, bays):
    vehicle = _vehicle(session, shop)
    execution.assign_bay(session, vehicle, bays["BAY-03"])
    steps = _steps(session, vehicle)
    execution.run_step(
        session, unit=vehicle, step=steps["TYR-010"], operator="tech04", parts=TYRES
    )
    execution.run_step(
        session, unit=vehicle, step=steps["TYR-020"], operator="tech04",
        measurements=GOOD_TORQUE,
    )
    execution.run_step(
        session, unit=vehicle, step=steps["TYR-030"], operator="tech04",
        measurements={"TYRE_PSI": 35},
    )
    session.commit()

    result = execution.release(session, vehicle)
    session.commit()
    assert result["released"] is True
    assert vehicle.status is UnitStatus.COMPLETE
    assert vehicle.current_station_id is None  # bay is free again


def test_retorquing_after_a_hold_lets_the_car_out(session, shop, bays):
    """The failed reading stays on the record. The car still leaves."""
    vehicle = _vehicle(session, shop)
    execution.assign_bay(session, vehicle, bays["BAY-03"])
    steps = _steps(session, vehicle)
    execution.run_step(
        session, unit=vehicle, step=steps["TYR-010"], operator="tech04", parts=TYRES
    )
    execution.run_step(
        session, unit=vehicle, step=steps["TYR-020"], operator="tech04",
        measurements={**GOOD_TORQUE, "WHEEL_TQ_RR": 62},
    )
    session.commit()

    item = nc.open_for_unit(session, vehicle.id)[0]
    nc.disposition(
        session, item, decision=Disposition.REWORK, closed_by="foreman",
        resolution="Rear right re-torqued to 118 Nm with calibrated wrench.",
    )
    execution.run_step(
        session, unit=vehicle, step=steps["TYR-020"], operator="tech04",
        measurements=GOOD_TORQUE,
    )
    execution.run_step(
        session, unit=vehicle, step=steps["TYR-030"], operator="tech04",
        measurements={"TYRE_PSI": 35},
    )
    session.commit()

    assert execution.release(session, vehicle)["released"] is True

    record = genealogy.birth_certificate(session, vehicle)
    rr = [m for m in record["measurements"] if m["check"] == "WHEEL_TQ_RR"]
    assert len(rr) == 2
    assert rr[0]["in_spec"] is False and rr[1]["in_spec"] is True


# --------------------------------------------------------------------------
# The service record — what you show an angry customer
# --------------------------------------------------------------------------


def test_the_service_record_proves_what_was_fitted_and_measured(session, shop, bays):
    vehicle = _vehicle(session, shop)
    execution.assign_bay(session, vehicle, bays["BAY-03"])
    steps = _steps(session, vehicle)
    execution.run_step(
        session, unit=vehicle, step=steps["TYR-010"], operator="tech04", parts=TYRES
    )
    execution.run_step(
        session, unit=vehicle, step=steps["TYR-020"], operator="tech04",
        measurements=GOOD_TORQUE,
    )
    session.commit()

    record = genealogy.birth_certificate(session, vehicle)
    assert len(record["components"]) == 4
    assert {c["serial_or_lot"] for c in record["components"]} == {
        t["serial_or_lot"] for t in TYRES
    }
    torques = {m["check"]: m["value"] for m in record["measurements"]}
    assert torques["WHEEL_TQ_RR"] == 117
    assert all(s["operator"] == "tech04" for s in record["steps"])


def test_a_recalled_tyre_batch_is_traced_to_the_cars_that_have_it(session, shop, bays):
    for index, ro in enumerate(("RO-1", "RO-2"), start=3):
        vehicle = _vehicle(session, shop, ro=ro)
        execution.assign_bay(session, vehicle, bays[f"BAY-0{index}"])
        execution.run_step(
            session,
            unit=vehicle,
            step=_steps(session, vehicle)["TYR-010"],
            operator="tech04",
            parts=TYRES,
        )
    session.commit()

    hits = genealogy.where_used(session, serial_or_lot="DOT-3624-1")
    assert len(hits) == 2


# --------------------------------------------------------------------------
# Plan authoring
# --------------------------------------------------------------------------


def test_a_plan_needing_equipment_no_bay_has_is_refused(session, shop):
    released = routing.active_flow(session, line_id=shop.id, model_code="OILSVC")
    draft = routing.clone_flow(session, released)
    routing.add_step(
        session, draft, station=None, required_capability="DYNO",
        code="OIL-040", name="Power run on the dyno",
        checks=[{"code": "PWR", "name": "Peak power", "uom": "kW", "lsl": 80, "usl": 200}],
    )
    report = routing.validate(session, draft)
    assert not report.ok
    assert any("no bay in this shop has" in e for e in report.errors)


def test_a_shop_plan_is_not_judged_by_line_order_rules(session, shop):
    """Steps have no station, so the backwards-route check must not fire."""
    released = routing.active_flow(session, line_id=shop.id, model_code="TYRSVC")
    assert routing.validate(session, released).ok
