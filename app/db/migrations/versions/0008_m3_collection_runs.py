"""M3 Stage-B2: the durable identity and result of one operator-submitted collection.

Revision ID: 0008_m3_collection_runs
Revises: 0007_m3_product_facts_revisions
Create Date: 2026-09-17

Additive only (ADR-0010 §6; Issue #52 ruling 5706133893). One row per submitted collection,
opened in the same unit of work as its ``collect.*`` job so the operator holds a result identity
from the moment the request is accepted.

Unlike the source-truth tables this row is not append-only: a run moves from ``PENDING`` to
exactly one terminal outcome and then stops. The CHECK constraints make the result contract
structural — a revision may be named only by a ``RECORDED`` run, and a source identity the page
did not state ends the run as ``NO_REVISION``, which is a success with nothing to append.

Downgrade refuses while any run exists: a collection's result identity is never silently dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_m3_collection_runs"
down_revision: str | None = "0007_m3_product_facts_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "collection_runs"

# Vocabularies frozen with this revision (app.collect.models, app.collect.facts).
_OUTCOMES = ("PENDING", "RECORDED", "NO_REVISION", "FAILED")
_FACTS_STATUSES = ("CONFIRMED", "REVIEW_REQUIRED")


def _in(column: str, values: Sequence[str], *, nullable: bool = False) -> str:
    clause = f"{column} IN ({', '.join(repr(v) for v in values)})"
    return f"{column} IS NULL OR {clause}" if nullable else clause


def upgrade() -> None:
    op.create_table(
        RUNS,
        sa.Column("collection_run_id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("supplier_key", sa.String(40), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column(
            "revision_id",
            sa.String(36),
            sa.ForeignKey("product_facts_revisions.revision_id"),
            nullable=True,
        ),
        sa.Column("facts_status", sa.String(20), nullable=True),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in("outcome", _OUTCOMES), name="outcome_valid"),
        sa.CheckConstraint(
            _in("facts_status", _FACTS_STATUSES, nullable=True), name="facts_status_valid"
        ),
        sa.CheckConstraint(
            "(outcome = 'RECORDED' AND revision_id IS NOT NULL AND facts_status IS NOT NULL)"
            " OR (outcome <> 'RECORDED' AND revision_id IS NULL AND facts_status IS NULL)",
            name="revision_only_when_recorded",
        ),
        sa.CheckConstraint(
            "(outcome IN ('PENDING')) = (finished_at IS NULL)", name="finished_when_terminal"
        ),
        sa.CheckConstraint("source_url LIKE 'https://%'", name="source_url_https"),
    )
    op.create_index("ix_collection_runs_job_id", RUNS, ["job_id"])
    op.create_index("ix_collection_runs_supplier", RUNS, ["supplier_key", "requested_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {RUNS}")).scalar_one():
        raise RuntimeError("collection runs exist; a collection's result identity is never dropped")
    op.drop_index("ix_collection_runs_supplier", table_name=RUNS)
    op.drop_index("ix_collection_runs_job_id", table_name=RUNS)
    op.drop_table(RUNS)
