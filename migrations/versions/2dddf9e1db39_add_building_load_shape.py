"""add building load shape

Revision ID: 2dddf9e1db39
Revises: a1c4f7b28d13
Create Date: 2026-08-26 11:09:22.254264

A5: the TOU carbon weighting consumes each building's measured hour-of-day load
shape, derived by the nightly refit from the same series that produces
`annual_kwh`. The columns start NULL - the domain reads a missing shape as flat,
which reproduces the pre-A5 flat grid factor exactly - so no backfill is needed
and existing deployments keep costing identically until the next refit.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2dddf9e1db39"
down_revision: str | None = "a1c4f7b28d13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON on SQLite - the same variant the models use,
# so autogenerate never proposes to "fix" it either way.
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("building", sa.Column("load_shape", JSON_TYPE, nullable=True))
    op.add_column(
        "building", sa.Column("load_shape_source", sa.String(length=16), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("building", "load_shape_source")
    op.drop_column("building", "load_shape")
