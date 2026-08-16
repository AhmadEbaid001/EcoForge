"""Portfolio loading.

Phase 0 reads the building portfolio from a GeoJSON fixture. From Phase 1 the same
`Building` objects come from PostgreSQL instead; nothing downstream changes, which
is the point of keeping the domain layer free of storage concerns.
"""

from __future__ import annotations

import json
from pathlib import Path

from gemp.domain.models import Building
from gemp.paths import data_dir


def buildings_path() -> Path:
    return data_dir() / "buildings.geojson"


def load_buildings(path: Path | None = None) -> list[Building]:
    """Read the portfolio fixture into domain objects."""
    path = path or buildings_path()
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. The portfolio fixture is generated, not versioned - "
            f"run: python scripts/gen_buildings.py"
        )

    collection = json.loads(path.read_text(encoding="utf-8"))
    return [Building.model_validate(f["properties"]) for f in collection["features"]]


def load_geojson(path: Path | None = None) -> dict:
    """Raw GeoJSON, for the map layer which needs the footprint geometry."""
    path = path or buildings_path()
    return json.loads(path.read_text(encoding="utf-8"))
