"""Synthetic building attributes, shared by both portfolio generators.

Real OSM footprints give us geometry and roof area. They do not give us HVAC age,
insulation quality, occupancy pattern or consumption - no open dataset does for
Egyptian public buildings. Those are generated here, deterministically from a seed,
and are exactly the fields a real deployment would collect through the profile-entry
form described in the proposal's Section 2.3.

The distributions are chosen so the portfolio is heterogeneous in the three
dimensions that make the best intervention building-specific rather than universal:
roof area relative to consumption, absolute roof area, and insulation quality.
"""

from __future__ import annotations

import math
import random

METERS_PER_DEG_LAT = 111_320.0

# (occupancy, weight, EUI range kWh/m2/yr, floors range)
OCCUPANCY_MIX: list[tuple[str, float, tuple[int, int], tuple[int, int]]] = [
    ("office", 0.40, (140, 190), (2, 6)),
    ("school", 0.22, (80, 120), (2, 4)),
    ("clinic", 0.18, (160, 220), (2, 5)),
    ("admin_24x7", 0.20, (200, 260), (3, 8)),
]

INSULATION_MIX = [("poor", 0.45), ("fair", 0.35), ("good", 0.20)]
ORIENTATION_MIX = [
    ("FLAT", 0.70), ("S", 0.10), ("SE", 0.06), ("SW", 0.06), ("E", 0.04), ("W", 0.04)
]
HVAC_TYPES = [("split", 0.35), ("package", 0.35), ("chiller", 0.30)]


def weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    return rng.choices([o[0] for o in options], weights=[o[1] for o in options], k=1)[0]


def polygon_area_m2(ring: list[list[float]]) -> float:
    """Ground area of a lon/lat ring, via the shoelace formula on a local projection.

    Equirectangular around the ring's own centroid. At building scale the distortion
    is far below the uncertainty in everything else we do with the number.
    """
    if len(ring) < 4:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    scale_x = METERS_PER_DEG_LAT * math.cos(math.radians(lat0))
    pts = [(p[0] * scale_x, p[1] * METERS_PER_DEG_LAT) for p in ring]

    total = 0.0
    for (x1, y1), (x2, y2) in zip(pts[:-1], pts[1:], strict=True):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def centroid(ring: list[list[float]]) -> tuple[float, float]:
    """(lat, lon) centroid of a ring, ignoring the repeated closing vertex."""
    pts = ring[:-1] if ring[0] == ring[-1] else ring
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts))


def square_footprint(lat: float, lon: float, area_m2: float) -> list[list[float]]:
    """A square polygon of the given ground area, centred on (lat, lon)."""
    half = math.sqrt(area_m2) / 2.0
    dlat = half / METERS_PER_DEG_LAT
    dlon = half / (METERS_PER_DEG_LAT * math.cos(math.radians(lat)))
    return [
        [lon - dlon, lat - dlat],
        [lon + dlon, lat - dlat],
        [lon + dlon, lat + dlat],
        [lon - dlon, lat + dlat],
        [lon - dlon, lat - dlat],
    ]


def make_properties(
    rng: random.Random,
    index: int,
    district: str,
    roof_area_m2: float,
    lat: float,
    lon: float,
    osm_id: int | None = None,
) -> dict:
    """Everything the domain model needs, given a footprint."""
    occupancy = weighted_choice(rng, [(o[0], o[1]) for o in OCCUPANCY_MIX])
    spec = next(o for o in OCCUPANCY_MIX if o[0] == occupancy)
    eui_lo, eui_hi = spec[2]
    floors_lo, floors_hi = spec[3]

    floors = rng.randint(floors_lo, floors_hi)
    floor_area = round(roof_area_m2 * floors, 1)
    glazing_area = round(floor_area * rng.uniform(0.08, 0.30), 1)
    annual_kwh = round(floor_area * rng.uniform(eui_lo, eui_hi), 0)

    props = {
        "id": f"b{index + 1:03d}",
        "code": f"NAC-{district.split('-')[0]}-{index + 1:03d}",
        "name": f"{occupancy.replace('_', ' ').title()} Building {index + 1}",
        "district": district,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "floor_area_m2": floor_area,
        "roof_area_m2": round(roof_area_m2, 1),
        "glazing_area_m2": glazing_area,
        "roof_orientation": weighted_choice(rng, ORIENTATION_MIX),
        "hvac_type": weighted_choice(rng, HVAC_TYPES),
        "hvac_age_yr": rng.randint(2, 25),
        "insulation_quality": weighted_choice(rng, INSULATION_MIX),
        "occupancy_pattern": occupancy,
        "annual_kwh": annual_kwh,
    }
    if osm_id is not None:
        props["osm_way_id"] = osm_id
    return props
