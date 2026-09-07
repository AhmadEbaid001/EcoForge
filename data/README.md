# `data/` — the interface between the two team members

Two files carry the entire contract between systems engineering and environmental
engineering. **Nada edits values and never code. Ahmed edits code and never values.**
Both are schema-validated on every push, so a malformed edit fails the build rather
than silently producing wrong recommendations.

**Exception, 7 September 2026.** Two days before submission the contract was crossed
once, deliberately: four values in `params.yaml` were changed from the code side
against primary sources — the grid emission factor, the electricity tariff, the solar
specific yield and the roof-orientation table. Each carries its source, its locator
and the date it was fetched, in a comment beside the value. Nothing was estimated:
every saving fraction and cost in `catalog.csv`, the `adj` condition multipliers, the
end-use shares and the two solar cost terms were left exactly as they were, and the
one value that could not be sourced — the time-of-use profile — was labelled a
placeholder rather than given a citation that does not support it.

| File | Owner | Contents |
|---|---|---|
| `catalog.csv` | Nada | One row per retrofit intervention: cost model, saving fraction, service life, embodied carbon, applicability rule, **source citation** |
| `params.yaml` | Nada | Grid factor, analysis horizon, discount rate, end-use shares, condition multipliers, solar physics, anomaly threshold |
| `buildings.geojson` | versioned | The 50-building portfolio every published number was measured on. Real OpenStreetMap footprints, from `scripts/fetch_osm_buildings.py`. **Do not regenerate it casually** — `scripts/gen_buildings.py` is an offline fallback that draws synthetic squares, and swapping one for the other changes the answer. |

Validate an edit before committing:

```bash
python -m gemp.domain.catalog --validate
```

## `catalog.csv` columns

| Column | Meaning |
|---|---|
| `id` | Stable identifier. Never reuse an id with different numbers — bump the version suffix instead. |
| `end_use` | Which end use this reduces: `hvac`, `lighting`, `plug`, or `generation`. Determines which slice of consumption the saving applies to. |
| `exclusive_group` | At most one intervention per group per building. Two HVAC plant replacements are not a bundle. |
| `adj_key` | Which block of `params.yaml → adj` applies. Blank means no condition adjustment. |
| `cost_type` | `fixed`, `per_m2_floor`, `per_m2_roof`, `per_m2_glazing`, or `solar`. `solar` uses the fixed-plus-per-kWp model in `params.yaml`. |
| `cost_value` | Multiplier for `cost_type`. Ignored when `cost_type=solar`. **EGP, never thousands of EGP.** |
| `saving_frac` | Fraction of the **target end use** removed — *not* a fraction of total consumption. Ignored for `generation`. |
| `service_life_yr` | Drives how many replacement cycles are charged over the 30-year horizon. |
| `embodied_type` / `embodied_value` | Embodied carbon model, kgCO2e. |
| `applies_if` | Semicolon-separated conditions, e.g. `hvac_age_yr>=8` or `insulation_quality in poor\|fair`. Blank means always applicable. |
| `source_ref` | **Required.** Rows still reading `TODO` fail `--strict` validation, which gates submission. |

## Why `saving_frac` is a fraction of the end use

The platform sees one number per building: total metered consumption. Turning that
into "an HVAC replacement here saves X kWh" needs two extra pieces of information,
which is what these two files supply:

```
E = annual_kwh  ×  end_use_share[occupancy][end_use]  ×  saving_frac  ×  adj(building)
```

If `saving_frac` were expressed against total consumption instead, the numbers would
stop being comparable across buildings with different occupancy patterns, and the
end-use share table would have nothing to do.

## Units

All money is **EGP**, as integers, everywhere — in this file, in the database, and in
the solver. The division by 1000 that produces "benefit per thousand EGP" happens only
at the display layer. Do not enter thousands here.

## Before submission

```bash
python -m gemp.domain.catalog --validate --strict
```

`--strict` fails on any row whose `source_ref` still says `TODO`. Every number the
platform reports derives from this file, so an uncited row is a question you cannot
answer during judging.
