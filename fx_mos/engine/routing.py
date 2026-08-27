"""Flow authoring and release.

An engineer writes a flow, the MOS validates it, and releasing it makes it the
active routing for every unit started from that moment on. Units already on the
line keep the flow version they started with — that is what makes a build record
defensible a year later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Flow, FlowStatus, FlowStep, Line, Station, Unit, utcnow


class FlowError(Exception):
    """Raised when a flow cannot be authored or released."""


@dataclass
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


# --------------------------------------------------------------------------


def next_version(session: Session, code: str) -> int:
    latest = session.scalars(
        select(Flow.version).where(Flow.code == code).order_by(Flow.version.desc())
    ).first()
    return (latest or 0) + 1


def create_flow(
    session: Session,
    *,
    code: str,
    name: str,
    line: Line,
    model_code: str,
    notes: str = "",
) -> Flow:
    flow = Flow(
        code=code,
        name=name,
        version=next_version(session, code),
        line_id=line.id,
        model_code=model_code,
        notes=notes,
        status=FlowStatus.DRAFT,
    )
    session.add(flow)
    session.flush()
    return flow


def clone_flow(session: Session, source: Flow, *, notes: str = "") -> Flow:
    """Start a new draft from a released flow. The normal way to change a line."""
    draft = Flow(
        code=source.code,
        name=source.name,
        version=next_version(session, source.code),
        line_id=source.line_id,
        model_code=source.model_code,
        notes=notes or f"Derived from v{source.version}",
        status=FlowStatus.DRAFT,
    )
    session.add(draft)
    session.flush()
    for step in source.steps:
        # Append through the relationship, not session.add: otherwise the draft's
        # loaded collection goes stale and validate() reviews the wrong flow.
        draft.steps.append(
            FlowStep(
                station_id=step.station_id,
                sequence=step.sequence,
                code=step.code,
                name=step.name,
                work_instruction=step.work_instruction,
                mandatory=step.mandatory,
                interlock=step.interlock,
                checks=list(step.checks or []),
                expected_parts=list(step.expected_parts or []),
                standard_seconds=step.standard_seconds,
            )
        )
    session.flush()
    return draft


def add_step(
    session: Session,
    flow: Flow,
    *,
    station: Station,
    code: str,
    name: str,
    sequence: int | None = None,
    work_instruction: str = "",
    mandatory: bool = True,
    interlock: bool = True,
    checks: list | None = None,
    expected_parts: list | None = None,
    standard_seconds: float = 30.0,
) -> FlowStep:
    if flow.status is not FlowStatus.DRAFT:
        raise FlowError(
            f"flow {flow.code} v{flow.version} is {flow.status.value}; "
            "clone it to a new draft before editing"
        )
    if station.line_id != flow.line_id:
        raise FlowError(f"station {station.code} is not on line {flow.line_id}")

    if sequence is None:
        highest = max((s.sequence for s in flow.steps), default=0)
        sequence = highest + 10

    step = FlowStep(
        station_id=station.id,
        sequence=sequence,
        code=code,
        name=name,
        work_instruction=work_instruction,
        mandatory=mandatory,
        interlock=interlock,
        checks=checks or [],
        expected_parts=expected_parts or [],
        standard_seconds=standard_seconds,
    )
    flow.steps.append(step)
    session.flush()
    return step


# --------------------------------------------------------------------------


_REQUIRED_CHECK_KEYS = {"code", "name"}


def validate(session: Session, flow: Flow) -> ValidationReport:
    """Catch the mistakes that would otherwise be found by a stopped line."""
    errors: list[str] = []
    warnings: list[str] = []

    steps = sorted(flow.steps, key=lambda s: s.sequence)
    if not steps:
        errors.append("Flow has no steps.")
        return ValidationReport(False, errors, warnings)

    if len({s.sequence for s in steps}) != len(steps):
        errors.append("Two steps share a sequence number. Ordering would be ambiguous.")

    # Steps must not move backwards through the line.
    stations = {s.id: s for s in session.scalars(
        select(Station).where(Station.line_id == flow.line_id)
    )}
    last_seq = -1
    for step in steps:
        station = stations.get(step.station_id)
        if station is None:
            errors.append(f"Step {step.code} points at a station on another line.")
            continue
        if station.sequence < last_seq:
            errors.append(
                f"Step {step.code} runs at {station.code}, which is upstream of the "
                "previous step. A unit cannot travel backwards."
            )
        last_seq = max(last_seq, station.sequence)

    seen_checks: set[str] = set()
    for step in steps:
        for check in step.checks or []:
            missing = _REQUIRED_CHECK_KEYS - set(check)
            if missing:
                errors.append(f"Check in {step.code} is missing {sorted(missing)}.")
                continue
            key = f"{step.code}:{check['code']}"
            if key in seen_checks:
                errors.append(f"Check {check['code']} is declared twice in {step.code}.")
            seen_checks.add(key)

            lsl, usl = check.get("lsl"), check.get("usl")
            if lsl is not None and usl is not None and lsl >= usl:
                errors.append(
                    f"Check {check['code']} in {step.code} has a lower limit at or "
                    "above its upper limit."
                )
            if lsl is None and usl is None and check.get("interlock"):
                warnings.append(
                    f"Check {check['code']} interlocks the line but has no limits, "
                    "so it can never fail."
                )

        for part in step.expected_parts or []:
            if "part_number" not in part:
                errors.append(f"A part line in {step.code} has no part number.")

        if step.mandatory and step.interlock and not (step.checks or step.expected_parts):
            warnings.append(
                f"Step {step.code} interlocks the line but records nothing. "
                "Operators will be blocked with no way to satisfy it."
            )

    if not any(s.station and s.station.zone.value == "END_OF_LINE" for s in steps):
        warnings.append("No step runs at end of line. Units will never be signed off.")

    return ValidationReport(not errors, errors, warnings)


def release(session: Session, flow: Flow, *, released_by: str) -> Flow:
    """Make a draft the active routing. Archives the previous released version."""
    report = validate(session, flow)
    if not report.ok:
        raise FlowError("; ".join(report.errors))

    current = session.scalars(
        select(Flow).where(
            Flow.code == flow.code,
            Flow.status == FlowStatus.RELEASED,
            Flow.id != flow.id,
        )
    ).all()
    for old in current:
        old.status = FlowStatus.ARCHIVED

    flow.status = FlowStatus.RELEASED
    flow.released_at = utcnow()
    flow.released_by = released_by
    session.flush()
    return flow


def active_flow(session: Session, *, line_id: int, model_code: str) -> Flow | None:
    return session.scalars(
        select(Flow)
        .where(
            Flow.line_id == line_id,
            Flow.model_code == model_code,
            Flow.status == FlowStatus.RELEASED,
        )
        .order_by(Flow.version.desc())
    ).first()


def steps_for_station(flow: Flow, station_id: int) -> list[FlowStep]:
    return sorted(
        (s for s in flow.steps if s.station_id == station_id), key=lambda s: s.sequence
    )


def route_for_unit(session: Session, unit: Unit) -> list[FlowStep]:
    """The ordered list of steps this specific unit must clear."""
    flow = session.get(Flow, unit.flow_id)
    if flow is None:
        raise FlowError(f"unit {unit.serial} references a flow that no longer exists")
    stations = {s.id: s for s in session.scalars(
        select(Station).where(Station.line_id == flow.line_id)
    )}
    return sorted(
        flow.steps,
        key=lambda s: (stations[s.station_id].sequence, s.sequence),
    )
