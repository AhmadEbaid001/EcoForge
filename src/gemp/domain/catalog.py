"""Loading and validation of the two files Nada owns: catalog.csv and params.yaml.

This module is the enforcement point for the team contract. A malformed or
internally inconsistent edit fails loudly here rather than silently producing wrong
recommendations twenty minutes later in the optimizer.

Run it directly to check an edit:

    python -m gemp.domain.catalog --validate
    python -m gemp.domain.catalog --validate --strict   # also fails on uncited rows
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gemp.domain.models import Building, Intervention

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
CATALOG_PATH = DATA_DIR / "catalog.csv"
PARAMS_PATH = DATA_DIR / "params.yaml"

SHARE_TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# params.yaml
# ---------------------------------------------------------------------------


class SolarParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    usable_roof_frac: float = Field(gt=0, le=1)
    m2_per_kwp: float = Field(gt=0)
    yield_kwh_per_kwp: float = Field(gt=0)
    orientation_factor: dict[str, float]
    soiling_loss: float = Field(ge=0, lt=1)
    self_consumption_cap: float = Field(gt=0, le=1)
    solar_fixed_egp: float = Field(ge=0)
    solar_egp_per_kwp: float = Field(gt=0)


class BundlingParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_bundle_size: int = Field(ge=1, le=5)
    cap_total_saving_frac: float = Field(gt=0, le=1)


class ConstraintParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_funded_per_district: int | None = None


class GuardParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_building_saving_frac: float = Field(gt=0, le=1)


class Params(BaseModel):
    """Everything in params.yaml, validated."""

    model_config = ConfigDict(frozen=True)

    horizon_yr: int = Field(gt=0)
    grid_emission_factor: float = Field(gt=0)
    discount_rate: float = Field(ge=0, lt=1)
    electricity_tariff_egp_per_kwh: float = Field(gt=0)

    anomaly_k: float = Field(gt=0)
    anomaly_window_days: int = Field(gt=0)

    end_use_share: dict[str, dict[str, float]]
    adj: dict[str, dict[str, dict[str, float]]]

    solar: SolarParams
    bundling: BundlingParams
    constraints: ConstraintParams
    guards: GuardParams

    @model_validator(mode="after")
    def _shares_sum_to_one(self) -> Params:
        for occupancy, shares in self.end_use_share.items():
            total = sum(shares.values())
            if abs(total - 1.0) > SHARE_TOLERANCE:
                raise ValueError(
                    f"end_use_share[{occupancy}] sums to {total:.6f}, expected 1.0. "
                    f"End-use shares partition a building's consumption, so they must "
                    f"sum to exactly one."
                )
            for use, frac in shares.items():
                if not 0.0 <= frac <= 1.0:
                    raise ValueError(
                        f"end_use_share[{occupancy}][{use}] = {frac} is outside [0, 1]"
                    )
        return self

    def share(self, occupancy: str, end_use: str) -> float:
        """Fraction of total consumption attributable to `end_use` (F3)."""
        try:
            return self.end_use_share[occupancy][end_use]
        except KeyError as exc:
            raise KeyError(
                f"no end_use_share entry for occupancy={occupancy!r} end_use={end_use!r}; "
                f"add it to params.yaml"
            ) from exc

    def adj_multiplier(self, adj_key: str, building: Building) -> float:
        """Condition multiplier for this intervention at this building (F3).

        A blank `adj_key` means the intervention's saving fraction is unaffected by
        building condition, which returns 1.0.
        """
        if not adj_key:
            return 1.0
        rules = self.adj.get(adj_key)
        if rules is None:
            raise KeyError(
                f"catalog references adj_key={adj_key!r} but params.yaml has no such "
                f"block under `adj`"
            )

        multiplier = 1.0
        for field, buckets in rules.items():
            value = getattr(building, field, None)
            if value is None:
                raise KeyError(
                    f"adj[{adj_key}] keys on building field {field!r}, which does not "
                    f"exist on Building"
                )
            multiplier *= _match_bucket(buckets, value, context=f"adj[{adj_key}][{field}]")
        return multiplier


def _match_bucket(buckets: dict[str, float], value: Any, context: str) -> float:
    """Resolve a value against bucket keys such as `poor`, `<5`, `5-14`, `>=15`.

    Categorical values match by exact string. Numeric values match by range
    expression. An unmatched value is an error rather than a silent 1.0, because a
    silently ignored multiplier is exactly the kind of defect that produces a
    plausible-looking but wrong recommendation.
    """
    if isinstance(value, str):
        if value in buckets:
            return buckets[value]
        raise KeyError(f"{context}: no bucket matches value {value!r} (have {sorted(buckets)})")

    number = float(value)
    for key, factor in buckets.items():
        if _numeric_bucket_matches(key, number):
            return factor
    raise KeyError(f"{context}: no bucket matches value {number} (have {sorted(buckets)})")


def _numeric_bucket_matches(key: str, value: float) -> bool:
    key = key.strip()
    for op in (">=", "<=", ">", "<", "=="):
        if key.startswith(op):
            bound = float(key[len(op) :])
            return {
                ">=": value >= bound,
                "<=": value <= bound,
                ">": value > bound,
                "<": value < bound,
                "==": value == bound,
            }[op]
    if "-" in key:
        low, high = (float(part) for part in key.split("-", 1))
        return low <= value <= high
    return value == float(key)


# ---------------------------------------------------------------------------
# applies_if mini-DSL
# ---------------------------------------------------------------------------
#
# Deliberately not JSON. Nada edits catalog.csv in a spreadsheet, and JSON inside a
# CSV cell requires quote-doubling that Excel mangles. Grammar:
#
#     cond ( ";" cond )*
#     cond := field OP value | field "in" v1 "|" v2 ...
#     OP   := >= | <= | > | < | == | !=


def applies(intervention: Intervention, building: Building) -> bool:
    """True when this intervention is a legitimate option for this building."""
    rule = (intervention.applies_if or "").strip()
    if not rule:
        return True
    return all(
        _condition_holds(part.strip(), building, intervention.id)
        for part in rule.split(";")
        if part.strip()
    )


def _condition_holds(cond: str, building: Building, iv_id: str) -> bool:
    if " in " in cond:
        field, _, raw = cond.partition(" in ")
        allowed = {v.strip() for v in raw.split("|")}
        return str(_building_field(building, field.strip(), iv_id)) in allowed

    for op in (">=", "<=", "!=", "==", ">", "<"):
        if op in cond:
            field, _, raw = cond.partition(op)
            value = _building_field(building, field.strip(), iv_id)
            bound = float(raw.strip())
            return {
                ">=": value >= bound,
                "<=": value <= bound,
                ">": value > bound,
                "<": value < bound,
                "==": value == bound,
                "!=": value != bound,
            }[op]

    raise ValueError(f"{iv_id}: cannot parse applies_if condition {cond!r}")


def _building_field(building: Building, field: str, iv_id: str) -> Any:
    if not hasattr(building, field):
        raise ValueError(
            f"{iv_id}: applies_if references {field!r}, which is not a Building field "
            f"or property"
        )
    return getattr(building, field)


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------


def load_params(path: Path | None = None) -> Params:
    path = path or PARAMS_PATH
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Params.model_validate(raw)


def load_catalog(path: Path | None = None) -> list[Intervention]:
    path = path or CATALOG_PATH
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    if not rows:
        raise ValueError(f"{path} contains no intervention rows")

    catalog = [Intervention.model_validate(_clean_row(row)) for row in rows]

    ids = [iv.id for iv in catalog]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate intervention ids in catalog: {sorted(duplicates)}")

    return catalog


def _clean_row(row: dict[str, str | None]) -> dict[str, str]:
    return {k: (v or "").strip() for k, v in row.items() if k is not None}


def cross_validate(catalog: list[Intervention], params: Params) -> list[str]:
    """Checks that span both files. Returns a list of problems (empty when clean)."""
    problems: list[str] = []

    for iv in catalog:
        if iv.adj_key and iv.adj_key not in params.adj:
            problems.append(
                f"{iv.id}: adj_key={iv.adj_key!r} has no matching block in params.yaml `adj`"
            )
        if iv.is_generation:
            continue
        for occupancy, shares in params.end_use_share.items():
            if iv.end_use not in shares:
                problems.append(
                    f"{iv.id}: end_use={iv.end_use!r} missing from "
                    f"end_use_share[{occupancy}]"
                )

    for orientation in ("N", "NE", "E", "SE", "S", "SW", "W", "NW", "FLAT"):
        if orientation not in params.solar.orientation_factor:
            problems.append(f"params.solar.orientation_factor missing {orientation!r}")

    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the GEMP data contract.")
    parser.add_argument("--validate", action="store_true", help="run validation (default)")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail when any catalog row still lacks a citation (gates submission)",
    )
    args = parser.parse_args(argv)
    del args.validate  # validation is the only mode; the flag reads better in CI

    try:
        params = load_params()
        catalog = load_catalog()
    except Exception as exc:  # noqa: BLE001 - CLI boundary, report and exit
        print(f"FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    problems = cross_validate(catalog, params)
    for problem in problems:
        print(f"FAIL  {problem}", file=sys.stderr)

    uncited = [iv.id for iv in catalog if iv.needs_citation]
    if uncited:
        level = "FAIL" if args.strict else "WARN"
        stream = sys.stderr if args.strict else sys.stdout
        print(
            f"{level}  {len(uncited)} catalog row(s) still lack a source_ref: "
            f"{', '.join(uncited)}",
            file=stream,
        )
        if not args.strict:
            print(
                "      Every number the platform reports derives from these rows. "
                "Run with --strict before submission.",
                file=stream,
            )

    if problems or (uncited and args.strict):
        return 1

    print(
        f"OK    {len(catalog)} interventions, "
        f"{len(params.end_use_share)} occupancy types, horizon {params.horizon_yr}yr, "
        f"grid factor {params.grid_emission_factor} kgCO2e/kWh"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
