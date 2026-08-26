"""Translation between stored rows and domain objects.

The domain layer must not import SQLAlchemy - that is what lets the optimizer run
against CSV fixtures with no infrastructure. This module is the one place allowed to
know about both sides.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from gemp.db import (
    AnomalyRow,
    BuildingRow,
    CandidateRow,
    InterventionRow,
    ReadingRow,
    insert_ignore,
)
from gemp.domain.models import FLAT_SHAPE, Building, Candidate, Intervention
from gemp.domain.portfolio import load_geojson


# A building row whose load shape is missing, malformed or the wrong length is
# costed FLAT rather than trusted - a truncated JSON list would otherwise bias
# every TOU-weighted number for one building in a way nothing downstream checks.
def _shape_of(row: BuildingRow) -> tuple[float, ...] | None:
    raw = row.load_shape
    if not isinstance(raw, (list, tuple)) or len(raw) != 24:
        return None
    try:
        return tuple(float(v) for v in raw)
    except (TypeError, ValueError):
        return None


def to_domain_building(row: BuildingRow) -> Building:
    shape = _shape_of(row)
    return Building(
        id=row.id,
        code=row.code,
        name=row.name,
        district=row.district,
        lat=row.lat,
        lon=row.lon,
        floor_area_m2=row.floor_area_m2,
        roof_area_m2=row.roof_area_m2,
        glazing_area_m2=row.glazing_area_m2,
        roof_orientation=row.roof_orientation,
        hvac_type=row.hvac_type,
        hvac_age_yr=row.hvac_age_yr,
        insulation_quality=row.insulation_quality,
        occupancy_pattern=row.occupancy_pattern,
        annual_kwh=row.annual_kwh,
        hourly_shape=shape if shape is not None else FLAT_SHAPE,
    )


def load_buildings(session: Session) -> list[Building]:
    rows = session.execute(select(BuildingRow).order_by(BuildingRow.code)).scalars().all()
    return [to_domain_building(row) for row in rows]


def load_building_rows(session: Session) -> list[BuildingRow]:
    return list(session.execute(select(BuildingRow).order_by(BuildingRow.code)).scalars())


def import_portfolio(session: Session, geojson: dict | None = None) -> int:
    """Load the GeoJSON fixture into the database. Idempotent."""
    collection = geojson or load_geojson()
    now = datetime.now(UTC)

    records = []
    for feature in collection["features"]:
        props = feature["properties"]
        records.append({
            "id": props["id"],
            "code": props["code"],
            "name": props["name"],
            "district": props["district"],
            "lat": props["lat"],
            "lon": props["lon"],
            "footprint": feature["geometry"],
            "floor_area_m2": props["floor_area_m2"],
            "roof_area_m2": props["roof_area_m2"],
            "glazing_area_m2": props["glazing_area_m2"],
            "roof_orientation": props["roof_orientation"],
            "hvac_type": props["hvac_type"],
            "hvac_age_yr": props["hvac_age_yr"],
            "insulation_quality": props["insulation_quality"],
            "occupancy_pattern": props["occupancy_pattern"],
            "annual_kwh": props["annual_kwh"],
            "annual_kwh_source": "profile",
            "updated_at": now,
        })

    session.execute(insert_ignore(BuildingRow, session.bind.dialect.name), records)
    return len(records)


def import_catalog(session: Session, catalog: list[Intervention]) -> int:
    """Snapshot the CSV catalog so a stored run can be reproduced against it."""
    now = datetime.now(UTC)
    records = [
        {**iv.model_dump(exclude={"notes"}), "loaded_at": now} for iv in catalog
    ]
    session.execute(insert_ignore(InterventionRow, session.bind.dialect.name), records)
    return len(records)


def materialize_candidates(
    session: Session, candidates: Sequence[Candidate], inputs_hash: str
) -> int:
    """Persist the candidate set that a run was solved against.

    The optimizer works from an in-memory set for speed, so this table is not on the
    request path. It exists so a stored run can be explained months later - "why was
    this building funded" is answerable only if the options it was compared against
    were written down - and so a stale set is detectable via `inputs_hash` rather than
    silently reused after the catalog is corrected.

    Rows for other input hashes are cleared: keeping several generations would make
    "the current candidate set" ambiguous, which is the opposite of the point.
    """
    session.execute(delete(CandidateRow).where(CandidateRow.inputs_hash != inputs_hash))

    computed_at = datetime.now(UTC)
    records = [
        {
            "key": c.key,
            "building_id": c.building_id,
            "district": c.district,
            "intervention_ids": list(c.intervention_ids),
            "label": c.label,
            "cost_egp": c.cost_egp,
            "annual_kwh_saving": c.annual_kwh_saving,
            "lifetime_benefit_kgco2e": c.lifetime_benefit_kgco2e,
            "annual_egp_saving": c.annual_egp_saving,
            "computed_at": computed_at,
            "inputs_hash": inputs_hash,
        }
        for c in candidates
    ]
    if records:
        session.execute(insert_ignore(CandidateRow, session.bind.dialect.name), records)
    return len(records)


def stored_candidate_count(session: Session, inputs_hash: str | None = None) -> int:
    stmt = select(func.count()).select_from(CandidateRow)
    if inputs_hash:
        stmt = stmt.where(CandidateRow.inputs_hash == inputs_hash)
    return session.execute(stmt).scalar_one()


def latest_reading_ts(session: Session, building_id: str | None = None) -> datetime | None:
    """Newest stored reading, in DATA time.

    Every rolling window in the system is anchored here rather than on the wall clock.
    Under accelerated replay the two diverge immediately, and a window anchored on
    `now()` would silently come back empty.
    """
    stmt = select(func.max(ReadingRow.ts))
    if building_id:
        stmt = stmt.where(ReadingRow.building_id == building_id)
    return session.execute(stmt).scalar_one_or_none()


def read_series(
    session: Session,
    building_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 5000,
) -> list[ReadingRow]:
    stmt = select(ReadingRow).where(ReadingRow.building_id == building_id)
    if start:
        stmt = stmt.where(ReadingRow.ts >= start)
    if end:
        stmt = stmt.where(ReadingRow.ts < end)
    stmt = stmt.order_by(ReadingRow.ts.desc()).limit(limit)
    return list(reversed(list(session.execute(stmt).scalars())))


def read_chain(session: Session, building_id: str) -> list[dict]:
    """Every reading for one building, ordered by sequence, shaped for the verifier."""
    rows = session.execute(
        select(ReadingRow)
        .where(ReadingRow.building_id == building_id)
        .order_by(ReadingRow.seq)
    ).scalars().all()

    return [
        {"building_id": r.building_id, "ts": r.ts, "kw": r.kw,
         "source": r.source, "seq": r.seq, "sig": r.sig}
        for r in rows
    ]


def acknowledge_anomalies(
    session: Session,
    *,
    ids: Sequence[int] | None = None,
    building_id: str | None = None,
    before: datetime | None = None,
    severity: str | None = None,
    acknowledged: bool = True,
) -> int:
    """Mark anomalies acknowledged (or un-acknowledge them). Returns rows changed.

    The `acknowledged` column has existed since Phase 2 and two queries filter on it,
    but until now nothing could set it: the map counted open anomalies and offered no
    way to close one, so the "roughly 1.5 alerts per building" inbox only ever grew.
    Sixteen months of replay had accumulated over eleven thousand critical alerts that
    no one could clear.

    Every selector is optional but at least one is required by the caller, not here -
    see the API layer. Acknowledging is reversible by design (`acknowledged=False`),
    which is what makes a bulk operation over thousands of rows a safe thing to
    expose at all.
    """
    stmt = update(AnomalyRow).values(acknowledged=acknowledged)

    if ids:
        stmt = stmt.where(AnomalyRow.id.in_(list(ids)))
    if building_id:
        stmt = stmt.where(AnomalyRow.building_id == building_id)
    if before is not None:
        stmt = stmt.where(AnomalyRow.ts < before)
    if severity:
        stmt = stmt.where(AnomalyRow.severity == severity)

    # Only touch rows that would actually change, so the reported count means "alerts
    # this call closed" rather than "rows the WHERE clause happened to match".
    stmt = stmt.where(AnomalyRow.acknowledged.is_(not acknowledged))

    result = session.execute(stmt)
    return int(result.rowcount or 0)


def open_anomaly_count(session: Session, building_id: str | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(AnomalyRow)
        .where(AnomalyRow.acknowledged.is_(False))
    )
    if building_id:
        stmt = stmt.where(AnomalyRow.building_id == building_id)
    return int(session.execute(stmt).scalar_one())
