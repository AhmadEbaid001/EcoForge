"""Translation between stored rows and domain objects.

The domain layer must not import SQLAlchemy - that is what lets the optimizer run
against CSV fixtures with no infrastructure. This module is the one place allowed to
know about both sides.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gemp.db import BuildingRow, InterventionRow, ReadingRow, insert_ignore
from gemp.domain.models import Building, Intervention
from gemp.domain.portfolio import load_geojson


def to_domain_building(row: BuildingRow) -> Building:
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
