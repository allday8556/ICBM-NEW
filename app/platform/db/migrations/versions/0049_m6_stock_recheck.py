"""M6-B supplier stock recheck: one row per requested re-collection and what it judged (ADR-0023 §4).

Revision ID: 0049_m6_stock_recheck
Revises: 0048_m6_listing_sync
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_m6_stock_recheck"
down_revision: str | None = "0048_m6_listing_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECHECKS = "operate_stock_rechecks"


def upgrade() -> None:
    op.create_table(
        RECHECKS,
        sa.Column("recheck_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("source_product_id", sa.String(length=200), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("collection_run_id", sa.String(length=36), nullable=True),
        sa.Column("revision_id", sa.String(length=36), nullable=True),
        sa.Column("availability", sa.String(length=20), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("trigger IN ('AUTO', 'OPERATOR')", name="trigger_valid"),
        sa.CheckConstraint("state IN ('SUBMITTING', 'REQUESTED', 'FINISHED')", name="state_valid"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('RECORDED', 'NO_REVISION', 'FAILED', 'REFUSED')",
            name="outcome_valid",
        ),
        sa.CheckConstraint(
            "(state = 'FINISHED') = (finished_at IS NOT NULL AND outcome IS NOT NULL)",
            name="finished_has_outcome",
        ),
        sa.CheckConstraint(
            "state <> 'REQUESTED' OR collection_run_id IS NOT NULL",
            name="requested_has_run",
        ),
        sa.CheckConstraint(
            "availability IS NULL OR availability IN ('ON_SALE', 'SOLD_OUT', 'REVIEW_REQUIRED')",
            name="availability_valid",
        ),
        sa.CheckConstraint(
            "(outcome = 'RECORDED') = (revision_id IS NOT NULL)", name="recorded_has_revision"
        ),
        sa.CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
        sa.PrimaryKeyConstraint("recheck_id"),
    )
    op.create_index(
        "ix_operate_stock_rechecks_source",
        RECHECKS,
        ["supplier_key", "source_product_id", "requested_at"],
        unique=False,
    )
    op.create_index(
        "ux_operate_stock_rechecks_one_pending",
        RECHECKS,
        ["supplier_key", "source_product_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('SUBMITTING', 'REQUESTED')"),
    )


def downgrade() -> None:
    op.drop_index("ux_operate_stock_rechecks_one_pending", table_name=RECHECKS)
    op.drop_index("ix_operate_stock_rechecks_source", table_name=RECHECKS)
    op.drop_table(RECHECKS)
