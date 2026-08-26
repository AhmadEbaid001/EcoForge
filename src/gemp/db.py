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

import threading
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
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.sql import Insert
from sqlalchemy.sql import insert as generic_insert

from gemp.config import get_settings

# JSONB in PostgreSQL, plain JSON everywhere else. The fallback exists so the
# ingestion path can be integration-tested against SQLite with no server running -
# which matters more than usual here, because the container stack needs hardware
# virtualisation that not every development machine has enabled.
JSONType = JSON().with_variant(JSONB(), "postgresql")

# SQLite auto-increments INTEGER PRIMARY KEY only - a BIGINT primary key silently
# fails its NOT NULL constraint on insert. BigInteger everywhere else.
AutoPK = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    pass


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, _record):
    """Make SQLite enforce foreign keys, as PostgreSQL does.

    SQLite ignores foreign-key constraints unless this pragma is set, which turns the
    contract tests into a weaker check than the database they stand in for. That is
    not hypothetical: a run row and its allocations were being inserted in the wrong
    order, PostgreSQL rejected the transaction, and the API returned a run_id for a
    run that never existed - while the SQLite-backed tests passed happily throughout.

    A test suite that accepts what production rejects is worse than no suite, because
    it converts an outage into a surprise.
    """
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


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

    # A5: normalised hour-of-day load shape (24 floats, mean 1.0), written by the
    # same nightly job. Null until the first refit after this migration - the
    # domain treats a missing shape as flat, which reproduces the old flat factor
    # exactly, so pre-existing databases need no backfill.
    load_shape: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    load_shape_source: Mapped[str | None] = mapped_column(String(16), nullable=True)

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

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
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

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
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
# identity, access and audit
# ---------------------------------------------------------------------------


class OrganizationRow(Base):
    """The tenancy seam, occupied by exactly one row today.

    Full multi-tenancy means an org_id on every table and a filter on every query,
    and the failure mode of getting one query wrong is showing one customer another
    customer's portfolio. That is not a change to rush. What this buys instead is the
    ability to add it without a user-table migration: users already belong somewhere,
    so scoping the portfolio later is additive rather than structural.
    """

    __tablename__ = "organization"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserRow(Base):
    """An account. `password_hash` is self-describing scrypt - see auth/passwords.py.

    Not called `user`: that is a reserved word in PostgreSQL, and while quoting makes
    it work, every hand-written query against it then needs quoting too, forever.
    """

    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )

    # Case-insensitive by storing the normalised form. Two accounts differing only in
    # capitalisation are an impersonation waiting to happen.
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))

    password_hash: Mapped[str] = mapped_column(Text)
    # Forces a change at next login. Set on every admin-issued password, so a
    # temporary credential cannot quietly become permanent.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SessionRow(Base):
    """A logged-in session.

    `token_hash`, never the token. A database dump, a stray backup or a SQL injection
    that reads this table gets values that cannot be replayed as a cookie - the same
    reason passwords are not stored either. The hash is SHA-256 rather than scrypt
    because the input is 32 bytes of CSPRNG output, not a guessable secret: there is
    nothing to brute force, and a login-rate KDF on every single request would be a
    self-inflicted denial of service.
    """

    __tablename__ = "user_session"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Absolute expiry, independent of activity. An idle timeout alone lets a stolen
    # cookie live forever as long as it is used.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Recorded for the "where am I signed in" view and for after-the-fact questions.
    # Deliberately not used to VALIDATE the session: IPs change mid-session on mobile
    # networks and user agents change on browser update, so binding to them logs
    # people out at random and buys very little.
    ip: Mapped[str] = mapped_column(String(45), default="")
    user_agent: Mapped[str] = mapped_column(String(256), default="")


class AuditRow(Base):
    """Who did what, when, and to what.

    Append-only by convention and by the absence of any code that updates or deletes
    a row. An action that changes shared state without leaving a trace is one nobody
    can answer questions about afterwards, and "the optimizer suddenly funds different
    buildings" is exactly the question that gets asked during a demonstration.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(AutoPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # Nullable: a failed login has no authenticated user by definition, and those are
    # precisely the events worth keeping.
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")

    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(128), default="")
    outcome: Mapped[str] = mapped_column(String(16), default="ok")
    ip: Mapped[str] = mapped_column(String(45), default="")
    detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True)


# ---------------------------------------------------------------------------
# engine / session
# ---------------------------------------------------------------------------

_engine = None
_SessionLocal = None


# Both lazy globals are built on first use, and first use is not single-threaded:
# the ingester runs in its own thread inside the API process and the scheduler in
# another, so two of them can reach an unset `_engine` at the same moment during
# start-up. Without the lock both would call create_engine and one would be
# discarded - along with its connection pool, which is not garbage a long-lived
# process should be quietly accumulating. Double-checked so the lock is paid for
# once rather than on every session.
#
# REENTRANT, and that is not a detail: get_sessionmaker holds this lock while it
# calls get_engine, which takes it again. A plain Lock deadlocks the first thread
# that builds both - which is every start-up, since the sessionmaker is what asks
# for the engine. The API came up, logged "application startup complete", and then
# hung on the first request that touched the database.
_init_lock = threading.RLock()


def get_engine():
    global _engine
    if _engine is None:
        with _init_lock:
            if _engine is None:
                _engine = create_engine(
                    get_settings().dsn, pool_pre_ping=True, future=True
                )
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        with _init_lock:
            if _SessionLocal is None:
                _SessionLocal = sessionmaker(
                    bind=get_engine(), expire_on_commit=False
                )
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
