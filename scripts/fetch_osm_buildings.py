"""Build the portfolio fixture from REAL, NAMED public buildings via OpenStreetMap.

Fetches building ways from the Overpass API, keeps only the ones OpenStreetMap
actually names, and attaches the synthetic operational attributes from `_attrs.py`
(HVAC age, insulation quality, occupancy, consumption), which no open dataset
provides for Egyptian public buildings.

Two earlier versions of this file are the reason it looks like this.

The first asked for `way[building]` inside a bounding box and kept whatever was
large enough, then labelled the results "Office Building 4" and so on. An audit
against OSM found what those fifty footprints actually were: twenty apartment
blocks, seven residential, four commercial, three places of worship, a train
station and a house. The building labelled "Admin 24X7 Building 23" was a mosque;
"Office Building 11" was a shopping mall; "School Building 15" was a car-parts shop.

The second asked for public amenity and office tags, which fixed WHAT the buildings
were, but over New Cairo alone OSM names only twenty-six public ways between 300 and
8,000 m2 - so nineteen of the fifty still ended up called "School (2)". A portfolio
described as real government buildings should not contain nineteen buildings whose
name is a type and a number.

This version asks over Greater Cairo, where OSM holds 135 named public ways in the
same size band, and KEEPS ONLY NAMED ONES. Every building in the fixture carries the
name OSM gives that way, its `osm_way_id`, and a district resolved by reverse
geocoding its own centroid. Nothing about the identity of a building is invented: a
plausible ministry name on a footprint that is not that ministry is a fabricated
record, and this project is built on the opposite claim.

    python scripts/fetch_osm_buildings.py                 # Greater Cairo, 50 named
    python scripts/fetch_osm_buildings.py --count 40 --min-area 400
    python scripts/gen_buildings.py                       # offline fallback, squares

Network use is two read-only public APIs: Overpass for the footprints, Nominatim for
the districts. If either fails the script says so and exits non-zero rather than
silently producing a different fixture; run gen_buildings.py to work offline.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from _attrs import centroid, make_properties, polygon_area_m2

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
OUT_PATH = DATA_DIR / "buildings.geojson"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "gemp-robodam2026/0.1 (academic project)"

# Greater Cairo: Giza and Shubra in the west, the New Administrative Capital in the
# east, Helwan in the south. Wide on purpose - most of the NAMED public estate is in
# the old city, and a box drawn around New Cairo alone holds twenty-six named ways.
DEFAULT_BBOX = (29.9000, 31.1000, 30.2000, 31.9000)   # south, west, north, east

# What counts as public estate. `university` is deliberately absent: campus
# buildings are named `B1`, `D7`, `H5`, and the universities in range are private or
# their own authority rather than estate a ministry funds. `community_centre` is
# absent for a plainer reason - in this bbox every one of them is a gated-compound
# club house.
PUBLIC_SELECTORS = [
    "way[building][office=government]",
    "way[building][government]",
    "way[building=government]",
    "way[building=public]",
    "way[building=civic]",
    "way[building][amenity=townhall]",
    "way[building][amenity=courthouse]",
    "way[building][amenity=police]",
    "way[building][amenity=fire_station]",
    "way[building][amenity=school]",
    "way[building=school]",
    "way[building][amenity=hospital]",
    "way[building=hospital]",
    "way[building][amenity=clinic]",
    "way[building][amenity=library]",
]

# Tags that say "not Egyptian public estate" even though a public selector matched.
# An embassy is foreign sovereign property; a university building answers to its own
# authority rather than to a ministry.
EXCLUDE_TAGS = ("diplomatic", "embassy", "consulate")
EXCLUDE_TAG_VALUES = {"office": {"diplomatic"}, "building": {"university"}}

# OSM does not tag a private school as private, and it tags a professional syndicate
# as nothing more specific than `building=public`. These are the words that separate
# them here, matched against the OSM name. It is a blunt filter and the README names
# it as one: it keeps a private school whose name does not say so, and it would drop
# a state school that called itself an academy.
EXCLUDE_NAME_HINTS = (
    "international", "academy", "american", "british", "canadian", "german",
    "syndicate", "embassy", "consulate", "club house", "arab league",
    "نقابة",   # naqaba - syndicate, for the ways OSM names only in Arabic
)

# Preference is a mix rather than a ranking. Ranking by kind filled all fifty slots
# with ministries and police stations, because forty-four named state buildings are
# in range - but a real programme covers schools and hospitals too, and the
# optimizer has more to say when the portfolio's load shapes differ.
GROUP_OF_KIND = {
    "government": "state", "townhall": "state", "courthouse": "state",
    "public": "state", "civic": "state",
    "school": "education",
    "hospital": "health", "clinic": "health",
    "police": "safety", "fire_station": "safety",
    "library": "civic_life",
}
GROUP_QUOTA = {"state": 20, "education": 12, "health": 8, "safety": 6, "civic_life": 4}

# What a building of each kind does with energy. The previous version drew occupancy
# at random, which put a 24x7 admin load inside a primary school. The kind is known
# here, so it is used, and `admin_24x7` for hospitals and police stations is the
# point of the mapping: those buildings never go dark, which is most of why their
# retrofit is worth more than a school's.
KIND_OCCUPANCY = {
    "government": "office", "townhall": "office", "courthouse": "office",
    "public": "office", "civic": "office", "library": "office",
    "school": "school",
    "hospital": "admin_24x7", "clinic": "clinic",
    "police": "admin_24x7", "fire_station": "admin_24x7",
}

# About 330 m at this latitude. Two buildings inside one cell are two buildings in
# one compound, not two sites.
CELL_DEGREES = 0.003
PER_CELL = 3

# Nominatim asks for one request a second and no concurrency. Fifty buildings is
# under a minute, which is cheaper than shipping a hand-written table of district
# boundaries that would go stale and that nobody could check.
NOMINATIM_DELAY_S = 1.1

# Overpass answers a busy moment with a 5xx rather than a queue.
OVERPASS_ATTEMPTS = 4
OVERPASS_BACKOFF_S = 30


def fetch_ways(bbox: tuple[float, float, float, float], timeout: int = 180) -> list[dict]:
    """Public buildings only, with their tags.

    `out geom` carries the tags as well as the geometry, which the first version
    fetched and threw away - so the name OSM already held for each way was
    discarded and replaced with a generated one.
    """
    south, west, north, east = bbox
    selectors = "".join(
        f"  {sel}({south},{west},{north},{east});\n" for sel in PUBLIC_SELECTORS
    )
    query = (
        f"[out:json][timeout:{timeout}];\n(\n{selectors});\n"
        f"out geom;"
    )
    request = urllib.request.Request(
        OVERPASS_URL,
        data=urllib.parse.urlencode({"data": query}).encode(),
        headers={"User-Agent": USER_AGENT},
    )
    # Overpass is a shared free service with two slots per client, and it answers a
    # busy moment with 504 rather than a queue. Retrying is the documented way to
    # use it; failing the first time would send someone to the offline fallback and
    # a different fixture over a thirty-second wait.
    for attempt in range(OVERPASS_ATTEMPTS):
        try:
            # OVERPASS_URL is the https constant above, not an input.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(request, timeout=timeout + 15) as response:  # nosec B310
                payload = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == OVERPASS_ATTEMPTS - 1:
                raise
            print(f"  overpass {exc.code}, retrying in {OVERPASS_BACKOFF_S}s",
                  file=sys.stderr)
            time.sleep(OVERPASS_BACKOFF_S)

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


def reverse_district(lat: float, lon: float, zoom: int = 12) -> str | None:
    """The district OSM itself puts this point in.

    zoom 12 answers "which city or city district", which is the granularity the
    per-district cap needs: finer and every building is its own district, so the cap
    constrains nothing; coarser and the whole portfolio is one district, so the cap
    forbids nearly everything.
    """
    query = urllib.parse.urlencode({
        "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
        "format": "jsonv2", "zoom": zoom, "accept-language": "en",
    })
    request = urllib.request.Request(
        f"{NOMINATIM_URL}?{query}", headers={"User-Agent": USER_AGENT}
    )
    # NOMINATIM_URL is the https constant above; lat and lon are floats.
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    with urllib.request.urlopen(request, timeout=45) as response:  # nosec B310
        address = json.load(response).get("address", {})

    # `state` last and not as an afterthought: over central Cairo, zoom 12 answers
    # with the governorate alone - {"state": "Cairo"} and nothing else - and a
    # building in Zamalek belongs to Cairo as surely as one that came back with
    # city="Cairo" did.
    for key in ("city_district", "town", "city", "suburb", "county",
                "state_district", "state"):
        value = address.get(key)
        if value:
            return value
    return None


def assign_districts(features: list[dict]) -> None:
    """Give every building the district its own coordinates resolve to.

    The previous version cut the bounding box into four bands of longitude and named
    them after the cities those bands mostly contained. That held because the box
    was small. Over Greater Cairo it would be fiction, and the per-district cap is a
    real constraint in the optimizer: a cap of two per district means something if
    the districts are places and nothing at all if they are slices of a rectangle.
    """
    for feature in features:
        district = reverse_district(feature["_lat"], feature["_lon"])
        if district is None:
            raise RuntimeError(
                f"Nominatim returned no district for way {feature['_osm_id']} "
                f"at {feature['_lat']:.5f},{feature['_lon']:.5f}"
            )
        feature["_district"] = district
        time.sleep(NOMINATIM_DELAY_S)


def spread(candidates: list[dict], count: int) -> list[dict]:
    """Take the best `count`, but never more than a handful from one compound.

    Without this the portfolio collapsed onto a single facility: twenty-two
    consecutive `building=public` ways, all unnamed, all inside a hundred metres of
    each other, took forty-four per cent of the fifty slots. That is one site mapped
    as many small structures - not twenty-two separately metered buildings - and it
    would have made the district cap meaningless and the map a single dense blob.

    Candidates arrive in preference order, so this keeps that order and simply skips
    a building once its cell is full. A second pass relaxes the limit rather than
    returning short: a portfolio of the right size matters more than perfect spacing
    when the region runs out of public buildings.
    """
    picked: list[dict] = []
    used: dict[tuple[int, int], int] = {}

    for item in candidates:
        cell = (int(item["_lat"] / CELL_DEGREES), int(item["_lon"] / CELL_DEGREES))
        if used.get(cell, 0) >= PER_CELL:
            continue
        used[cell] = used.get(cell, 0) + 1
        picked.append(item)
        if len(picked) == count:
            return picked

    chosen_ids = {id(x) for x in picked}
    for item in candidates:
        if len(picked) == count:
            break
        if id(item) not in chosen_ids:
            picked.append(item)
    return picked


def public_kind(tags: dict) -> str:
    """Which flavour of public building OSM says this is.

    The selectors that matched are not reported back by Overpass, so the kind is
    read off the tags in the same order of specificity the selectors use.
    """
    if tags.get("office") == "government" or tags.get("government"):
        return "government"
    for key in ("amenity", "building"):
        value = tags.get(key)
        if value in GROUP_OF_KIND:
            return value
    return "public"


def osm_name(tags: dict) -> str | None:
    """The name OSM holds for this way, English where OSM has one."""
    return tags.get("name:en") or tags.get("name")


def is_public_estate(tags: dict, name: str) -> bool:
    """False for what a public selector matches but a ministry does not own."""
    if any(tag in tags for tag in EXCLUDE_TAGS):
        return False
    for key, bad in EXCLUDE_TAG_VALUES.items():
        if tags.get(key) in bad:
            return False
    lowered = name.lower()
    return not any(hint in lowered for hint in EXCLUDE_NAME_HINTS)


def usable_candidates(ways: list[dict], min_area: float, max_area: float) -> list[dict]:
    """Named public footprints in the size band, with everything the fixture needs."""
    usable = []
    for way in ways:
        tags = way.get("tags", {})
        name = osm_name(tags)
        if not name or not is_public_estate(tags, name):
            continue
        ring = to_ring(way)
        area = polygon_area_m2(ring)
        if not (min_area <= area <= max_area):
            continue
        lat, lon = centroid(ring)
        usable.append({
            "_ring": ring, "_area": area, "_lat": lat, "_lon": lon,
            "_osm_id": way.get("id"), "_tags": tags, "_kind": public_kind(tags),
            "_osm_name": name,
        })
    return usable


def choose(usable: list[dict], count: int) -> list[dict]:
    """Fill each group's share with its largest buildings, then top up by size.

    Largest-first inside a group because roof area is what a solar or an insulation
    option has to work with, and a portfolio of the smallest public buildings in
    Cairo would leave every intervention marginal.
    """
    by_group: dict[str, list[dict]] = {}
    for item in usable:
        by_group.setdefault(GROUP_OF_KIND.get(item["_kind"], "state"), []).append(item)
    for group in by_group.values():
        group.sort(key=lambda f: -f["_area"])

    total_quota = sum(GROUP_QUOTA.values())
    chosen: list[dict] = []
    for group, quota in GROUP_QUOTA.items():
        pool = by_group.get(group, [])
        share = round(quota * count / total_quota)
        chosen += spread(pool, min(share, len(pool)))

    if len(chosen) < count:
        taken = {id(item) for item in chosen}
        rest = sorted((i for i in usable if id(i) not in taken), key=lambda f: -f["_area"])
        chosen += spread(rest, count - len(chosen))
    return chosen[:count]


def build(count: int, min_area: float, max_area: float, seed: int, bbox) -> dict:
    ways = fetch_ways(bbox)
    if not ways:
        raise RuntimeError("Overpass returned no building ways for this bounding box")

    usable = usable_candidates(ways, min_area, max_area)
    if len(usable) < count:
        raise RuntimeError(
            f"only {len(usable)} NAMED public footprints between {min_area:.0f} and "
            f"{max_area:.0f} m2 in this bbox; widen --bbox or lower --min-area"
        )

    chosen = choose(usable, count)
    assign_districts(chosen)

    # Reproducibility is the requirement here, not unpredictability.
    rng = random.Random(seed)  # nosec B311

    features = []
    seen_names: dict[str, int] = {}
    ordered = sorted(chosen, key=lambda f: (f["_district"], -f["_area"]))
    for index, item in enumerate(ordered):
        properties = make_properties(
            rng, index, item["_district"], item["_area"], item["_lat"], item["_lon"],
            osm_id=item["_osm_id"],
            occupancy=KIND_OCCUPANCY.get(item["_kind"], "office"),
        )
        # Duplicates are numbered rather than merged: OSM legitimately gives several
        # ways in one complex the same name, and they are still separate buildings
        # with separate meters.
        label = item["_osm_name"]
        seen_names[label] = seen_names.get(label, 0) + 1
        if seen_names[label] > 1:
            label = f"{label} ({seen_names[label]})"
        properties["name"] = label
        properties["osm_kind"] = item["_kind"]
        properties["name_source"] = "openstreetmap"
        properties["district_source"] = "nominatim"
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
            "source": "OpenStreetMap via Overpass and Nominatim, ODbL",
            "bbox": list(bbox),
            "seed": seed,
            "count": count,
            "note": (
                "Footprints, locations, names and districts are real OSM data: every "
                "building is a way tagged as public estate, its name is the name OSM "
                "gives that way, and its district is what Nominatim resolves that "
                "building's own centroid to. No institution name is invented. "
                "Operational attributes (occupancy, HVAC age, insulation quality, "
                "consumption) are synthetic; a deployment would collect them through "
                "the profile-entry form."
            ),
            "selectors": PUBLIC_SELECTORS,
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
    # ensure_ascii=False: half these buildings are named in Arabic, and a fixture
    # full of \u0645\u062f\u0631 escapes cannot be read or reviewed.
    args.out.write_text(json.dumps(collection, indent=1, ensure_ascii=False),
                        encoding="utf-8")

    areas = [f["properties"]["roof_area_m2"] for f in collection["features"]]
    districts = {f["properties"]["district"] for f in collection["features"]}
    total_kwh = sum(f["properties"]["annual_kwh"] for f in collection["features"])
    print(f"wrote {args.out} - {len(areas)} named OSM public buildings "
          f"across {len(districts)} districts")
    print(f"roof area: {min(areas):,.0f} - {max(areas):,.0f} m2 "
          f"(median {sorted(areas)[len(areas) // 2]:,.0f})")
    print(f"portfolio consumption: {total_kwh / 1e6:.2f} GWh/yr")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
