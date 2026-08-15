"""Database schema and session handling.

**No PostGIS.** Building footprints are stored as GeoJSON in a JSONB column and the
district is a plain string. The system never runs a spatial query - it groups by
district for the cap constraint and hands geometry to Leaflet verbatim - so the
extension would add a deployment dependency and buy nothing. Recorded here because
"why is there no PostGIS in a GIS project" is a reasonable question to ask.

TimescaleDB is used for exactly two things: the `reading` hypertable, and the
`reading_hourly` continuous aggregate that keeps the dashboard and the forecasting
feature builder cheap.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.sql import Insert
from sqlalchemy.sql import insert as generic_insert

from gemp.config import get_settings

# JSONB in PostgreSQL, plain JSON everywhere else. The fallback exists so the
# ingestion path can be integration-tested against SQLite with no server running -
# which matters more than usual here, because the container stack needs hardware
# virtualisation that not every development machine has enabled.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def insert_ignore(table, dialect_name: str) -> Insert:
    """Dialect-appropriate "insert, skipping rows that already exist".

    Readings are deduplicated on (building_id, ts) because MQTT QoS 1 is
    at-least-once and redelivery is normal rather than exceptional.
    """
    if dialect_name == "postgresql":
        return pg_insert(table).on_conflict_do_nothing()
    if dialect_name == "sqlite":
        return generic_insert(table).prefix_with("OR IGNORE")
    return generic_insert(table)


# ---------------------------------------------------------------------------
# portfolio
# ---------------------------------------------------------------------------


class BuildingRow(Base):
    __tablename__ = "building"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(Text)
    district: Mapped[str] = mapped_column(String(64), index=True)

    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    footprint: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    floor_area_m2: Mapped[float] = mapped_column(Float)
    roof_area_m2: Mapped[float] = mapped_column(Float)
    glazing_area_m2: Mapped[float] = mapped_column(Float)
    roof_orientation: Mapped[str] = mapped_column(String(8))

    hvac_type: Mapped[str] = mapped_column(String(32))
    hvac_age_yr: Mapped[int] = mapped_column(Integer)
    insulation_quality: Mapped[str] = mapped_column(String(16))
    occupancy_pattern: Mapped[str] = mapped_column(String(32))

    # Refreshed nightly from the forecast (F3). The profile figure is the seed value.
    annual_kwh: Mapped[float] = mapped_column(Float)
    annual_kwh_source: Mapped[str] = mapped_column(String(16), default="profile")

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# time series
# ---------------------------------------------------------------------------


class ReadingRow(Base):
    """Metered consumption. Hypertable on `ts`, chunked daily.

    `seq` is per building and monotonic. It is part of the signed payload, which is
    what lets the verifier distinguish a deleted row from a modified one.
    """

    __tablename__ = "reading"

    building_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("building.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    kw: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))
    seq: Mapped[int] = mapped_column(BigInteger)
    sig: Mapped[bytes] = mapped_column(LargeBinary(32))

    __table_args__ = (
        Index("ix_reading_building_seq", "building_id", "seq"),
    )


class IntegrityCheckpointRow(Base):
    """Anchor for the hash chain (F5).

    Written hourly and mirrored to a file bind-mounted OUTSIDE the PostgreSQL volume.
    Without an external anchor, deleting the most recent rows leaves a chain that
    still verifies - there is nothing after the deleted tail to break.
    """

    __tablename__ = "integrity_checkpoint"

    building_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger)
    head_sig: Mapped[bytes] = mapped_column(LargeBinary(32))


# ---------------------------------------------------------------------------
# catalog snapshot and derived candidates
# ---------------------------------------------------------------------------


class InterventionRow(Base):
    """Snapshot of data/catalog.csv, loaded at startup.

    The CSV remains the source of truth that Nada edits; this table exists so a
    stored optimization run can be reproduced against the catalog as it was.
    """

    __tablename__ = "intervention"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    end_use: Mapped[str] = mapped_column(String(16))
    exclusive_group: Mapped[str] = mapped_column(String(32))
    adj_key: Mapped[str] = mapped_column(String(32), default="")
    cost_type: Mapped[str] = mapped_column(String(24))
    cost_value: Mapped[float] = mapped_column(Float)
    saving_frac: Mapped[float] = mapped_column(Float)
    service_life_yr: Mapped[int] = mapped_column(Integer)
    embodied_type: Mapped[str] = mapped_column(String(24))
    embodied_value: Mapped[float] = mapped_column(Float)
    applies_if: Mapped[str] = mapped_column(Text, default="")
    source_ref: Mapped[str] = mapped_column(Text)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CandidateRow(Base):
    """Materialised (building, option) pairs - the optimizer's direct input."""

    __tablename__ = "candidate"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    building_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("building.id", ondelete="CASCADE"), index=True
    )
    district: Mapped[str] = mapped_column(String(64), index=True)
    intervention_ids: Mapped[list] = mapped_column(JSONType)
    label: Mapped[str] = mapped_column(Text)

    cost_egp: Mapped[float] = mapped_column(Float)
    annual_kwh_saving: Mapped[float] = mapped_column(Float)
    lifetime_benefit_kgco2e: Mapped[float] = mapped_column(Float)
    annual_egp_saving: Mapped[float] = mapped_column(Float)

    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Hash of the catalog + params + annual_kwh that produced this row, so a stale
    # candidate set is detectable rather than silently reused.
    inputs_hash: Mapped[str] = mapped_column(String(64), index=True)


# ---------------------------------------------------------------------------
# model outputs
# ---------------------------------------------------------------------------


class ForecastRow(Base):
    __tablename__ = "forecast"

    building_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("building.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    yhat: Mapped[float] = mapped_column(Float)
    model_version: Mapped[str] = mapped_column(String(64))
    made_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AnomalyRow(Base):
    __tablename__ = "anomaly"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    building_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("building.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_kw: Mapped[float] = mapped_column(Float)
    expected_kw: Mapped[float] = mapped_column(Float)
    residual: Mapped[float] = mapped_column(Float)
    robust_z: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(16))
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("building_id", "ts", name="uq_anomaly_building_ts"),)


class OptimizationRunRow(Base):
    __tablename__ = "optimization_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    budget_egp: Mapped[float] = mapped_column(Float)
    objective: Mapped[str] = mapped_column(String(32))
    solver: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    max_funded_per_district: Mapped[int | None] = mapped_column(Integer, nullable=True)

    objective_value: Mapped[float] = mapped_column(Float)
    total_cost_egp: Mapped[float] = mapped_column(Float)
    total_kwh_saving: Mapped[float] = mapped_column(Float)
    total_benefit_kgco2e: Mapped[float] = mapped_column(Float)
    buildings_funded: Mapped[int] = mapped_column(Integer)
    solve_ms: Mapped[float] = mapped_column(Float)
    inputs_hash: Mapped[str] = mapped_column(String(64))


class AllocationRow(Base):
    __tablename__ = "allocation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("optimization_run.id", ondelete="CASCADE"), index=True
    )
    building_id: Mapped[str] = mapped_column(String(32), index=True)
    building_code: Mapped[str] = mapped_column(String(64))
    district: Mapped[str] = mapped_column(String(64))
    candidate_key: Mapped[str] = mapped_column(String(255))
    intervention_ids: Mapped[list] = mapped_column(JSONType)
    label: Mapped[str] = mapped_column(Text)
    cost_egp: Mapped[float] = mapped_column(Float)
    annual_kwh_saving: Mapped[float] = mapped_column(Float)
    lifetime_benefit_kgco2e: Mapped[float] = mapped_column(Float)
    annual_egp_saving: Mapped[float] = mapped_column(Float)


# ---------------------------------------------------------------------------
# engine / session
# ---------------------------------------------------------------------------

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().dsn, pool_pre_ping=True, future=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
