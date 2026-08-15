"""Build the portfolio fixture from REAL building footprints via OpenStreetMap.

Fetches building ways from the Overpass API for a bounding box, keeps those large
enough to plausibly be public buildings, and attaches the synthetic operational
attributes from `_attrs.py` (HVAC age, insulation quality, occupancy, consumption),
which no open dataset provides.

Why bother when squares would render: the map is the demonstration. Real footprints
in a real district read as a deployment; a grid of identical squares reads as a
mock-up. It also removes a question from judging - "is this actual geography?" -
for about an hour of work.

    python scripts/fetch_osm_buildings.py                 # default district
    python scripts/fetch_osm_buildings.py --count 50 --min-area 400
    python scripts/gen_buildings.py                       # offline fallback, squares

Network use is a read-only GET against a public API. If it fails, the script says so
and exits non-zero rather than silently producing a different fixture; run
gen_buildings.py if you need to work offline.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from _attrs import centroid, make_properties, polygon_area_m2

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
OUT_PATH = DATA_DIR / "buildings.geojson"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# New Cairo / Fifth Settlement. Chosen for OSM building coverage, which is far
# better here than in the New Administrative Capital itself.
DEFAULT_BBOX = (30.0200, 31.4700, 30.0500, 31.5200)   # south, west, north, east

DISTRICT_NAMES = [
    "R3-Government",
    "R5-Residential",
    "Downtown-CBD",
    "Knowledge-City",
    "Medical-City",
]


def fetch_ways(bbox: tuple[float, float, float, float], timeout: int = 90) -> list[dict]:
    south, west, north, east = bbox
    query = (
        f"[out:json][timeout:{timeout}];\n"
        f"way[building]({south},{west},{north},{east});\n"
        f"out geom;"
    )
    request = urllib.request.Request(
        OVERPASS_URL,
        data=urllib.parse.urlencode({"data": query}).encode(),
        headers={"User-Agent": "gemp-robodam2026/0.1 (academic project)"},
    )
    with urllib.request.urlopen(request, timeout=timeout + 15) as response:
        payload = json.load(response)

    return [
        element
        for element in payload.get("elements", [])
        if element.get("type") == "way" and element.get("geometry")
    ]


def to_ring(way: dict) -> list[list[float]]:
    ring = [[node["lon"], node["lat"]] for node in way["geometry"]]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def assign_districts(features: list[dict]) -> None:
    """Split the portfolio into five contiguous districts by longitude.

    Contiguous bands rather than random labels, so the per-district cap constraint
    means something geographically and the map's district colouring is legible.
    """
    ordered = sorted(features, key=lambda f: f["_lon"])
    per_district = max(1, len(ordered) // len(DISTRICT_NAMES))
    for position, feature in enumerate(ordered):
        index = min(position // per_district, len(DISTRICT_NAMES) - 1)
        feature["_district"] = DISTRICT_NAMES[index]


def build(count: int, min_area: float, max_area: float, seed: int, bbox) -> dict:
    ways = fetch_ways(bbox)
    if not ways:
        raise RuntimeError("Overpass returned no building ways for this bounding box")

    usable = []
    for way in ways:
        ring = to_ring(way)
        area = polygon_area_m2(ring)
        if not (min_area <= area <= max_area):
            continue
        lat, lon = centroid(ring)
        usable.append({"_ring": ring, "_area": area, "_lat": lat, "_lon": lon,
                       "_osm_id": way.get("id")})

    if len(usable) < count:
        raise RuntimeError(
            f"only {len(usable)} footprints between {min_area:.0f} and {max_area:.0f} m2 "
            f"in this bbox; widen --bbox or lower --min-area"
        )

    rng = random.Random(seed)
    # Largest first, then a deterministic sample - biases toward the bigger buildings
    # a government portfolio would actually contain.
    usable.sort(key=lambda f: f["_area"], reverse=True)
    chosen = usable[: count * 3]
    chosen = rng.sample(chosen, count)
    assign_districts(chosen)

    features = []
    for index, item in enumerate(sorted(chosen, key=lambda f: (f["_district"], -f["_area"]))):
        properties = make_properties(
            rng, index, item["_district"], item["_area"], item["_lat"], item["_lon"],
            osm_id=item["_osm_id"],
        )
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [item["_ring"]]},
                "properties": properties,
            }
        )

    return {
        "type": "FeatureCollection",
        "metadata": {
            "generator": "scripts/fetch_osm_buildings.py",
            "source": "OpenStreetMap via Overpass API, ODbL",
            "bbox": list(bbox),
            "seed": seed,
            "count": count,
            "note": (
                "Footprints and locations are real OSM data. Operational attributes "
                "(occupancy, HVAC age, insulation quality, consumption) are synthetic; "
                "a deployment would collect them through the profile-entry form."
            ),
        },
        "features": features,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--min-area", type=float, default=300.0, help="m2")
    parser.add_argument("--max-area", type=float, default=8000.0, help="m2")
    parser.add_argument("--seed", type=int, default=20260814)
    parser.add_argument("--bbox", type=float, nargs=4, default=list(DEFAULT_BBOX),
                        metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    try:
        collection = build(args.count, args.min_area, args.max_area, args.seed,
                           tuple(args.bbox))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
        print("      offline fallback: python scripts/gen_buildings.py", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(collection, indent=1), encoding="utf-8")

    areas = [f["properties"]["roof_area_m2"] for f in collection["features"]]
    total_kwh = sum(f["properties"]["annual_kwh"] for f in collection["features"])
    print(f"wrote {args.out} - {len(areas)} real OSM footprints")
    print(f"roof area: {min(areas):,.0f} - {max(areas):,.0f} m2 "
          f"(median {sorted(areas)[len(areas) // 2]:,.0f})")
    print(f"portfolio consumption: {total_kwh / 1e6:.2f} GWh/yr")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
