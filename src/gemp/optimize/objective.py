"""What "best" means. Shared by all three solvers.

The platform deliberately exposes more than one objective. A climate target and a
budget holder do not always rank the same intervention first, and hiding that
behind a single number would be a worse decision-support tool, not a simpler one.
Switching objectives live is also the clearest demonstration that the platform
changes the decision rather than merely displaying it.
"""

from __future__ import annotations

from collections.abc import Callable

from gemp.domain.models import Candidate

ObjectiveFn = Callable[[Candidate], float]

OBJECTIVES: dict[str, ObjectiveFn] = {
    # Net life-cycle carbon benefit over the common horizon at the FLAT grid
    # factor, kgCO2e. Default. Kept exactly as A5 found it so every number the
    # paper measured before the TOU factor stays reproducible.
    "lca_carbon": lambda c: c.lifetime_benefit_kgco2e,
    # A5: the same benefit with the grid factor weighted by the building's own
    # MEASURED hourly shape against the TOU marginal profile - a kWh saved before
    # the evening peak is not worth what one saved during it. Equal to lca_carbon
    # when no profile is loaded, which is what makes old-vs-new measurable rather
    # than a silent redefinition.
    "tou_carbon": lambda c: c.lifetime_tou_benefit_kgco2e,
    # First-year energy saved, kWh. What a conventional monitoring tool would rank on.
    "raw_kwh": lambda c: c.annual_kwh_saving,
    # First-year money saved at the current tariff, EGP. What a budget holder optimises.
    "egp_saved": lambda c: c.annual_egp_saving,
}

OBJECTIVE_UNITS: dict[str, str] = {
    "lca_carbon": "kgCO2e",
    "tou_carbon": "kgCO2e",
    "raw_kwh": "kWh/yr",
    "egp_saved": "EGP/yr",
}


def objective_fn(name: str) -> ObjectiveFn:
    try:
        return OBJECTIVES[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown objective {name!r}; choose from {sorted(OBJECTIVES)}"
        ) from exc


def density(candidate: Candidate, name: str) -> float:
    """Objective value per EGP of capital.

    F1: this ratio is the ranking key for the greedy heuristic and for display. It is
    NOT the integer programme's objective, because densities are not additive under a
    budget constraint - summing them yields a quantity with no unit and a degenerate
    optimum.
    """
    if candidate.cost_egp <= 0:
        return 0.0
    return objective_fn(name)(candidate) / candidate.cost_egp
