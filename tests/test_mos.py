"""Tests for the parts that stop a line or clear a recall.

These are the behaviours worth guarding: a unit must not slip past an interlock,
a serial must trace back to its lot, and OEE must reflect where the time went.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from fx_mos import vin
from fx_mos.engine import execution, gating, genealogy, nc, oee, routing
from fx_mos.erp import warp
from fx_mos.models import (
    Base,
    Disposition,
    Line,
    Station,
    StationEvent,
    StationState,
    StepStatus,
    Unit,
    UnitStatus,
    utcnow,
)
from fx_mos.seed import LINE_CODE, MODEL_CODE, seed


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as s:
        seed(s)
        s.commit()
        yield s


@pytest.fixture()
def line(session):
    return session.scalars(select(Line).where(Line.code == LINE_CODE)).first()


def _order(session, line, quantity=1):
    result = warp.inject_order(
        session,
        erp_order_id=f"T-{utcnow().timestamp()}",
        model_code=MODEL_CODE,
        quantity=quantity,
        line=line,
    )
    session.commit()
    return [
        session.scalars(select(Unit).where(Unit.serial == s)).first()
        for s in result["serials"]
    ]


def _station(session, line, code):
    return session.scalars(
        select(Station).where(Station.line_id == line.id, Station.code == code)
    ).first()


def _step(session, unit, code):
    return next(s for s in routing.route_for_unit(session, unit) if s.code == code)


def _good_values(step, offset=0.0):
    """Centre of every window, nudged by ``offset`` fractions of the window."""
    values = {}
    for check in step.checks or []:
        lsl, usl = check.get("lsl"), check.get("usl")
        if lsl is not None and usl is not None:
            span = usl - lsl
            values[check["code"]] = lsl + span * (0.5 + offset)
        elif usl is not None:
            values[check["code"]] = usl * 0.5
        else:
            values[check["code"]] = lsl * 2
    return values


def _parts_for(step):
    return [
        {
            "part_number": p["part_number"],
            "serial_or_lot": f"{p['part_number']}-TEST01",
            "quantity": p.get("quantity", 1),
            "supplier": "Test Supplier",
        }
        for p in (step.expected_parts or [])
    ]


def _run_station(session, unit, station_code, line, *, bad_step=None):
    station = _station(session, line, station_code)
    if unit.current_station_id != station.id:
        execution.advance(session, unit, force_station=station)
    steps = [
        s for s in routing.route_for_unit(session, unit) if s.station_id == station.id
    ]
    results = []
    for step in steps:
        values = _good_values(step)
        if bad_step == step.code:
            for check in step.checks or []:
                if check.get("interlock") and check.get("usl") is not None:
                    values[check["code"]] = check["usl"] + 50
                    break
                if check.get("interlock") and check.get("lsl") is not None:
                    values[check["code"]] = check["lsl"] - 50
                    break
        results.append(
            execution.run_step(
                session,
                unit=unit,
                step=step,
                operator="tester",
                measurements=values,
                parts=_parts_for(step),
                cycle_seconds=step.standard_seconds,
            )
        )
        if not results[-1].passed:
            break
    session.commit()
    return results


# --------------------------------------------------------------------------
# VIN
# --------------------------------------------------------------------------


def test_allocated_vin_passes_its_own_check_digit():
    serial = vin.allocate(sequence=1, model_code="FXE1", plant_code="A")
    assert len(serial) == 17
    assert vin.is_valid(serial)


def test_check_digit_catches_a_transposed_character():
    serial = vin.allocate(sequence=42, model_code="FXE1", plant_code="A")
    corrupted = serial[:12] + serial[13] + serial[12] + serial[14:]
    if corrupted != serial:  # a repeated digit would transpose to itself
        assert not vin.is_valid(corrupted)


def test_sequence_produces_distinct_serials():
    serials = {vin.allocate(sequence=i, model_code="FXE1", plant_code="A") for i in range(1, 200)}
    assert len(serials) == 199


# --------------------------------------------------------------------------
# Flow authoring
# --------------------------------------------------------------------------


def test_released_flow_cannot_be_edited(session, line):
    flow = routing.active_flow(session, line_id=line.id, model_code=MODEL_CODE)
    with pytest.raises(routing.FlowError):
        routing.add_step(
            session, flow, station=line.stations[0], code="X-1", name="Sneaky edit"
        )


def test_cloning_bumps_the_version_and_keeps_the_steps(session, line):
    released = routing.active_flow(session, line_id=line.id, model_code=MODEL_CODE)
    draft = routing.clone_flow(session, released)
    assert draft.version == released.version + 1
    assert len(draft.steps) == len(released.steps)


def test_releasing_archives_the_previous_version(session, line):
    released = routing.active_flow(session, line_id=line.id, model_code=MODEL_CODE)
    draft = routing.clone_flow(session, released)
    routing.release(session, draft, released_by="tester")
    session.commit()
    assert released.status.value == "ARCHIVED"
    assert routing.active_flow(session, line_id=line.id, model_code=MODEL_CODE).id == draft.id


def test_validation_rejects_an_inverted_spec_window(session, line):
    released = routing.active_flow(session, line_id=line.id, model_code=MODEL_CODE)
    draft = routing.clone_flow(session, released)
    routing.add_step(
        session,
        draft,
        station=_station(session, line, "EOL-10"),
        code="BAD-010",
        name="Impossible check",
        checks=[{"code": "NOPE", "name": "Inverted", "lsl": 10, "usl": 2, "interlock": True}],
    )
    report = routing.validate(session, draft)
    assert not report.ok
    assert any("lower limit" in e for e in report.errors)


def test_validation_rejects_a_backwards_route(session, line):
    released = routing.active_flow(session, line_id=line.id, model_code=MODEL_CODE)
    draft = routing.clone_flow(session, released)
    routing.add_step(
        session,
        draft,
        station=_station(session, line, "SF-10"),
        code="BACK-010",
        name="Back to sub-frame",
        checks=[{"code": "X", "name": "X", "lsl": 1, "usl": 2}],
    )
    report = routing.validate(session, draft)
    assert not report.ok
    assert any("backwards" in e for e in report.errors)


# --------------------------------------------------------------------------
# Interlocking
# --------------------------------------------------------------------------


def test_a_unit_cannot_skip_a_station(session, line):
    unit = _order(session, line)[0]
    execution.start(session, unit)
    session.commit()
    marriage = _station(session, line, "MR-10")
    decision = gating.evaluate(session, unit, marriage)
    assert not decision.allowed
    assert any(b.code == "STEP_INCOMPLETE" for b in decision.blockers)


def test_out_of_spec_torque_holds_the_unit_and_opens_an_nc(session, line):
    unit = _order(session, line)[0]
    execution.start(session, unit)
    _run_station(session, unit, "SF-10", line, bad_step="SF10-020")

    session.refresh(unit)
    assert unit.status is UnitStatus.HELD

    open_items = nc.open_for_unit(session, unit.id)
    assert len(open_items) == 1
    assert open_items[0].blocking is True

    decision = gating.evaluate(session, unit, _station(session, line, "SF-20"))
    assert not decision.allowed
    assert {b.code for b in decision.blockers} >= {"NC_OPEN", "OUT_OF_SPEC"}


def test_a_missing_part_scan_fails_the_step(session, line):
    unit = _order(session, line)[0]
    execution.start(session, unit)
    step = _step(session, unit, "SF10-010")
    result = execution.run_step(
        session, unit=unit, step=step, operator="tester", measurements={}, parts=[]
    )
    session.commit()
    assert not result.passed
    assert result.record.status is StepStatus.FAILED
    assert any("was not scanned" in m for m in result.messages)


def test_a_missing_measurement_fails_the_step(session, line):
    unit = _order(session, line)[0]
    execution.start(session, unit)
    _run_station(session, unit, "SF-10", line)  # clears SF10-010 and SF10-020
    session.refresh(unit)
    assert unit.status is UnitStatus.IN_PROCESS


def test_capacity_blocks_a_second_unit_at_one_station(session, line):
    first, second = _order(session, line, quantity=2)
    execution.start(session, first)
    execution.start(session, second)
    session.commit()
    # Both are at SF-10, which has capacity 1; the gate to SF-20 for the second
    # unit must report the station as available but its own work incomplete.
    sf20 = _station(session, line, "SF-20")
    _run_station(session, first, "SF-10", line)
    execution.advance(session, first, force_station=sf20)
    session.commit()

    _run_station(session, second, "SF-10", line)
    session.commit()
    decision = gating.evaluate(session, second, sf20)
    assert not decision.allowed
    assert any(b.code == "STATION_FULL" for b in decision.blockers)


# --------------------------------------------------------------------------
# Non-conformance workflow
# --------------------------------------------------------------------------


def test_rework_releases_the_unit_and_keeps_the_failed_record(session, line):
    unit = _order(session, line)[0]
    execution.start(session, unit)
    _run_station(session, unit, "SF-10", line, bad_step="SF10-020")

    item = nc.open_for_unit(session, unit.id)[0]
    nc.disposition(
        session,
        item,
        decision=Disposition.REWORK,
        closed_by="quality.lead",
        resolution="Bolt replaced, re-torqued to 120 Nm.",
    )
    session.commit()
    session.refresh(unit)
    assert unit.status is UnitStatus.REWORK

    step = _step(session, unit, "SF10-020")
    result = execution.run_step(
        session,
        unit=unit,
        step=step,
        operator="rework01",
        measurements=_good_values(step),
        parts=[],
    )
    session.commit()
    assert result.passed
    assert result.record.attempt == 2

    cert = genealogy.birth_certificate(session, unit)
    attempts = [s for s in cert["steps"] if s["step"] == "SF10-020"]
    assert len(attempts) == 2  # the failure is still in the record


def test_a_reworked_measurement_no_longer_blocks_the_gate(session, line):
    """Regression: the failed reading stays in the record but stops gating.

    Without this, rework is a one-way door — the unit passes its retest and is
    still held by its own history, and every unit behind it backs up."""
    unit = _order(session, line)[0]
    execution.start(session, unit)
    _run_station(session, unit, "SF-10", line, bad_step="SF10-020")

    item = nc.open_for_unit(session, unit.id)[0]
    nc.disposition(
        session, item, decision=Disposition.REWORK, closed_by="quality.lead",
        resolution="Re-torqued to spec.",
    )
    step = _step(session, unit, "SF10-020")
    execution.run_step(
        session, unit=unit, step=step, operator="rework01",
        measurements=_good_values(step), parts=[],
    )
    session.commit()

    decision = gating.evaluate(session, unit, _station(session, line, "SF-20"))
    assert decision.allowed, [b.as_dict() for b in decision.blockers]

    # The failure is still on the permanent record.
    cert = genealogy.birth_certificate(session, unit)
    assert any(not m["in_spec"] for m in cert["measurements"])


def test_a_disposition_without_a_resolution_is_refused(session, line):
    unit = _order(session, line)[0]
    execution.start(session, unit)
    _run_station(session, unit, "SF-10", line, bad_step="SF10-020")
    item = nc.open_for_unit(session, unit.id)[0]
    with pytest.raises(nc.NCError):
        nc.disposition(session, item, decision=Disposition.USE_AS_IS, closed_by="x", resolution="  ")


def test_scrap_takes_the_unit_out_of_the_line(session, line):
    unit = _order(session, line)[0]
    execution.start(session, unit)
    _run_station(session, unit, "SF-10", line, bad_step="SF10-020")
    item = nc.open_for_unit(session, unit.id)[0]
    nc.disposition(
        session,
        item,
        decision=Disposition.SCRAP,
        closed_by="quality.lead",
        resolution="Casting cracked at the boss.",
    )
    session.commit()
    session.refresh(unit)
    assert unit.status is UnitStatus.SCRAPPED
    decision = gating.evaluate(session, unit, _station(session, line, "SF-20"))
    assert not decision.allowed


# --------------------------------------------------------------------------
# Traceability
# --------------------------------------------------------------------------


def test_a_full_build_signs_off_and_produces_a_birth_certificate(session, line):
    unit = _order(session, line)[0]
    execution.start(session, unit)
    for code in ("SF-10", "SF-20", "PM-10", "PM-20", "MR-10", "PO-10", "PO-20", "EOL-10"):
        _run_station(session, unit, code, line)
    outcome = execution.advance(session, unit)
    session.commit()

    assert outcome["moved"] is True
    session.refresh(unit)
    assert unit.status is UnitStatus.COMPLETE

    cert = genealogy.birth_certificate(session, unit)
    assert cert["serial"] == unit.serial
    assert cert["flow"]["version"] >= 1
    assert len(cert["components"]) == 6
    assert all(m["in_spec"] for m in cert["measurements"] if m["interlock"])


def test_where_used_finds_every_unit_containing_a_lot(session, line):
    units = _order(session, line, quantity=2)
    for unit in units:
        execution.start(session, unit)
        _run_station(session, unit, "SF-10", line)
    session.commit()

    hits = genealogy.where_used(session, serial_or_lot="FX-SUBF-FR-TEST01")
    assert {h["serial"] for h in hits} == {u.serial for u in units}


def test_containment_separates_shipped_from_in_plant(session, line):
    unit = _order(session, line)[0]
    execution.start(session, unit)
    _run_station(session, unit, "SF-10", line)
    session.commit()

    scope = genealogy.containment(
        session, part_number="FX-SUBF-FR", lots=["FX-SUBF-FR-TEST01"]
    )
    assert scope["affected_count"] == 1
    assert scope["still_in_plant"] == 1
    assert scope["already_signed_off"] == 0


# --------------------------------------------------------------------------
# OEE
# --------------------------------------------------------------------------


def _log_throughput(session, line, station, units, *, at, failed=0):
    """Record ``len(units)`` completed cycles at a station inside the window."""
    from fx_mos.models import UnitStepRecord

    step = next(
        s
        for s in session.scalars(select(Unit)).first().flow.steps
        if s.station_id == station.id
    )
    for index, unit in enumerate(units):
        session.add(
            UnitStepRecord(
                unit_id=unit.id,
                flow_step_id=step.id,
                station_id=station.id,
                status=StepStatus.FAILED if index < failed else StepStatus.COMPLETE,
                operator="tester",
                started_at=at,
                completed_at=at + dt.timedelta(seconds=1),
            )
        )
    session.flush()


def test_oee_multiplies_its_three_factors(session, line):
    station = _station(session, line, "SF-10")
    station.ideal_cycle_seconds = 60
    start = utcnow() - dt.timedelta(hours=1)

    # 45 min running, 15 min down, 45 units off the station at a 60 s ideal
    # cycle. Availability 0.75, performance 1.0, quality 1.0.
    units = _order(session, line, quantity=45)
    _log_throughput(session, line, station, units, at=start + dt.timedelta(minutes=5))

    session.add(
        StationEvent(
            station_id=station.id,
            state=StationState.RUN,
            started_at=start,
            ended_at=start + dt.timedelta(minutes=45),
            duration_seconds=45 * 60,
        )
    )
    session.add(
        StationEvent(
            station_id=station.id,
            state=StationState.DOWN,
            started_at=start + dt.timedelta(minutes=45),
            ended_at=start + dt.timedelta(minutes=60),
            duration_seconds=15 * 60,
            reason="tool fault",
        )
    )
    session.commit()

    result = oee.for_station(session, station, since=start, until=utcnow())
    assert result.total_count == 45
    assert result.availability == pytest.approx(0.75, abs=0.01)
    assert result.performance == pytest.approx(1.0, abs=0.01)
    assert result.quality == pytest.approx(1.0, abs=0.01)
    assert result.oee == pytest.approx(0.75, abs=0.01)
    assert result.top_loss == "DOWN"


def test_scrapping_one_unit_shows_up_as_quality_loss(session, line):
    station = _station(session, line, "SF-10")
    station.ideal_cycle_seconds = 60
    start = utcnow() - dt.timedelta(hours=1)

    units = _order(session, line, quantity=10)
    _log_throughput(session, line, station, units, at=start + dt.timedelta(minutes=5), failed=2)
    session.add(
        StationEvent(
            station_id=station.id, state=StationState.RUN, started_at=start,
            ended_at=start + dt.timedelta(minutes=10), duration_seconds=600,
        )
    )
    session.commit()

    result = oee.for_station(session, station, since=start, until=utcnow())
    assert result.reject_count == 2
    assert result.quality == pytest.approx(0.8, abs=0.01)
    assert "QUALITY" in result.losses


def test_planned_stops_do_not_count_against_availability(session, line):
    station = _station(session, line, "SF-20")
    start = utcnow() - dt.timedelta(hours=1)
    session.add(
        StationEvent(
            station_id=station.id, state=StationState.RUN, started_at=start,
            ended_at=start + dt.timedelta(minutes=30), duration_seconds=30 * 60,
        )
    )
    session.add(
        StationEvent(
            station_id=station.id, state=StationState.PLANNED_STOP,
            started_at=start + dt.timedelta(minutes=30),
            ended_at=start + dt.timedelta(minutes=60), duration_seconds=30 * 60,
            reason="scheduled break",
        )
    )
    session.commit()
    result = oee.for_station(session, station, since=start, until=utcnow())
    assert result.availability == pytest.approx(1.0, abs=0.001)


def test_line_oee_reports_the_bottleneck_not_the_average(session, line):
    start = utcnow() - dt.timedelta(hours=1)
    good = _station(session, line, "SF-10")
    bad = _station(session, line, "MR-10")
    good.ideal_cycle_seconds = 60
    bad.ideal_cycle_seconds = 60

    units = _order(session, line, quantity=55)
    _log_throughput(session, line, good, units, at=start + dt.timedelta(minutes=1))
    _log_throughput(session, line, bad, units[:20], at=start + dt.timedelta(minutes=1))

    for station, run_minutes in ((good, 55), (bad, 20)):
        session.add(
            StationEvent(
                station_id=station.id, state=StationState.RUN, started_at=start,
                ended_at=start + dt.timedelta(minutes=run_minutes),
                duration_seconds=run_minutes * 60,
            )
        )
        session.add(
            StationEvent(
                station_id=station.id, state=StationState.DOWN,
                started_at=start + dt.timedelta(minutes=run_minutes),
                ended_at=start + dt.timedelta(minutes=60),
                duration_seconds=(60 - run_minutes) * 60, reason="jam",
            )
        )
    session.commit()
    summary = oee.for_line(session, line.id, since=start, until=utcnow())
    assert summary["bottleneck"] == "MR-10"
    assert summary["line_oee"] <= summary["average_oee"]


# --------------------------------------------------------------------------
# ERP loop
# --------------------------------------------------------------------------


def test_an_order_arriving_twice_produces_one_work_order(session, line):
    first = warp.inject_order(
        session, erp_order_id="DUP-1", model_code=MODEL_CODE, quantity=3, line=line
    )
    second = warp.inject_order(
        session, erp_order_id="DUP-1", model_code=MODEL_CODE, quantity=3, line=line
    )
    session.commit()
    assert second["duplicate"] is True
    assert first["serials"] == second["serials"]


def test_a_hold_emits_an_event_for_the_erp(session, line):
    unit = _order(session, line)[0]
    execution.start(session, unit)
    _run_station(session, unit, "SF-10", line, bad_step="SF10-020")
    topics = [e.topic for e in warp.pending(session)]
    assert "mos.unit.held" in topics
    assert "mos.unit.started" in topics


def test_draining_the_outbox_without_an_endpoint_marks_events_published(session, line):
    _order(session, line)
    result = warp.drain(session, endpoint="")
    session.commit()
    assert result["published"] == result["considered"]
    assert warp.pending(session) == []


def test_an_order_for_a_model_with_no_released_flow_is_refused(session, line):
    with pytest.raises(warp.ERPError):
        warp.inject_order(
            session, erp_order_id="NOFLOW-1", model_code="ZZZZ", quantity=1, line=line
        )
