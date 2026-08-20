"""Offline fallback portfolio: synthetic square footprints, no network required.

Prefer `fetch_osm_buildings.py`, which uses real OpenStreetMap footprints and makes
the map read as a deployment rather than a mock-up. This script exists so the project
can be regenerated with no internet at all - which matters on demonstration day, and
matters for CI.

Both scripts produce the same schema and share `_attrs.py`, so everything downstream
is indifferent to which one was run.

    python scripts/gen_buildings.py [--seed 20260814] [--count 50]
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from _attrs import make_properties, square_footprint

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


def generate(count: int, seed: int) -> dict:
    # Reproducibility is the requirement here, not unpredictability.
    rng = random.Random(seed)  # nosec B311
    features = []

    for index in range(count):
        district, base_lat, base_lon = DISTRICTS[index % len(DISTRICTS)]
        lat = base_lat + rng.uniform(-0.006, 0.006)
        lon = base_lon + rng.uniform(-0.008, 0.008)
        roof_area = round(rng.uniform(280, 2200), 1)

        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [square_footprint(lat, lon, roof_area)],
                },
                "properties": make_properties(rng, index, district, roof_area, lat, lon),
            }
        )

    return {
        "type": "FeatureCollection",
        "metadata": {
            "generator": "scripts/gen_buildings.py",
            "source": "synthetic - square footprints, no network",
            "seed": seed,
            "count": count,
            "note": "Offline fallback. Prefer scripts/fetch_osm_buildings.py.",
        },
        "features": features,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=20260814)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    collection = generate(args.count, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(collection, indent=1), encoding="utf-8")

    total_kwh = sum(f["properties"]["annual_kwh"] for f in collection["features"])
    print(f"wrote {args.out} - {args.count} synthetic buildings, seed {args.seed}")
    print(f"portfolio consumption: {total_kwh / 1e6:.2f} GWh/yr")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
