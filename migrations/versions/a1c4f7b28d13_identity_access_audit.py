"""identity, access and audit

Adds accounts, sessions and an audit trail. Purely additive: no existing table is
touched, so an upgrade cannot disturb a running portfolio, and the downgrade drops
only what this migration created.

Revision ID: a1c4f7b28d13
Revises: 0b65a9ee802e
Create Date: 2026-08-17 14:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1c4f7b28d13'
down_revision: str | None = '0b65a9ee802e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on PostgreSQL, plain JSON elsewhere - the same variant the application models
# use, so a schema built by Alembic matches one built by create_all.
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        'organization',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.create_table(
        'app_user',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('display_name', sa.String(length=128), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('must_change_password', sa.Boolean(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # Unique, and the application stores the lowercased form, so `Admin` and
        # `admin` cannot both exist.
        sa.UniqueConstraint('username'),
    )
    op.create_index('ix_app_user_organization_id', 'app_user', ['organization_id'])

    op.create_table(
        'user_session',
        # The PRIMARY KEY is the hash of the token, never the token. A dump of this
        # table cannot be replayed as a cookie.
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip', sa.String(length=45), nullable=False),
        sa.Column('user_agent', sa.String(length=256), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('token_hash'),
    )
    op.create_index('ix_user_session_user_id', 'user_session', ['user_id'])
    op.create_index('ix_user_session_expires_at', 'user_session', ['expires_at'])
    op.create_index('ix_user_session_last_seen_at', 'user_session', ['last_seen_at'])

    op.create_table(
        'audit_log',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        # Nullable on purpose: a failed login has no authenticated user, and those
        # are the rows most worth keeping.
        sa.Column('user_id', sa.String(length=36), nullable=True),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('target', sa.String(length=128), nullable=False),
        sa.Column('outcome', sa.String(length=16), nullable=False),
        sa.Column('ip', sa.String(length=45), nullable=False),
        sa.Column('detail', JSON_TYPE, nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_log_ts', 'audit_log', ['ts'])
    op.create_index('ix_audit_log_user_id', 'audit_log', ['user_id'])
    op.create_index('ix_audit_log_action', 'audit_log', ['action'])


def downgrade() -> None:
    op.drop_table('audit_log')
    op.drop_table('user_session')
    op.drop_table('app_user')
    op.drop_table('organization')
