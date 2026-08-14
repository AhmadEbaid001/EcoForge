"""Shared conversion between solver output and the common `Allocation` type."""

from __future__ import annotations

from collections.abc import Sequence

from gemp.domain.models import AllocationItem, Building, Candidate


def to_items(picks: Sequence[Candidate], buildings: Sequence[Building]) -> list[AllocationItem]:
    by_id = {b.id: b for b in buildings}
    return [
        AllocationItem(
            building_id=c.building_id,
            building_code=by_id[c.building_id].code,
            district=c.district,
            candidate_key=c.key,
            intervention_ids=c.intervention_ids,
            label=c.label,
            cost_egp=c.cost_egp,
            annual_kwh_saving=c.annual_kwh_saving,
            lifetime_benefit_kgco2e=c.lifetime_benefit_kgco2e,
            annual_egp_saving=c.annual_egp_saving,
        )
        for c in picks
    ]
