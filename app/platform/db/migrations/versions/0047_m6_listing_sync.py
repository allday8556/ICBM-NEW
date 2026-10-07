"""M6-A listing-state sync: the sync runs and their append-only listing observations (ADR-0023 §3).

Revision ID: 0047_m6_listing_sync
Revises: 0046_sequential_bulk_registration
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_m6_listing_sync"
down_revision: str | None = "0046_sequential_bulk_registration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "operate_listing_sync_runs"
OBSERVATIONS = "operate_listing_observations"


def upgrade() -> None:
    op.create_table(
        RUNS,
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("targets", sa.Integer(), nullable=False),
        sa.Column("observed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("trigger IN ('AUTO', 'OPERATOR')", name="trigger_valid"),
        sa.CheckConstraint("state IN ('RUNNING', 'FINISHED')", name="state_valid"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('COMPLETED', 'COMPLETED_WITH_FAILURES',"
            " 'SESSION_UNAVAILABLE', 'RATE_LIMITED', 'INTERRUPTED')",
            name="outcome_valid",
        ),
        sa.CheckConstraint(
            "(state = 'FINISHED') = (finished_at IS NOT NULL AND outcome IS NOT NULL)",
            name="finished_has_outcome",
        ),
        sa.CheckConstraint(
            "targets >= 0 AND observed >= 0 AND failed >= 0 AND observed + failed <= targets",
            name="counts_bounded",
        ),
        sa.CheckConstraint("correlation_id <> ''", name="correlation_present"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ux_operate_listing_sync_runs_one_running",
        RUNS,
        ["state"],
        unique=True,
        sqlite_where=sa.text("state = 'RUNNING'"),
    )
    op.create_index("ix_operate_listing_sync_runs_started", RUNS, ["started_at"], unique=False)
    op.create_table(
        OBSERVATIONS,
        sa.Column("observation_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("registration_id", sa.String(length=36), nullable=False),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("sale_status", sa.String(length=32), nullable=True),
        sa.Column("display_status", sa.String(length=32), nullable=True),
        sa.Column("sale_price", sa.Integer(), nullable=True),
        sa.Column("stock_quantity", sa.Integer(), nullable=True),
        sa.Column("seller_code_matches", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "result IN ('OBSERVED', 'NOT_FOUND', 'READ_FAILED')", name="result_valid"
        ),
        sa.CheckConstraint(
            "result = 'OBSERVED' OR (sale_status IS NULL AND display_status IS NULL"
            " AND sale_price IS NULL AND stock_quantity IS NULL AND seller_code_matches IS NULL)",
            name="only_observed_carries_fields",
        ),
        sa.CheckConstraint(
            "(result = 'READ_FAILED') = (error_code IS NOT NULL)", name="failure_has_code"
        ),
        sa.CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
        sa.ForeignKeyConstraint(["run_id"], [f"{RUNS}.run_id"]),
        sa.ForeignKeyConstraint(["registration_id"], ["marketplace_registrations.registration_id"]),
        sa.PrimaryKeyConstraint("observation_id"),
    )
    op.create_index(
        "ix_operate_listing_observations_registration",
        OBSERVATIONS,
        ["registration_id", "observed_at"],
        unique=False,
    )
    # Observations are append-only evidence: never rewritten, never deleted.
    op.execute(
        f"""
        CREATE TRIGGER {OBSERVATIONS}_no_update
        BEFORE UPDATE ON {OBSERVATIONS}
        BEGIN SELECT RAISE(ABORT, 'listing observations are append-only'); END;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {OBSERVATIONS}_no_delete
        BEFORE DELETE ON {OBSERVATIONS}
        BEGIN SELECT RAISE(ABORT, 'listing observations are append-only'); END;
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {OBSERVATIONS}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {OBSERVATIONS}_no_update")
    op.drop_index("ix_operate_listing_observations_registration", table_name=OBSERVATIONS)
    op.drop_table(OBSERVATIONS)
    op.drop_index("ix_operate_listing_sync_runs_started", table_name=RUNS)
    op.drop_index("ux_operate_listing_sync_runs_one_running", table_name=RUNS)
    op.drop_table(RUNS)
