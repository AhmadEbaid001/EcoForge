# Findings

Two results that change what the paper can claim. Both were in the README until it grew
long enough that they buried the rest of it; neither has been shortened in the move,
because the reasoning is the point.

---

## Open finding: the LCA layer currently changes no decisions

At every budget from 2 M to 40 M EGP, the `lca_carbon` and `raw_kwh` objectives fund
**the same buildings with the same interventions**. Embodied carbon is a median 8 % of
gross avoided emissions across the candidate set, and the options that actually get
funded are the ones where it is smallest. At an Egyptian grid factor of
0.45 kgCO₂e/kWh, embodied carbon is simply too small to reverse a retrofit ranking.

The LCA layer does earn its place in one respect: it correctly refuses to fund at least
one glazing option whose manufacture emits more than it avoids over thirty years, which
a raw-kWh ranking would treat as a benefit.

This is pinned by `test_lca_adjustment_barely_changes_the_funded_set`. If a time-of-use
marginal emission factor is added — making an HVAC kWh saved at a summer afternoon peak
worth more carbon than a lighting kWh saved in the evening — that test should start
failing, and the failure is the signal that the LCA layer has begun to matter.

Stated more usefully as what the difference is worth: choosing funding by raw kWh
instead of life-cycle carbon costs at most **0.02 %** of the carbon a carbon-optimal
allocation would deliver, across 20 budgets.

---

## Closed finding: the anomaly precision target, and what it took to reach it

Episode-level detection reads **precision 0.83 at recall 0.88** (k = 5), against gates
of 0.6 and 0.8 in the technical review. Both are met. The route there is worth more
than the number, because for most of the project this section said the target was
unreachable, and the reasoning that led to that conclusion was half sound.

**The sound half.** `k` moves a point along a single precision/recall curve. To find out
whether the curve itself could be moved, 64 combinations of `k`, minimum episode
duration and minimum peak z were measured against injected ground truth. Requiring
longer episodes buys precision at about the same exchange rate as raising `k`. Requiring
a higher peak z makes precision *worse*, moving it from 0.607 to 0.535, which is
informative rather than merely disappointing: a frozen meter barely deviates from its
expectation at all, while the largest residuals are legitimate load that the forecaster
failed to anticipate. Residual magnitude does not separate real faults from forecast
misses. All of that still holds.

**The unsound half** was treating 0.55 as a property of the problem. It was a property
of the forecaster, and the forecaster had one systematic failure doing most of the
damage. Egyptian building load STEPS at midnight — into the Friday–Saturday weekend,
into a public holiday, out of one — and every lag feature says the building was busy an
hour ago. Relative residual dispersion measured 0.125 at hour 00 against 0.06 for the
rest of the day, and 640 of 1,379 false alarms began at hour 00, on Fridays and public
holidays. The detector scales residuals against a window pooled across all hours, so a
systematically worse hour breaches the threshold on ordinary days.

The fix is one idea: predict the **ratio** to a causal hour-of-week profile rather than
the load itself. A tree adds leaf values, so in kW a holiday is a per-building, per-hour
constant learned from a handful of examples; in ratio space it is "0.3×", one split that
holds everywhere. Held-out MAPE moved 3.83 % → 3.24 %, and episode precision at recall
0.88 moved 0.55 → 0.83. The precision gain is far larger than the accuracy gain because
what it removed was not noise but a systematic error at one hour of the day.

Two measurement defects were fixed in the same pass, and the second was the larger.
Forecasts are now floored at zero — the model had been predicting down to −1.32 kW in
the overnight trough, and the detector divides by the expected value, so an ordinary
1 kW reading became a robust z of 15,319. And ground truth from a simulator run that
rewound its data clock is now discarded: such a run republishes timestamps that already
exist, every reading is dropped as a duplicate, and the faults it records happened to
nothing. That had 1,762 of 2,212 recorded events describing data that was never stored,
and it read as recall 0.375 for a detector whose recall is 0.84.
