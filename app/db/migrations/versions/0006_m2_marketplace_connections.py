"""M2 PR-A: SmartStore connection generation high-water marks and the account binding.

Revision ID: 0006_m2_marketplace_connections
Revises: 0005_error_class_taxonomy
Create Date: 2026-09-15

Additive only. One row per connected marketplace account. The credential and session generation
high-water marks keep every committed generation new (AUTH.md §13). The account binding unit is
admitted complete or not at all (ACCOUNT_IDENTITY.md §5: ``binding_commit_not_proven ->
NOT_BOUND``). No client_id, secret or token is stored here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_m2_marketplace_connections"
down_revision: str | None = "0005_error_class_taxonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "marketplace_connections"

# Frozen with this revision; the integration tests compare it with the model's expression.
BINDING_IS_COMPLETE_OR_ABSENT = (
    "(provider_account_uid IS NULL AND provider_account_id IS NULL"
    " AND bound_credential_generation IS NULL AND bound_session_generation IS NULL"
    " AND bound_at IS NULL AND bound_by IS NULL)"
    " OR (provider_account_uid IS NOT NULL AND provider_account_uid <> ''"
    " AND bound_credential_generation IS NOT NULL AND bound_credential_generation >= 1"
    " AND bound_session_generation IS NOT NULL AND bound_session_generation >= 1"
    " AND bound_at IS NOT NULL AND bound_by IS NOT NULL AND bound_by <> '')"
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("credential_generation_hwm", sa.Integer(), nullable=False),
        sa.Column("session_generation_hwm", sa.Integer(), nullable=False),
        sa.Column("provider_account_uid", sa.String(length=100), nullable=True),
        sa.Column("provider_account_id", sa.String(length=100), nullable=True),
        sa.Column("bound_credential_generation", sa.Integer(), nullable=True),
        sa.Column("bound_session_generation", sa.Integer(), nullable=True),
        sa.Column("bound_at", sa.DateTime(), nullable=True),
        sa.Column("bound_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "credential_generation_hwm >= 0",
            name=op.f("ck_marketplace_connections_credential_hwm_non_negative"),
        ),
        sa.CheckConstraint(
            "session_generation_hwm >= 0",
            name=op.f("ck_marketplace_connections_session_hwm_non_negative"),
        ),
        sa.CheckConstraint(
            BINDING_IS_COMPLETE_OR_ABSENT,
            name=op.f("ck_marketplace_connections_binding_complete_or_absent"),
        ),
        sa.PrimaryKeyConstraint("marketplace_key", name=op.f("pk_marketplace_connections")),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
