"""Persist strictly sequential bulk-registration runs.

Revision ID: 0042_sequential_bulk_registration
Revises: 0041_category_catalog
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_sequential_bulk_registration"
down_revision: str | None = "0041_category_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "registration_bulk_runs"
ITEMS = "registration_bulk_items"


def upgrade() -> None:
    op.create_table(
        RUNS,
        sa.Column("bulk_run_id", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="request_fingerprint_hex",
        ),
        sa.CheckConstraint("total > 0", name="total_positive"),
        sa.CheckConstraint(
            "state IN ('RUNNING', 'COMPLETED', 'COMPLETED_WITH_FAILURES')",
            name="state_valid",
        ),
        sa.CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
        sa.CheckConstraint("correlation_id <> ''", name="correlation_present"),
        sa.CheckConstraint("updated_at >= created_at", name="updated_after_created"),
        sa.PrimaryKeyConstraint("bulk_run_id"),
    )
    op.create_index(
        "ix_registration_bulk_runs_fingerprint_state",
        RUNS,
        ["request_fingerprint", "state"],
        unique=False,
    )
    op.create_table(
        ITEMS,
        sa.Column("bulk_item_id", sa.String(length=36), nullable=False),
        sa.Column("bulk_run_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("send_request_json", sa.Text(), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("error_class", sa.String(length=20), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("ordinal >= 1", name="ordinal_positive"),
        sa.CheckConstraint(
            "state IN ('WAITING', 'QUEUED', 'SUCCEEDED', 'FAILED')", name="state_valid"
        ),
        sa.CheckConstraint(
            "json_valid(send_request_json) AND json_type(send_request_json) = 'object'",
            name="send_request_is_object",
        ),
        sa.CheckConstraint(
            "error_class IS NULL OR error_class IN ('TRANSIENT', 'RATE_LIMITED', 'AUTH', "
            "'VALIDATION', 'POLICY_BLOCKED', 'NOT_FOUND', 'CONFLICT', 'DUPLICATE', "
            "'REVIEW_REQUIRED', 'FATAL', 'UNKNOWN')",
            name="error_class_valid",
        ),
        sa.CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
        sa.CheckConstraint(
            "(state = 'FAILED') = (error_code IS NOT NULL)", name="failure_has_error"
        ),
        sa.CheckConstraint(
            "(error_class IS NULL) = (error_code IS NULL)", name="error_class_with_code"
        ),
        sa.CheckConstraint(
            "(error_message IS NULL) = (error_code IS NULL)", name="error_message_with_code"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR queued_at IS NOT NULL", name="finish_after_queue"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= queued_at", name="finished_after_queue"
        ),
        sa.ForeignKeyConstraint(["bulk_run_id"], [f"{RUNS}.bulk_run_id"]),
        sa.ForeignKeyConstraint(["intent_id"], ["registration_intents.intent_id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"]),
        sa.PrimaryKeyConstraint("bulk_item_id"),
        sa.UniqueConstraint("bulk_run_id", "ordinal"),
        sa.UniqueConstraint("bulk_run_id", "intent_id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index(
        "ix_registration_bulk_items_intent_state",
        ITEMS,
        ["intent_id", "state"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {RUNS}")).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot downgrade 0042: {held} bulk registration run(s) exist; operation history "
            "cannot be silently discarded"
        )
    op.drop_index("ix_registration_bulk_items_intent_state", table_name=ITEMS)
    op.drop_table(ITEMS)
    op.drop_index("ix_registration_bulk_runs_fingerprint_state", table_name=RUNS)
    op.drop_table(RUNS)
