"""M3 Stage-B2: one revision per durable run, and a durable same-product read interval.

Revision ID: 0009_m3_one_revision_per_run
Revises: 0008_m3_collection_runs
Create Date: 2026-09-17

PR #70 review 5231130447, both P0 findings.

A collection appends its revision and then settles its run. If the process dies between the two,
the job is still retryable, and without a guard the next attempt would read the provider again and
append a *second* immutable revision for the same run. The unique index makes that impossible in
the database, not only in the code path: recollection history belongs to distinct runs.

The same-product interval of ADR-0010 §4 was declared in the profile and never enforced. A run now
reserves its product read durably, and the reservation is what the next run is measured against,
so a restart or a concurrent submission cannot read the same product twice inside the interval.

Additive: one index and one nullable column. Downgrade drops exactly those.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_m3_one_revision_per_run"
down_revision: str | None = "0008_m3_collection_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVISIONS = "product_facts_revisions"
RUNS = "collection_runs"
ONE_PER_RUN = "ux_product_facts_revisions_collection_run"
READ_PACE = "ix_collection_runs_product_read"


def upgrade() -> None:
    op.create_index(ONE_PER_RUN, REVISIONS, ["collection_run_id"], unique=True)
    op.add_column(RUNS, sa.Column("product_read_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(READ_PACE, RUNS, ["supplier_key", "source_url", "product_read_at"])


def downgrade() -> None:
    op.drop_index(READ_PACE, table_name=RUNS)
    op.drop_column(RUNS, "product_read_at")
    op.drop_index(ONE_PER_RUN, table_name=REVISIONS)
