"""The core deliverable: budget-constrained retrofit allocation.

Three solvers, one interface, one return type:

    cpsat        exact multiple-choice knapsack via OR-Tools CP-SAT
    greedy       benefit-density heuristic, the fast explainable fallback
    equal_split  budget divided evenly across the portfolio - the status-quo baseline

They share `Allocation` so the evaluation harness is a for-loop and the live baseline
comparison in the UI is a dropdown rather than a rebuild.
"""

from gemp.optimize.runner import OBJECTIVES, SOLVERS, solve

__all__ = ["OBJECTIVES", "SOLVERS", "solve"]
