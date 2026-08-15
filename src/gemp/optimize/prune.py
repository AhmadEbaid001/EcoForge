"""Remove options that can never be part of an optimal solution.

Within one building, option A dominates option B when A costs no more and delivers no
less. Any solution containing B can be rewritten to use A instead: the budget
constraint only loosens, the objective only improves, and the multiple-choice and
district-cap constraints are untouched because it is still one option at the same
building in the same district. So B is safe to discard before solving.

On the fixture portfolio this removes about two thirds of the candidate set. That
matters for Phase 3 rather than for correctness: the budget slider re-solves on every
drag, and a smaller model keeps the interaction responsive once HTTP and JSON
serialisation are added on top of the solve.

Dominance depends on which objective is active - an option can be dominated on carbon
but not on raw kWh - so pruning happens at solve time, not at expansion time.

The three solvers are all invariant under this transformation:

  * CP-SAT   - argued above.
  * greedy   - if A dominates B then A's density is at least B's, so greedy already
               reached A first and skipped B as a used building.
  * equal    - A costs no more, so A is affordable whenever B is, and scores at least
    split    as well; the per-building maximum is unchanged.

`test_pruning_does_not_change_any_solver_result` pins that invariance.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from gemp.domain.models import Candidate
from gemp.optimize.objective import objective_fn


def prune_dominated(candidates: Sequence[Candidate], objective: str) -> list[Candidate]:
    """Keep only the cost/benefit Pareto frontier at each building."""
    value_of = objective_fn(objective)

    by_building: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_building[candidate.building_id].append(candidate)

    kept: list[Candidate] = []
    for group in by_building.values():
        # Cheapest first; on a cost tie the higher-value option comes first and wins.
        ordered = sorted(group, key=lambda c: (c.cost_egp, -value_of(c)))
        best_so_far = float("-inf")
        for candidate in ordered:
            value = value_of(candidate)
            if value > best_so_far:
                kept.append(candidate)
                best_so_far = value

    return kept
