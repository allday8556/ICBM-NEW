"""M0 foundation: durable jobs, job attempts, append-only audit events.

Revision ID: 0001_m0_foundation
Revises:
Create Date: 2026-09-13

Only cross-cutting foundation tables are created in M0. Canonical commerce entities
(SupplierConnection, ProductFactsRevision, Product, ...) arrive with their milestones after
architect schema review (ARCHITECT_REVIEW §8).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_m0_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATES = "('QUEUED', 'RUNNING', 'RETRY_SCHEDULED', 'SUCCEEDED', 'DEAD')"
ATTEMPT_OUTCOMES = "('SUCCEEDED', 'FAILED', 'INTERRUPTED')"


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("target_ref", sa.String(length=200), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("last_error_class", sa.String(length=20), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"state IN {JOB_STATES}", name=op.f("ck_jobs_state_valid")),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_jobs_attempt_count_non_negative")),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_jobs_max_attempts_positive")),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_jobs")),
    )
    op.create_index("ix_jobs_state_next_attempt_at", "jobs", ["state", "next_attempt_at"])
    op.create_index("ix_jobs_correlation_id", "jobs", ["correlation_id"])
    op.create_index("ix_jobs_job_type", "jobs", ["job_type"])

    op.create_table(
        "job_attempts",
        sa.Column("attempt_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("error_class", sa.String(length=20), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            f"outcome IS NULL OR outcome IN {ATTEMPT_OUTCOMES}",
            name=op.f("ck_job_attempts_outcome_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.job_id"],
            name=op.f("fk_job_attempts_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f("pk_job_attempts")),
        sa.UniqueConstraint("job_id", "attempt_no", name=op.f("uq_job_attempts_job_id_attempt_no")),
    )

    op.create_table(
        "audit_events",
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_ref", sa.String(length=200), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_audit_events")),
        sa.UniqueConstraint("event_id", name=op.f("uq_audit_events_event_id")),
    )
    op.create_index("ix_audit_events_correlation_id", "audit_events", ["correlation_id"])
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])

    # Append-only is enforced by the database itself, not only by the absence of an API.
    op.execute(
        "CREATE TRIGGER trg_audit_events_no_update BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_audit_events_no_delete BEFORE DELETE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_update")
    op.drop_index("ix_audit_events_occurred_at", table_name="audit_events")
    op.drop_index("ix_audit_events_correlation_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("job_attempts")
    op.drop_index("ix_jobs_job_type", table_name="jobs")
    op.drop_index("ix_jobs_correlation_id", table_name="jobs")
    op.drop_index("ix_jobs_state_next_attempt_at", table_name="jobs")
    op.drop_table("jobs")
