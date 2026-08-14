"""GEMP - Green Energy Monitoring Platform.

A budget-constrained retrofit decision-support system for public-sector building
portfolios. The core deliverable is the optimizer in `gemp.optimize`; everything
else exists to feed it (ingest, ml) or to display its output (api, web).

Module layout deliberately mirrors the logical architecture of the proposal's
Figure 1, but runs as a single process. See README.md.
"""

__version__ = "0.1.0"
