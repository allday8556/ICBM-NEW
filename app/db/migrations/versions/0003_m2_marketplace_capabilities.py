"""M2 PR-B: marketplace capability truth — independent axes and scoped workflow overlays.

Revision ID: 0003_m2_marketplace_capabilities
Revises: 0002_m1_supplier_connections
Create Date: 2026-09-14

Additive only. One row per marketplace with an adopted capability contract (SmartStore in M2):
each axis is its own column, and each human-action overlay is a row keyed by its typed scope.
CHECK constraints repeat the single-table invariants of docs/platforms/smartstore/
CAPABILITY_MAPPING.md. No secret, token or provider identity is stored here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_m2_marketplace_capabilities"
down_revision: str | None = "0002_m1_supplier_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAPABILITIES = "marketplace_capabilities"
OVERLAYS = "marketplace_workflow_overlays"
ERROR_CLASSES = (
    "('TRANSIENT', 'RATE_LIMITED', 'AUTH', 'VALIDATION', 'POLICY_BLOCKED', 'NOT_FOUND', 'UNKNOWN')"
)


def upgrade() -> None:
    op.create_table(
        CAPABILITIES,
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("auth", sa.String(length=20), nullable=False),
        sa.Column("auth_verified_at", sa.DateTime(), nullable=True),
        sa.Column("write_scope_status", sa.String(length=20), nullable=False),
        sa.Column("evidence_strength", sa.String(length=20), nullable=True),
        sa.Column("write_status", sa.String(length=20), nullable=False),
        sa.Column("contract_freshness", sa.String(length=20), nullable=False),
        sa.Column("error_class", sa.String(length=20), nullable=True),
        sa.Column("remote_outcome", sa.String(length=20), nullable=True),
        sa.Column("session_generation_floor", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "auth IN ('READY', 'NOT_READY', 'AUTH_MISMATCH', 'NOT_BOUND')",
            name=op.f("ck_marketplace_capabilities_auth_valid"),
        ),
        sa.CheckConstraint(
            "auth <> 'READY' OR auth_verified_at IS NOT NULL",
            name=op.f("ck_marketplace_capabilities_ready_is_proven"),
        ),
        sa.CheckConstraint(
            "write_scope_status IN ('READY', 'MISSING', 'UNKNOWN')",
            name=op.f("ck_marketplace_capabilities_write_scope_valid"),
        ),
        sa.CheckConstraint(
            "evidence_strength IS NULL OR evidence_strength IN ('OPERATOR_ATTESTED', 'MACHINE_VERIFIED')",
            name=op.f("ck_marketplace_capabilities_strength_valid"),
        ),
        sa.CheckConstraint(
            "(write_scope_status = 'UNKNOWN') = (evidence_strength IS NULL)",
            name=op.f("ck_marketplace_capabilities_strength_matches_scope"),
        ),
        sa.CheckConstraint(
            "write_status IN ('UNVERIFIED', 'BLOCKED')",
            name=op.f("ck_marketplace_capabilities_m2_write_unproven"),
        ),
        sa.CheckConstraint(
            "write_scope_status <> 'MISSING' OR write_status = 'BLOCKED'",
            name=op.f("ck_marketplace_capabilities_missing_scope_blocks_write"),
        ),
        sa.CheckConstraint(
            "contract_freshness IN ('CURRENT', 'STALE', 'REVIEW_REQUIRED')",
            name=op.f("ck_marketplace_capabilities_freshness_valid"),
        ),
        sa.CheckConstraint(
            f"error_class IS NULL OR error_class IN {ERROR_CLASSES}",
            name=op.f("ck_marketplace_capabilities_error_class_valid"),
        ),
        sa.CheckConstraint(
            "remote_outcome IS NULL OR remote_outcome IN ('APPLIED_PROVEN', 'NOT_APPLIED_PROVEN', 'UNKNOWN')",
            name=op.f("ck_marketplace_capabilities_remote_outcome_valid"),
        ),
        sa.PrimaryKeyConstraint("marketplace_key", name=op.f("pk_marketplace_capabilities")),
    )
    op.create_table(
        OVERLAYS,
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("workflow_scope", sa.String(length=30), nullable=False),
        sa.Column("workflow_state", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=True),
        sa.Column("session_generation", sa.Integer(), nullable=True),
        sa.Column("entered_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "workflow_scope IN ('AUTHENTICATION', 'PRODUCT_REGISTRATION')",
            name=op.f("ck_marketplace_workflow_overlays_scope_valid"),
        ),
        sa.CheckConstraint(
            "workflow_state IN ('PAUSED', 'REVIEW_REQUIRED')",
            name=op.f("ck_marketplace_workflow_overlays_state_valid"),
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code IN ('AUTH_RETRY_LIMIT', 'APPLICATION_REAUTH_REQUIRED', 'SCOPE_INSUFFICIENT', 'ACCOUNT_RESTRICTED')",
            name=op.f("ck_marketplace_workflow_overlays_reason_valid"),
        ),
        sa.CheckConstraint(
            "(workflow_state = 'PAUSED') = (reason_code IS NOT NULL)",
            name=op.f("ck_marketplace_workflow_overlays_paused_has_reason"),
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code = 'ACCOUNT_RESTRICTED'"
            " OR (reason_code = 'SCOPE_INSUFFICIENT' AND workflow_scope = 'PRODUCT_REGISTRATION')"
            " OR (reason_code IN ('AUTH_RETRY_LIMIT', 'APPLICATION_REAUTH_REQUIRED')"
            " AND workflow_scope = 'AUTHENTICATION')",
            name=op.f("ck_marketplace_workflow_overlays_reason_fits_scope"),
        ),
        sa.CheckConstraint(
            "(COALESCE(reason_code, '') = 'APPLICATION_REAUTH_REQUIRED')"
            " = (session_generation IS NOT NULL)",
            name=op.f("ck_marketplace_workflow_overlays_reauth_records_generation"),
        ),
        sa.ForeignKeyConstraint(
            ["marketplace_key"],
            [f"{CAPABILITIES}.marketplace_key"],
            name=op.f("fk_marketplace_workflow_overlays_marketplace_key_marketplace_capabilities"),
        ),
        sa.PrimaryKeyConstraint(
            "marketplace_key", "workflow_scope", name=op.f("pk_marketplace_workflow_overlays")
        ),
    )


def downgrade() -> None:
    op.drop_table(OVERLAYS)
    op.drop_table(CAPABILITIES)
