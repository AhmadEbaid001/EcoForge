"""Fetch the streets and neighbouring buildings the portfolio sits among.

`fetch_osm_buildings.py` keeps fifty footprints and throws the district away. That
is why the map reads as fifty shapes on an empty field: correct geography that
nobody can place, because there is nothing around it to place it against. A
reviewer asked the obvious question - which building is that, and what is it next
to - and the map had no answer.

This fetches what the other script discards - the road network, and every other
building footprint - and writes it to `web/data/context.geojson`, served from this
origin like any other file in `web/`.

It asks in two layers rather than one, because the portfolio spans Greater Cairo:
the arterial roads, water and parks over the whole extent, and then every street
and footprint within a few hundred metres of each portfolio building. Asking for
every road in Greater Cairo would be a refused query and an unusable file.

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
PORTFOLIO_PATH = Path(__file__).resolve().parents[1] / "data" / "buildings.geojson"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# The portfolio spans Greater Cairo now, and "every road and building in Greater
# Cairo" is a query no public Overpass instance should be asked for and a file no
# browser should be asked to draw. So the context arrives in two layers:
#
#   arterials  motorway and trunk roads, plus water and parks, over the whole
#              extent - the Nile and the ring road, which is what places a
#              building at the zoom the map opens on
#   local      every road and footprint within a few hundred metres of a portfolio
#              building, which is what places it once someone zooms in
#
# The local layer is fifty small boxes in one union rather than one large box, and
# that is the difference between a file of about a megabyte and a refused query.
DEFAULT_BBOX = (29.9000, 31.1000, 30.2000, 31.9000)   # south, west, north, east

# About 275 m at this latitude, in each direction from a building's centroid. At
# 440 m the file came back at 6.4 MB and 15,000 SVG nodes, which is a slow first
# paint in exchange for streets nobody zooms in far enough to read.
LOCAL_HALF_DEGREES = 0.0025

# Mainlines only. The `_link` classes are slip roads: 3,284 of them across Greater
# Cairo, 39,000 vertices, and at the zoom where the whole portfolio is on screen
# they draw as fuzz around every junction. They still arrive inside the local
# boxes, where they are a street someone can follow.
ARTERIAL_CLASSES = ("motorway", "trunk")

# About 22 m. What the whole-region layer is simplified to; see `simplify`.
ARTERIAL_TOLERANCE_DEGREES = 0.0002

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

# Coordinates are rounded to five decimals - about 1.1 m, still far finer than a
# stroke is wide at any zoom this map offers, and it takes about a third off the
# file. Six decimals was 11 cm, which was precision nothing could draw.
PRECISION = 5

# How many water and park polygons are worth keeping, largest first.
MAX_WATER_AND_PARKS = 120


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


def portfolio_boxes(path: Path) -> list[str]:
    """One small Overpass bbox per portfolio building, read from the fixture.

    Reading the fixture rather than taking a single bounding box means the local
    layer lands where the buildings are, however the portfolio is redrawn later.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    boxes = []
    for feature in data["features"]:
        props = feature["properties"]
        lat, lon = props["lat"], props["lon"]
        half = LOCAL_HALF_DEGREES
        boxes.append(
            f"{lat - half:.4f},{lon - half:.4f},{lat + half:.4f},{lon + half:.4f}"
        )
    return boxes


def fetch(bbox: tuple[float, float, float, float], boxes: list[str],
          timeout: int = 180) -> list[dict]:
    south, west, north, east = bbox
    whole = f"{south},{west},{north},{east}"
    arterials = "|".join(ARTERIAL_CLASSES)
    local = "".join(
        f"  way[highway]({box});\n  way[building]({box});\n" for box in boxes
    )
    query = (
        f"[out:json][timeout:{timeout}];\n"
        f"(\n"
        f'  way[highway~"^({arterials})$"]({whole});\n'
        f"  way[natural=water]({whole});\n"
        f"  way[leisure=park]({whole});\n"
        f"{local}"
        f");\n"
        f"out geom;"
    )
    payload = overpass(query, timeout)
    return [e for e in payload.get("elements", [])
            if e.get("type") == "way" and e.get("geometry")]


def line(way: dict) -> list[list[float]]:
    return [[round(n["lon"], PRECISION), round(n["lat"], PRECISION)]
            for n in way["geometry"]]


def simplify(points: list[list[float]], tolerance: float) -> list[list[float]]:
    """Ramer-Douglas-Peucker, in degrees.

    The arterial layer is the whole of Cairo's motorway and trunk network, and it
    arrived as 68,000 vertices, most of them describing the curve of a flyover to
    the metre. At the zoom where that layer is the point - the whole portfolio on
    one screen - a metre is a thousandth of a pixel, and the browser was spending
    890 ms a frame drawing it.
    """
    if len(points) < 3:
        return points

    first, last = points[0], points[-1]
    dx, dy = last[0] - first[0], last[1] - first[1]
    span = (dx * dx + dy * dy) ** 0.5

    worst, index = 0.0, 0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        if span == 0:
            distance = ((px - first[0]) ** 2 + (py - first[1]) ** 2) ** 0.5
        else:
            distance = abs(dy * px - dx * py + last[0] * first[1]
                           - last[1] * first[0]) / span
        if distance > worst:
            worst, index = distance, i

    if worst <= tolerance:
        return [first, last]
    return (simplify(points[:index + 1], tolerance)[:-1]
            + simplify(points[index:], tolerance))


def ring(way: dict) -> list[list[float]]:
    points = line(way)
    if points[0] != points[-1]:
        points.append(points[0])
    return points


def build(bbox, max_buildings: int, portfolio: Path) -> dict:
    ways = fetch(bbox, portfolio_boxes(portfolio))
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
            # The arterials span the whole region and are drawn at the zoom where
            # the region fits on a screen, so they are simplified. The local
            # streets are drawn when someone has zoomed into one building, where
            # the shape of the street is the information.
            if highway in ARTERIAL_CLASSES:
                points = simplify(points, ARTERIAL_TOLERANCE_DEGREES)
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

    # Same idea for green and blue: the Nile and the big parks are landmarks, the
    # 700 courtyard lawns behind them are 9,000 vertices of nothing.
    water.sort(key=lambda f: len(f["geometry"]["coordinates"][0]), reverse=True)
    water = water[:MAX_WATER_AND_PARKS]

    return {
        "type": "FeatureCollection",
        "metadata": {
            "generator": "scripts/fetch_osm_context.py",
            "source": "OpenStreetMap via Overpass API, ODbL",
            "bbox": list(bbox),
            "counts": {"roads": len(roads), "buildings": len(buildings),
                       "water_and_parks": len(water)},
            "note": (
                "Context only: arterial roads, water and parks across the whole "
                "portfolio extent, plus every street and footprint within a few "
                "hundred metres of a portfolio building, so one can be located "
                "against its surroundings at either zoom. Carries no portfolio "
                "data. Served from this origin, never fetched at runtime."
            ),
        },
        "features": water + roads + buildings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bbox", type=float, nargs=4, default=list(DEFAULT_BBOX),
                        metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    parser.add_argument("--max-buildings", type=int, default=900)
    parser.add_argument("--portfolio", type=Path, default=PORTFOLIO_PATH,
                        help="the fixture the local layer is drawn around")
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    try:
        collection = build(tuple(args.bbox), args.max_buildings, args.portfolio)
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
