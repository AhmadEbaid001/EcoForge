"""Core domain entities.

Pure Pydantic models with no persistence concerns. The SQLAlchemy tables added in
Phase 1 map onto these; these stay the currency of the domain and optimizer code.

Money is EGP as a plain number everywhere in this module. The division by 1000 that
produces "benefit per thousand EGP" happens only at the display layer (F12).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EndUse = Literal["hvac", "lighting", "plug", "other", "generation"]
CostType = Literal["fixed", "per_m2_floor", "per_m2_roof", "per_m2_glazing", "solar"]
EmbodiedType = Literal["fixed", "per_m2_floor", "per_m2_roof", "per_m2_glazing", "per_kwp"]
InsulationQuality = Literal["poor", "fair", "good"]
Occupancy = Literal["office", "school", "clinic", "admin_24x7"]
Orientation = Literal["N", "NE", "E", "SE", "S", "SW", "W", "NW", "FLAT"]

# Hours per day; the resolution of the TOU grid profile and of a building's
# measured load shape. Both are hour-of-day ordered, index 0 = local midnight.
HOURS_PER_DAY = 24

# The shape of a building with no measurement behind it: consumption evenly spread.
# Every fixture-built Building gets this by default, so CSV-fixture runs behave
# exactly as before A5 - a flat shape under a TOU profile yields precisely the
# flat grid factor.
FLAT_SHAPE: tuple[float, ...] = (1.0,) * HOURS_PER_DAY


class Building(BaseModel):
    """A single government building in the portfolio."""

    model_config = ConfigDict(frozen=True)

    id: str
    code: str
    name: str
    district: str

    lat: float
    lon: float

    floor_area_m2: float = Field(gt=0)
    roof_area_m2: float = Field(gt=0)
    glazing_area_m2: float = Field(ge=0)
    roof_orientation: Orientation

    hvac_type: str
    hvac_age_yr: int = Field(ge=0)
    insulation_quality: InsulationQuality
    occupancy_pattern: Occupancy

    # Whole-building annual consumption. In Phase 0 this comes from the fixture;
    # from Phase 2 it is the annualized forecast (F3), which is the only point at
    # which forecasting actually feeds the optimizer.
    annual_kwh: float = Field(gt=0)

    # A5: normalised 24-vector, mean kW by hour-of-day from the METERED series,
    # scaled to mean 1.0. It says WHEN this building consumes, which combined with
    # a time-of-use marginal grid factor says how much carbon each saved kWh is
    # worth here. Default flat: fixtures and pre-shape databases carry no shape,
    # and a flat shape reproduces the flat factor exactly.
    hourly_shape: tuple[float, ...] = FLAT_SHAPE

    @field_validator("hourly_shape")
    @classmethod
    def _shape_is_a_normalised_day(
        cls, value: tuple[float, ...]
    ) -> tuple[float, ...]:
        if len(value) != HOURS_PER_DAY:
            raise ValueError(
                f"hourly_shape must have {HOURS_PER_DAY} entries, got {len(value)}"
            )
        if any(v <= 0 for v in value):
            raise ValueError("hourly_shape entries must be > 0")
        mean = sum(value) / HOURS_PER_DAY
        return tuple(v / mean for v in value)

    @property
    def has_measured_shape(self) -> bool:
        return any(abs(v - 1.0) > 1e-9 for v in self.hourly_shape)

    @property
    def glazing_ratio(self) -> float:
        """Glazed area as a fraction of floor area. Used by `applies_if` rules."""
        return self.glazing_area_m2 / self.floor_area_m2

    @property
    def kwh_per_m2(self) -> float:
        """Energy use intensity. Handy for sanity-checking a fixture."""
        return self.annual_kwh / self.floor_area_m2


class Intervention(BaseModel):
    """One row of `data/catalog.csv`.

    `saving_frac` is a fraction of the TARGET END USE, not of total consumption.
    See data/README.md for why.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    end_use: EndUse
    exclusive_group: str
    adj_key: str = ""

    cost_type: CostType
    cost_value: float = Field(ge=0)

    saving_frac: float = Field(ge=0, le=1)
    service_life_yr: int = Field(gt=0)

    embodied_type: EmbodiedType
    embodied_value: float = Field(ge=0)

    applies_if: str = ""
    source_ref: str = Field(min_length=1)
    notes: str = ""

    @property
    def is_generation(self) -> bool:
        return self.end_use == "generation"

    @property
    def needs_citation(self) -> bool:
        """True while `source_ref` is still a placeholder (F14, gates --strict)."""
        return self.source_ref.strip().upper().startswith("TODO")

    @model_validator(mode="after")
    def _check_generation_consistency(self) -> Intervention:
        if self.is_generation and self.cost_type != "solar":
            raise ValueError(
                f"{self.id}: end_use=generation requires cost_type=solar "
                f"(got {self.cost_type!r}); generation is modelled separately from "
                f"end-use reduction"
            )
        if self.cost_type == "solar" and not self.is_generation:
            raise ValueError(f"{self.id}: cost_type=solar requires end_use=generation")
        if not self.is_generation and self.saving_frac == 0:
            raise ValueError(
                f"{self.id}: saving_frac=0 for a non-generation intervention means it "
                f"can never be selected; remove the row or give it a real fraction"
            )
        return self


class Candidate(BaseModel):
    """A costed, scored option for one building.

    An option is a single intervention or a pre-expanded compatible bundle (F4).
    Bundles are expanded into their own candidates so the solver stays a clean
    multiple-choice knapsack with no interaction terms.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    building_id: str
    district: str
    intervention_ids: tuple[str, ...]
    label: str

    cost_egp: float = Field(ge=0)
    annual_kwh_saving: float = Field(ge=0)
    lifetime_benefit_kgco2e: float
    # A5: the same benefit under the time-of-use marginal grid weighting. `None`
    # means "not differentiated" and resolves to the flat value below, so every
    # constructor written before A5 - including the test fixtures - keeps working
    # and, with no TOU profile loaded, the two numbers are equal by construction.
    lifetime_tou_benefit_kgco2e: float | None = None
    annual_egp_saving: float = Field(ge=0)

    @model_validator(mode="after")
    def _tou_defaults_to_flat(self) -> Candidate:
        if self.lifetime_tou_benefit_kgco2e is None:
            object.__setattr__(
                self, "lifetime_tou_benefit_kgco2e", self.lifetime_benefit_kgco2e
            )
        return self

    @property
    def score_per_kegp(self) -> float:
        """S = V / K, benefit density.

        F1: this is the RANKING key for the greedy heuristic and for display. It is
        deliberately not the ILP objective, because densities are not additive under
        a budget constraint.
        """
        if self.cost_egp <= 0:
            return 0.0
        return self.lifetime_benefit_kgco2e / (self.cost_egp / 1000.0)

    @property
    def is_bundle(self) -> bool:
        return len(self.intervention_ids) > 1


class AllocationItem(BaseModel):
    """One funded (building, option) pair in a solution."""

    building_id: str
    building_code: str
    district: str
    candidate_key: str
    intervention_ids: tuple[str, ...]
    label: str
    cost_egp: float
    annual_kwh_saving: float
    lifetime_benefit_kgco2e: float
    annual_egp_saving: float


class Allocation(BaseModel):
    """Result of one optimization run.

    All three solvers (CP-SAT, greedy, equal-split) return this same type, which is
    what makes the evaluation harness a for-loop and the live baseline comparison a
    dropdown rather than a rebuild (F8).
    """

    solver: str
    objective: str
    status: str
    budget_egp: float
    items: list[AllocationItem]
    solve_ms: float = 0.0

    @property
    def total_cost_egp(self) -> float:
        return sum(i.cost_egp for i in self.items)

    @property
    def total_kwh_saving(self) -> float:
        return sum(i.annual_kwh_saving for i in self.items)

    @property
    def total_benefit_kgco2e(self) -> float:
        return sum(i.lifetime_benefit_kgco2e for i in self.items)

    @property
    def total_egp_saving(self) -> float:
        return sum(i.annual_egp_saving for i in self.items)

    @property
    def buildings_funded(self) -> int:
        return len({i.building_id for i in self.items})

    @property
    def budget_used_frac(self) -> float:
        return self.total_cost_egp / self.budget_egp if self.budget_egp else 0.0

    @property
    def benefit_per_egp(self) -> float:
        """Headline metric: life-cycle benefit per EGP of BUDGET (not of spend).

        Denominator is the budget, not the amount spent, so that an allocation which
        leaves money unspent is penalised for it.
        """
        return self.total_benefit_kgco2e / self.budget_egp if self.budget_egp else 0.0

    @field_validator("items")
    @classmethod
    def _one_option_per_building(cls, items: list[AllocationItem]) -> list[AllocationItem]:
        seen = [i.building_id for i in items]
        if len(seen) != len(set(seen)):
            raise ValueError("allocation contains more than one option for the same building")
        return items
