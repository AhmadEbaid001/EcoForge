"""Fetch the streets and neighbouring buildings the portfolio sits among.

`fetch_osm_buildings.py` keeps fifty footprints and throws the district away. That
is why the map reads as fifty shapes on an empty field: correct geography that
nobody can place, because there is nothing around it to place it against. A
reviewer asked the obvious question - which building is that, and what is it next
to - and the map had no answer.

This fetches the same bounding box again and keeps what the other script
discards: the road network, and every other building footprint. The result is
written to `web/data/context.geojson` and served from this origin like any other
file in `web/`.

That last part is the whole point. A tile from a map provider would be an
off-origin request: blocked by the Content-Security-Policy, and dead on a
demonstration machine with the cable out (F13). Baking the surroundings into the
repository as one file keeps the context AND the offline guarantee.

    python scripts/fetch_osm_context.py
    python scripts/fetch_osm_context.py --bbox 30.02 31.47 30.05 31.52

Network use is a read-only GET against the public Overpass API, run once by hand.
Nothing fetches at runtime. If it fails the script says so and exits non-zero
rather than writing a half file; the map degrades to what it drew before.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

WEB_DATA = Path(__file__).resolve().parents[1] / "web" / "data"
OUT_PATH = WEB_DATA / "context.geojson"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# The same district `fetch_osm_buildings.py` draws the portfolio from.
DEFAULT_BBOX = (30.0200, 31.4700, 30.0500, 31.5200)   # south, west, north, east

# Roads worth drawing. `service` and `footway` are most of the ways in a
# residential OSM extract and, at the zoom this map opens on, they are noise
# that hides the streets someone would actually navigate by.
ROAD_CLASSES = {
    "motorway": 3.0, "motorway_link": 2.0,
    "trunk": 3.0, "trunk_link": 2.0,
    "primary": 2.4, "primary_link": 1.6,
    "secondary": 1.8, "secondary_link": 1.3,
    "tertiary": 1.4, "tertiary_link": 1.1,
    "residential": 1.0, "unclassified": 1.0, "living_street": 1.0,
}

# Coordinates are rounded to six decimals - about 11 cm, far finer than a
# stroke is wide, and it roughly halves the file.
PRECISION = 6


def overpass(query: str, timeout: int = 120) -> dict:
    request = urllib.request.Request(
        OVERPASS_URL,
        data=urllib.parse.urlencode({"data": query}).encode(),
        headers={"User-Agent": "gemp-robodam2026/0.1 (academic project)"},
    )
    # OVERPASS_URL is the https constant above, not an input.
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    with urllib.request.urlopen(request, timeout=timeout + 15) as response:  # nosec B310
        return json.load(response)


def fetch(bbox: tuple[float, float, float, float], timeout: int = 120) -> list[dict]:
    south, west, north, east = bbox
    box = f"{south},{west},{north},{east}"
    query = (
        f"[out:json][timeout:{timeout}];\n"
        f"(\n"
        f"  way[highway]({box});\n"
        f"  way[building]({box});\n"
        f"  way[natural=water]({box});\n"
        f"  way[leisure=park]({box});\n"
        f");\n"
        f"out geom;"
    )
    payload = overpass(query, timeout)
    return [e for e in payload.get("elements", [])
            if e.get("type") == "way" and e.get("geometry")]


def line(way: dict) -> list[list[float]]:
    return [[round(n["lon"], PRECISION), round(n["lat"], PRECISION)]
            for n in way["geometry"]]


def ring(way: dict) -> list[list[float]]:
    points = line(way)
    if points[0] != points[-1]:
        points.append(points[0])
    return points


def build(bbox, max_buildings: int) -> dict:
    ways = fetch(bbox)
    if not ways:
        raise RuntimeError("Overpass returned nothing for this bounding box")

    roads: list[dict] = []
    buildings: list[dict] = []
    water: list[dict] = []

    for way in ways:
        tags = way.get("tags", {})
        highway = tags.get("highway")
        if highway in ROAD_CLASSES:
            points = line(way)
            if len(points) < 2:
                continue
            roads.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": points},
                "properties": {"kind": "road", "class": highway,
                               "weight": ROAD_CLASSES[highway],
                               "name": tags.get("name")},
            })
        elif tags.get("building"):
            points = ring(way)
            if len(points) < 4:
                continue
            buildings.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [points]},
                "properties": {"kind": "building"},
            })
        elif tags.get("natural") == "water" or tags.get("leisure") == "park":
            points = ring(way)
            if len(points) < 4:
                continue
            water.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [points]},
                "properties": {"kind": "water" if tags.get("natural") == "water" else "park"},
            })

    # Biggest first, then capped. A cap by vertex count rather than by feature
    # count would be truer, but the outliers here are large compounds and those
    # are exactly the ones worth keeping as landmarks.
    buildings.sort(key=lambda f: len(f["geometry"]["coordinates"][0]), reverse=True)
    buildings = buildings[:max_buildings]

    return {
        "type": "FeatureCollection",
        "metadata": {
            "generator": "scripts/fetch_osm_context.py",
            "source": "OpenStreetMap via Overpass API, ODbL",
            "bbox": list(bbox),
            "counts": {"roads": len(roads), "buildings": len(buildings),
                       "water_and_parks": len(water)},
            "note": (
                "Context only: streets and neighbouring footprints so a portfolio "
                "building can be located against its surroundings. Carries no "
                "portfolio data. Served from this origin, never fetched at runtime."
            ),
        },
        "features": water + roads + buildings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bbox", type=float, nargs=4, default=list(DEFAULT_BBOX),
                        metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    parser.add_argument("--max-buildings", type=int, default=4000)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    try:
        collection = build(tuple(args.bbox), args.max_buildings)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
        print("      the map falls back to drawing the portfolio alone", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(collection, separators=(",", ":")), encoding="utf-8")

    counts = collection["metadata"]["counts"]
    size_kb = args.out.stat().st_size / 1024
    print(f"OK    {args.out}")
    print(f"      {counts['roads']} roads, {counts['buildings']} buildings, "
          f"{counts['water_and_parks']} water/parks, {size_kb:.0f} kB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
