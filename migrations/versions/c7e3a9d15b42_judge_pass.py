"""judge pass

Revision ID: c7e3a9d15b42
Revises: 2dddf9e1db39
Create Date: 2026-09-12 15:20:00.000000

The Judge Pass: a one-day viewer account issued at the booth. Additive only. One
nullable column on `app_user` - NULL is "never expires", which is every account
that exists today, so nothing about them changes - and two new tables. The
downgrade drops exactly what this created.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7e3a9d15b42"
down_revision: str | None = "2dddf9e1db39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON on SQLite - the same variant the models use.
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "judge_pass",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=64), nullable=False),
        sa.Column("lang", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mail_status", sa.String(length=16), nullable=False),
        sa.Column("mail_detail", sa.String(length=200), nullable=False),
        sa.Column("mail_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_judge_pass_created_at", "judge_pass", ["created_at"])

    op.create_table(
        "app_setting",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", JSON_TYPE, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_setting")
    op.drop_index("ix_judge_pass_created_at", table_name="judge_pass")
    op.drop_table("judge_pass")
    op.drop_column("app_user", "access_expires_at")
