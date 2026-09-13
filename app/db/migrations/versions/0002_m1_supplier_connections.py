"""M1: canonical supplier connection identity and its state machine.

Revision ID: 0002_m1_supplier_connections
Revises: 0001_m0_foundation
Create Date: 2026-09-13

Additive only. Secrets are never stored here (OS secret store + encrypted session file,
ADR-0007); this table holds identity, the explicit connection state and safe counters.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_m1_supplier_connections"
down_revision: str | None = "0001_m0_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = (
    "('DISCONNECTED', 'SESSION_CHECK', 'AUTHENTICATING', 'VERIFYING', 'READY', 'AUTH_EXPIRED', "
    "'REAUTHENTICATING', 'DEGRADED', 'PAUSED')"
)


def upgrade() -> None:
    op.create_table(
        "supplier_connections",
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.String(length=200), nullable=False),
        sa.Column("auth_required", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("auto_connect", sa.Boolean(), nullable=False),
        sa.Column("consecutive_auth_failures", sa.Integer(), nullable=False),
        sa.Column("real_login_attempts", sa.Integer(), nullable=False),
        sa.Column("session_reuse_count", sa.Integer(), nullable=False),
        sa.Column("reauth_count", sa.Integer(), nullable=False),
        sa.Column("last_login_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_class", sa.String(length=20), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"state IN {STATES}", name=op.f("ck_supplier_connections_state_valid")),
        sa.CheckConstraint(
            "state <> 'READY' OR last_verified_at IS NOT NULL",
            name=op.f("ck_supplier_connections_ready_is_proven"),
        ),
        sa.CheckConstraint(
            "consecutive_auth_failures >= 0 AND real_login_attempts >= 0 "
            "AND session_reuse_count >= 0 AND reauth_count >= 0",
            name=op.f("ck_supplier_connections_counters_non_negative"),
        ),
        sa.PrimaryKeyConstraint("connection_id", name=op.f("pk_supplier_connections")),
        sa.UniqueConstraint("supplier_key", name=op.f("uq_supplier_connections_supplier_key")),
    )


def downgrade() -> None:
    op.drop_table("supplier_connections")
