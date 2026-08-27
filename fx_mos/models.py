"""Persistent domain model for the FX Manufacturing Operating System.

The shape follows ISA-95: a plant contains lines, lines contain stations,
stations are grouped into zones (sub-frame -> pre-marriage -> marriage ->
post-marriage -> end of line). Work is described by a versioned flow, and every
physical thing that happens to a unit is written down exactly once.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    """Naive UTC timestamp. One clock, everywhere, no local time on the floor."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def _enum(py_enum, **kw):
    return SAEnum(py_enum, native_enum=False, length=32, validate_strings=True, **kw)


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class Zone(str, enum.Enum):
    SUBFRAME = "SUBFRAME"
    PRE_MARRIAGE = "PRE_MARRIAGE"
    MARRIAGE = "MARRIAGE"
    POST_MARRIAGE = "POST_MARRIAGE"
    END_OF_LINE = "END_OF_LINE"


ZONE_ORDER = [
    Zone.SUBFRAME,
    Zone.PRE_MARRIAGE,
    Zone.MARRIAGE,
    Zone.POST_MARRIAGE,
    Zone.END_OF_LINE,
]


class FlowStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    RELEASED = "RELEASED"
    ARCHIVED = "ARCHIVED"


class StepStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class UnitStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    IN_PROCESS = "IN_PROCESS"
    HELD = "HELD"
    REWORK = "REWORK"
    COMPLETE = "COMPLETE"
    SCRAPPED = "SCRAPPED"


class Severity(str, enum.Enum):
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


class NCStatus(str, enum.Enum):
    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    CLOSED = "CLOSED"


class Disposition(str, enum.Enum):
    REWORK = "REWORK"
    USE_AS_IS = "USE_AS_IS"
    SCRAP = "SCRAP"
    DEVIATION = "DEVIATION"


class StationState(str, enum.Enum):
    """OEE time buckets. Every second of the shift lands in exactly one."""

    RUN = "RUN"
    IDLE = "IDLE"
    DOWN = "DOWN"
    BLOCKED = "BLOCKED"   # downstream full
    STARVED = "STARVED"   # upstream empty
    CHANGEOVER = "CHANGEOVER"
    PLANNED_STOP = "PLANNED_STOP"  # excluded from planned production time


class OutboxStatus(str, enum.Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


# --------------------------------------------------------------------------
# Physical plant
# --------------------------------------------------------------------------


class Line(Base):
    __tablename__ = "lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    plant_code: Mapped[str] = mapped_column(String(1), default="A")
    takt_seconds: Mapped[float] = mapped_column(Float, default=90.0)

    stations: Mapped[list["Station"]] = relationship(
        back_populates="line", order_by="Station.sequence", cascade="all, delete-orphan"
    )


class Station(Base):
    __tablename__ = "stations"
    __table_args__ = (UniqueConstraint("line_id", "code", name="uq_station_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("lines.id"))
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    zone: Mapped[Zone] = mapped_column(_enum(Zone))
    sequence: Mapped[int] = mapped_column(Integer)
    ideal_cycle_seconds: Mapped[float] = mapped_column(Float, default=60.0)
    capacity: Mapped[int] = mapped_column(Integer, default=1)

    line: Mapped[Line] = relationship(back_populates="stations")


# --------------------------------------------------------------------------
# Process definition — the part an engineer edits and deploys
# --------------------------------------------------------------------------


class Flow(Base):
    """A versioned routing. Releasing a new version never rewrites history."""

    __tablename__ = "flows"
    __table_args__ = (UniqueConstraint("code", "version", name="uq_flow_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, default=1)
    line_id: Mapped[int] = mapped_column(ForeignKey("lines.id"))
    model_code: Mapped[str] = mapped_column(String(16))
    status: Mapped[FlowStatus] = mapped_column(_enum(FlowStatus), default=FlowStatus.DRAFT)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    released_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    steps: Mapped[list["FlowStep"]] = relationship(
        back_populates="flow", order_by="FlowStep.sequence", cascade="all, delete-orphan"
    )
    line: Mapped[Line] = relationship()


class FlowStep(Base):
    """One unit of work at one station.

    ``checks`` holds the measurement contract, e.g.::

        [{"code": "TORQUE_LH", "name": "Subframe bolt LH", "uom": "Nm",
          "lsl": 108.0, "usl": 132.0, "interlock": true}]

    ``expected_parts`` holds the BOM lines that must be scanned here::

        [{"part_number": "FX-BATT-100", "quantity": 1, "serialized": true}]
    """

    __tablename__ = "flow_steps"
    __table_args__ = (UniqueConstraint("flow_id", "code", name="uq_flow_step_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    flow_id: Mapped[int] = mapped_column(ForeignKey("flows.id"))
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160))
    work_instruction: Mapped[str] = mapped_column(Text, default="")
    mandatory: Mapped[bool] = mapped_column(Boolean, default=True)
    interlock: Mapped[bool] = mapped_column(Boolean, default=True)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    expected_parts: Mapped[list] = mapped_column(JSON, default=list)
    standard_seconds: Mapped[float] = mapped_column(Float, default=30.0)

    flow: Mapped[Flow] = relationship(back_populates="steps")
    station: Mapped[Station] = relationship()


# --------------------------------------------------------------------------
# Orders and units
# --------------------------------------------------------------------------


class WorkOrder(Base):
    __tablename__ = "work_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    erp_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    model_code: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[int] = mapped_column(Integer)
    line_id: Mapped[int] = mapped_column(ForeignKey("lines.id"))
    bom: Mapped[dict] = mapped_column(JSON, default=dict)
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    units: Mapped[list["Unit"]] = relationship(back_populates="work_order")


class Unit(Base):
    """A serialised thing being built. Its row is the birth certificate header."""

    __tablename__ = "units"

    id: Mapped[int] = mapped_column(primary_key=True)
    serial: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    work_order_id: Mapped[int] = mapped_column(ForeignKey("work_orders.id"))
    flow_id: Mapped[int] = mapped_column(ForeignKey("flows.id"))
    line_id: Mapped[int] = mapped_column(ForeignKey("lines.id"))
    model_code: Mapped[str] = mapped_column(String(16))
    bom: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[UnitStatus] = mapped_column(_enum(UnitStatus), default=UnitStatus.QUEUED)
    current_station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    work_order: Mapped[WorkOrder] = relationship(back_populates="units")
    flow: Mapped[Flow] = relationship()
    current_station: Mapped[Station | None] = relationship()


class UnitStepRecord(Base):
    __tablename__ = "unit_step_records"
    __table_args__ = (Index("ix_usr_unit_step", "unit_id", "flow_step_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    flow_step_id: Mapped[int] = mapped_column(ForeignKey("flow_steps.id"))
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[StepStatus] = mapped_column(_enum(StepStatus), default=StepStatus.PENDING)
    operator: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    cycle_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

    unit: Mapped[Unit] = relationship()
    flow_step: Mapped[FlowStep] = relationship()


class PartConsumption(Base):
    """Genealogy: which physical part, from which lot, went into which unit."""

    __tablename__ = "part_consumptions"
    __table_args__ = (Index("ix_part_serial", "serial_or_lot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    step_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("unit_step_records.id"), nullable=True
    )
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    part_number: Mapped[str] = mapped_column(String(64))
    serial_or_lot: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    supplier: Mapped[str] = mapped_column(String(64), default="")
    consumed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ProcessDatum(Base):
    """One measured value with its spec window at the moment of measurement."""

    __tablename__ = "process_data"
    __table_args__ = (Index("ix_pd_unit_check", "unit_id", "check_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    step_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("unit_step_records.id"), nullable=True
    )
    check_code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160), default="")
    value: Mapped[float] = mapped_column(Float)
    uom: Mapped[str] = mapped_column(String(16), default="")
    lsl: Mapped[float | None] = mapped_column(Float, nullable=True)
    usl: Mapped[float | None] = mapped_column(Float, nullable=True)
    in_spec: Mapped[bool] = mapped_column(Boolean, default=True)
    interlock: Mapped[bool] = mapped_column(Boolean, default=False)
    recorded_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class NonConformance(Base):
    __tablename__ = "non_conformances"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"))
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    flow_step_id: Mapped[int | None] = mapped_column(ForeignKey("flow_steps.id"), nullable=True)
    severity: Mapped[Severity] = mapped_column(_enum(Severity), default=Severity.MAJOR)
    blocking: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[NCStatus] = mapped_column(_enum(NCStatus), default=NCStatus.OPEN)
    disposition: Mapped[Disposition | None] = mapped_column(_enum(Disposition), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    opened_by: Mapped[str] = mapped_column(String(64), default="MOS")
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    closed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution: Mapped[str] = mapped_column(Text, default="")

    unit: Mapped[Unit] = relationship()
    station: Mapped[Station | None] = relationship()


class StationEvent(Base):
    """A closed time bucket at a station. Sums to the shift, used for OEE."""

    __tablename__ = "station_events"
    __table_args__ = (Index("ix_se_station_time", "station_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    state: Mapped[StationState] = mapped_column(_enum(StationState))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(160), default="")


class OutboxEvent(Base):
    """Transactional outbox. MOS never calls the ERP inside a floor transaction."""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(64), index=True)
    aggregate: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[OutboxStatus] = mapped_column(_enum(OutboxStatus), default=OutboxStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
