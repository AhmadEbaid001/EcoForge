"""Bake a satellite basemap into the repository, once, by hand.

The map draws real footprints on an empty field. That is honest and it is hard to
read: a reviewer looking at fifty shapes over Greater Cairo cannot tell the desert
from the delta, or a ministry compound from a school in a housing block. Imagery
underneath answers "where is this" in the way everyone already expects a map to.

WHY THIS IS A DOWNLOAD AND NOT A TILE LAYER
-------------------------------------------
Three constraints, and they all point the same way.

    F13         the demonstration has to survive an unplugged network cable. A tile
                layer is blank without one, and the map is the demonstration.
    the CSP     `default-src 'self'` - a request to tile.googleapis.com is not
                discouraged here, it is refused by the browser.
    the licence Google, Bing and Esri all forbid caching or redistributing their
                imagery outside their own services. There is no version of this
                that ships their pixels in a git repository.

So the imagery is fetched once, by this script, and committed. It is refreshed by
running it again - Sentinel-2 cloudless is rebuilt yearly, and a city changes
slowly enough that "manually, every so often" is the correct cadence rather than a
compromise.

THE SOURCE
----------
Sentinel-2 cloudless by EOX IT Services GmbH, from Copernicus Sentinel data, under
CC BY 4.0. It is the highest-resolution satellite basemap that may legally be
cached and redistributed with attribution: about 10 m per pixel, which resolves a
city block, a motorway interchange and the Nile, and does not resolve one school.
That is the right trade here - the buildings are drawn as vectors on top and stay
sharp at every zoom; the imagery is context beneath them, and it is context that
cannot be faked.

The attribution is not optional and the map renders it. See `web/data/basemap.json`.

    python scripts/fetch_basemap.py                    # the portfolio's own extent
    python scripts/fetch_basemap.py --zoom 13          # smaller file, softer image
    python scripts/fetch_basemap.py --bbox 29.9 31.1 30.2 31.9

Network use is a read-only GET per tile against a public WMTS endpoint, run once by
hand. Nothing fetches at runtime, ever.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO = ROOT / "data" / "buildings.geojson"
OUT_IMAGE = ROOT / "web" / "data" / "basemap.jpg"
OUT_MANIFEST = ROOT / "web" / "data" / "basemap.json"

TILES = ("https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857"
         "/default/g/{z}/{y}/{x}.jpg")
SOURCE = "Sentinel-2 cloudless 2020 by EOX IT Services GmbH"
LICENCE = "CC BY 4.0"
ATTRIBUTION = "Sentinel-2 cloudless 2020 by EOX IT Services GmbH (Copernicus data)"
USER_AGENT = "gemp-robodam2026/0.1 (academic project)"

TILE_PX = 256

# Zoom 14 is about 9.5 m per pixel at this latitude, which is Sentinel-2's own
# resolution: asking for more returns an upsampled blur and a file three times the
# size. Zoom 13 halves the detail and quarters the bytes.
DEFAULT_ZOOM = 14

# Room around the portfolio, and more than "a little": imagery that stops at the
# outermost building reads as a photograph pasted onto the map rather than as the
# ground under it. 0.12 degrees is about 13 km, which carries the imagery to both
# edges of the panel at the opening zoom and still has somewhere to go when a
# reader pans.
#
# It does NOT fill the panel vertically, and that is a deliberate stop. The map
# fits the portfolio's width, so filling the height as well would mean roughly
# three times the tiles and about 15 MB in the repository - for desert north and
# south of the fifty buildings, where there is nothing to see and nothing to
# decide.
PAD_DEGREES = 0.12

# Enough that a strip's own linear approximation of the Mercator curve is under a
# pixel; see `to_plate_carree`.
STRIPS = 96

ATTEMPTS = 4
BACKOFF_S = 5


def portfolio_bbox(path: Path, pad: float = PAD_DEGREES) -> tuple[float, float, float, float]:
    """The extent the fixture actually occupies, padded. (south, west, north, east)"""
    data = json.loads(path.read_text(encoding="utf-8"))
    lats = [f["properties"]["lat"] for f in data["features"]]
    lons = [f["properties"]["lon"] for f in data["features"]]
    return (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)


def lon_to_tile_x(lon: float, zoom: int) -> float:
    return (lon + 180.0) / 360.0 * (2 ** zoom)


def lat_to_tile_y(lat: float, zoom: int) -> float:
    phi = math.radians(lat)
    return (1.0 - math.asinh(math.tan(phi)) / math.pi) / 2.0 * (2 ** zoom)


def tile_y_to_lat(y: float, zoom: int) -> float:
    n = math.pi * (1.0 - 2.0 * y / (2 ** zoom))
    return math.degrees(math.atan(math.sinh(n)))


def fetch_tile(zoom: int, x: int, y: int) -> Image.Image:
    url = TILES.format(z=zoom, x=x, y=y)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(ATTEMPTS):
        try:
            # TILES is the https constant above; z/x/y are integers.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(request, timeout=45) as response:  # nosec B310
                return Image.open(io.BytesIO(response.read())).convert("RGB")
        except urllib.error.HTTPError as exc:
            # 404 is a tile the service does not hold - ocean, or outside coverage.
            # A blank square is the correct answer; retrying will not conjure one.
            if exc.code == 404:
                return Image.new("RGB", (TILE_PX, TILE_PX), (12, 16, 22))
            if attempt == ATTEMPTS - 1:
                raise
            print(f"  {exc.code} on {zoom}/{x}/{y}, retrying", file=sys.stderr)
            time.sleep(BACKOFF_S)
        except OSError:
            if attempt == ATTEMPTS - 1:
                raise
            time.sleep(BACKOFF_S)
    raise RuntimeError("unreachable")


def mosaic(bbox: tuple[float, float, float, float], zoom: int) -> tuple[Image.Image, dict]:
    """Every tile covering the bbox, stitched. Still in Web Mercator."""
    south, west, north, east = bbox

    x0 = math.floor(lon_to_tile_x(west, zoom))
    x1 = math.ceil(lon_to_tile_x(east, zoom))
    y0 = math.floor(lat_to_tile_y(north, zoom))   # north is the SMALLER tile row
    y1 = math.ceil(lat_to_tile_y(south, zoom))

    across, down = x1 - x0, y1 - y0
    total = across * down
    print(f"  {total} tiles at zoom {zoom} ({across} x {down}), "
          f"{across * TILE_PX} x {down * TILE_PX} px")

    canvas = Image.new("RGB", (across * TILE_PX, down * TILE_PX))
    done = 0
    for ty in range(y0, y1):
        for tx in range(x0, x1):
            canvas.paste(fetch_tile(zoom, tx, ty),
                         ((tx - x0) * TILE_PX, (ty - y0) * TILE_PX))
            done += 1
            if done % 50 == 0 or done == total:
                print(f"  fetched {done}/{total}")

    # What the stitched image actually covers, which is tile boundaries rather than
    # the bbox that was asked for.
    return canvas, {
        "west": (x0 / (2 ** zoom)) * 360.0 - 180.0,
        "east": (x1 / (2 ** zoom)) * 360.0 - 180.0,
        "north": tile_y_to_lat(y0, zoom),
        "south": tile_y_to_lat(y1, zoom),
    }


def to_plate_carree(image: Image.Image, cover: dict) -> Image.Image:
    """Resample from Web Mercator to linear latitude, which is what the map draws.

    `map.js` projects with `y = (maxLat - lat) * scale` - latitude straight onto
    pixels. Mercator stretches towards the poles, so pasting the mosaic behind that
    projection puts the imagery a few pixels north of the footprints drawn on it,
    and the error grows down the image. Over this extent it is about 25 m, which is
    half a building: small enough to look like nothing is wrong, big enough that
    every footprint sits slightly off its own roof.

    Done as horizontal strips rather than per row: within a strip the curve is
    linear to well under a pixel, and 96 resizes take a moment where 4,608 do not.
    """
    width, height = image.size
    north, south = cover["north"], cover["south"]
    span = north - south

    out = Image.new("RGB", (width, height))
    for i in range(STRIPS):
        # The strip's latitude range in the OUTPUT, which is linear in latitude.
        top_lat = north - span * (i / STRIPS)
        bottom_lat = north - span * ((i + 1) / STRIPS)

        # Where those latitudes sit in the SOURCE, which is not.
        src_top = mercator_pixel(top_lat, north, south, height)
        src_bottom = mercator_pixel(bottom_lat, north, south, height)

        strip = image.crop((0, int(src_top), width, max(int(src_bottom), int(src_top) + 1)))
        target_h = round(height / STRIPS)
        out.paste(
            strip.resize((width, max(target_h, 1)), Image.LANCZOS),
            (0, round(i * height / STRIPS)),
        )
    return out


def mercator_pixel(lat: float, north: float, south: float, height: int) -> float:
    """Row in a Mercator image spanning north..south that holds this latitude."""
    def project(value: float) -> float:
        return math.asinh(math.tan(math.radians(value)))

    top, bottom = project(north), project(south)
    return (top - project(lat)) / (top - bottom) * height


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bbox", type=float, nargs=4, default=None,
                        metavar=("SOUTH", "WEST", "NORTH", "EAST"),
                        help="default: the portfolio's own extent, padded")
    parser.add_argument("--zoom", type=int, default=DEFAULT_ZOOM)
    parser.add_argument("--quality", type=int, default=82)
    parser.add_argument("--max-width", type=int, default=8192,
                        help="downscale beyond this; keeps the repository sane")
    parser.add_argument("--out", type=Path, default=OUT_IMAGE)
    parser.add_argument("--manifest", type=Path, default=OUT_MANIFEST)
    args = parser.parse_args(argv)

    bbox = tuple(args.bbox) if args.bbox else portfolio_bbox(PORTFOLIO)
    print(f"  extent {bbox[0]:.4f},{bbox[1]:.4f} to {bbox[2]:.4f},{bbox[3]:.4f}")

    try:
        stitched, cover = mosaic(bbox, args.zoom)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
        print("      the map falls back to drawing without imagery", file=sys.stderr)
        return 1

    print("  reprojecting to the map's own projection")
    flat = to_plate_carree(stitched, cover)

    if flat.width > args.max_width:
        height = round(flat.height * args.max_width / flat.width)
        print(f"  downscaling {flat.width}x{flat.height} to {args.max_width}x{height}")
        flat = flat.resize((args.max_width, height), Image.LANCZOS)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    flat.save(args.out, "JPEG", quality=args.quality, optimize=True, progressive=True)

    args.manifest.write_text(json.dumps({
        "image": args.out.name,
        "bbox": {k: round(v, 6) for k, v in cover.items()},
        "zoom": args.zoom,
        "pixels": {"width": flat.width, "height": flat.height},
        "source": SOURCE,
        "licence": LICENCE,
        "attribution": ATTRIBUTION,
        "fetched_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "note": (
            "Baked into the repository on purpose: the demonstration must run with "
            "the network unplugged (F13) and the Content-Security-Policy allows no "
            "off-origin request. Refresh by re-running scripts/fetch_basemap.py."
        ),
    }, indent=1) + "\n", encoding="utf-8")

    size_mb = args.out.stat().st_size / 1024 / 1024
    print(f"\n  wrote {args.out} - {flat.width} x {flat.height}, {size_mb:.1f} MB")
    print(f"  wrote {args.manifest}")
    print(f"  {ATTRIBUTION}, {LICENCE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
