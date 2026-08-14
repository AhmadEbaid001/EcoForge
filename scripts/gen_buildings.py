"""Generate the 50-building portfolio fixture used from Phase 0 onward.

Deterministic: same seed produces the same portfolio, so evaluation results are
reproducible and a regression test can pin them (Phase 4).

The generated portfolio is deliberately heterogeneous in the three dimensions that
make the best intervention building-specific rather than universal (F2):

  * roof area relative to consumption  -> whether solar clips against the
                                          self-consumption cap
  * absolute roof area                 -> how much the fixed solar cost hurts
  * insulation quality                 -> how much the fabric measures gain

so that the "solar wins here, insulation wins there" result emerges from the model
rather than being asserted.

Usage:
    python scripts/gen_buildings.py [--seed 20260814] [--count 50]
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
OUT_PATH = DATA_DIR / "buildings.geojson"

# New Administrative Capital, Egypt. Five district clusters.
DISTRICTS = [
    ("R3-Government", 30.0180, 31.7380),
    ("R5-Residential", 30.0055, 31.7620),
    ("Downtown-CBD", 29.9940, 31.7285),
    ("Knowledge-City", 30.0310, 31.7515),
    ("Medical-City", 29.9860, 31.7460),
]

OCCUPANCY_MIX = [
    # (occupancy, weight, EUI range kWh/m2/yr, floors range)
    ("office", 0.40, (140, 190), (2, 6)),
    ("school", 0.22, (80, 120), (2, 4)),
    ("clinic", 0.18, (160, 220), (2, 5)),
    ("admin_24x7", 0.20, (200, 260), (3, 8)),
]

INSULATION_MIX = [("poor", 0.45), ("fair", 0.35), ("good", 0.20)]
ORIENTATION_MIX = [("FLAT", 0.70), ("S", 0.10), ("SE", 0.06), ("SW", 0.06), ("E", 0.04), ("W", 0.04)]
HVAC_TYPES = [("split", 0.35), ("package", 0.35), ("chiller", 0.30)]

METERS_PER_DEG_LAT = 111_320.0


def weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    return rng.choices([o[0] for o in options], weights=[o[1] for o in options], k=1)[0]


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


def generate(count: int, seed: int) -> dict:
    rng = random.Random(seed)
    features = []

    for i in range(count):
        district, base_lat, base_lon = DISTRICTS[i % len(DISTRICTS)]
        lat = base_lat + rng.uniform(-0.006, 0.006)
        lon = base_lon + rng.uniform(-0.008, 0.008)

        occupancy = weighted_choice(rng, [(o[0], o[1]) for o in OCCUPANCY_MIX])
        spec = next(o for o in OCCUPANCY_MIX if o[0] == occupancy)
        eui_lo, eui_hi = spec[2]
        floors_lo, floors_hi = spec[3]

        floors = rng.randint(floors_lo, floors_hi)
        roof_area = round(rng.uniform(280, 2200), 1)   # footprint == roof area
        floor_area = round(roof_area * floors, 1)
        glazing_ratio = rng.uniform(0.08, 0.30)
        glazing_area = round(floor_area * glazing_ratio, 1)

        eui = rng.uniform(eui_lo, eui_hi)
        annual_kwh = round(floor_area * eui, 0)

        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [square_footprint(lat, lon, roof_area)],
                },
                "properties": {
                    "id": f"b{i + 1:03d}",
                    "code": f"NAC-{district.split('-')[0]}-{i + 1:03d}",
                    "name": f"{occupancy.replace('_', ' ').title()} Building {i + 1}",
                    "district": district,
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "floor_area_m2": floor_area,
                    "roof_area_m2": roof_area,
                    "glazing_area_m2": glazing_area,
                    "roof_orientation": weighted_choice(rng, ORIENTATION_MIX),
                    "hvac_type": weighted_choice(rng, HVAC_TYPES),
                    "hvac_age_yr": rng.randint(2, 25),
                    "insulation_quality": weighted_choice(rng, INSULATION_MIX),
                    "occupancy_pattern": occupancy,
                    "annual_kwh": annual_kwh,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "metadata": {
            "generator": "scripts/gen_buildings.py",
            "seed": seed,
            "count": count,
            "note": "Synthetic fixture. Replace with real OSM footprints in Phase 3.",
        },
        "features": features,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260814)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    collection = generate(args.count, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(collection, indent=1), encoding="utf-8")

    total_kwh = sum(f["properties"]["annual_kwh"] for f in collection["features"])
    print(f"wrote {args.out} - {args.count} buildings, seed {args.seed}")
    print(f"portfolio consumption: {total_kwh / 1e6:.2f} GWh/yr")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
