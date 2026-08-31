"""Getting a changed portfolio into a database that already has one.

This is not a hypothetical. The fixture was replaced with fifty named public
buildings, the deployed host was reseeded, the seeder logged "imported 50 buildings"
- and the database went on serving the fifty it already had, because every fixture
numbers its buildings b001..b050 and the insert skipped every colliding row. The
readings around them were generated from the NEW profiles, so for twenty minutes the
deployment held consumption attributed to buildings that were not the ones it
described.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from gemp.db import Base, BuildingRow
from gemp.repository import import_portfolio


def collection(name: str, district: str, annual_kwh: float = 1_000_000.0) -> dict:
    """One building, shaped like the GeoJSON the fetcher writes."""
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[31.2, 30.0], [31.3, 30.0],
                                                             [31.3, 30.1], [31.2, 30.0]]]},
            "properties": {
                "id": "b001", "code": "CAI-001", "name": name, "district": district,
                "lat": 30.05, "lon": 31.25,
                "floor_area_m2": 5000.0, "roof_area_m2": 1600.0, "glazing_area_m2": 900.0,
                "roof_orientation": "FLAT", "hvac_type": "chiller", "hvac_age_yr": 12,
                "insulation_quality": "fair", "occupancy_pattern": "office",
                "annual_kwh": annual_kwh,
            },
        }],
    }


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(bind=engine) as open_session:
        yield open_session


def one(session) -> BuildingRow:
    return session.execute(select(BuildingRow)).scalars().one()


def test_an_ordinary_import_leaves_what_is_already_there(session):
    """Start-up imports on every boot and must not undo a nightly refit."""
    import_portfolio(session, collection("Ministry of Health", "Cairo"))
    import_portfolio(session, collection("SOMETHING ELSE", "Giza"))

    assert one(session).name == "Ministry of Health"


def test_replacing_makes_the_database_match_the_fixture(session):
    """The seeder's import. A portfolio replaced in the repository has to reach the
    rows, or the map shows the old buildings against the new readings."""
    import_portfolio(session, collection("Office Building 1", "Downtown-CBD"))
    import_portfolio(session, collection("Ministry of Health", "Cairo"), replace=True)

    row = one(session)
    assert row.name == "Ministry of Health"
    assert row.district == "Cairo"


def test_replacing_clears_a_load_shape_measured_from_readings_that_are_gone(session):
    """The shape is a measurement of the readings a reseed has just deleted, and it
    is per building: carrying it onto a different building would weight the TOU
    objective with another building's day."""
    import_portfolio(session, collection("Office Building 1", "Downtown-CBD"))
    row = one(session)
    row.load_shape = [1.0] * 24
    row.load_shape_source = "measured"
    session.flush()

    import_portfolio(session, collection("Ministry of Health", "Cairo"), replace=True)

    row = one(session)
    assert row.load_shape is None
    assert row.load_shape_source is None


def test_replacing_still_inserts_a_building_the_database_does_not_have(session):
    """A fixture that grew has to import cleanly against a database that has not."""
    import_portfolio(session, collection("Ministry of Health", "Cairo"), replace=True)

    assert one(session).name == "Ministry of Health"


def test_replacing_resets_consumption_to_the_fixture_profile(session):
    """`annual_kwh` is refreshed nightly from the forecast, and the forecast is fit
    to readings. After a reseed both are gone, so the profile figure is the only
    honest value to cost against until the next refit."""
    import_portfolio(session, collection("Office Building 1", "Downtown-CBD", 4_000_000.0))
    row = one(session)
    row.annual_kwh = 9_999_999.0
    row.annual_kwh_source = "forecast"
    session.flush()

    import_portfolio(session, collection("Ministry of Health", "Cairo", 2_500_000.0),
                     replace=True)

    row = one(session)
    assert row.annual_kwh == 2_500_000.0
    assert row.annual_kwh_source == "profile"
