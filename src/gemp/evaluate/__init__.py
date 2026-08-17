"""The evaluation harness: every claim the paper makes, re-measured on demand.

    python -m gemp.evaluate                 # solver claims, no infrastructure needed
    python -m gemp.evaluate --with-db       # adds the forecasting and anomaly claims

The point is not to produce numbers. It is to produce numbers that can be checked
against what has already been written down. Every claim carries the gate it has to
clear, and a claim that stops clearing its gate fails the run - including the two
claims that are currently expected to fail, which are reported as known-open rather
than quietly excluded.
"""

from __future__ import annotations

from gemp.evaluate.claims import ClaimResult, Verdict, run_claims
from gemp.evaluate.sweep import Instance, SweepRow, run_sweep, write_sweep_csv

__all__ = [
    "ClaimResult",
    "Instance",
    "SweepRow",
    "Verdict",
    "run_claims",
    "run_sweep",
    "write_sweep_csv",
]
