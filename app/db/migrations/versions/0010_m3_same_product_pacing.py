"""M3 Stage-B2: the durable pacing identity of a real product read.

Revision ID: 0010_m3_same_product_pacing
Revises: 0009_m3_one_revision_per_run
Create Date: 2026-09-17

PR #70 review 5231792043, second P0.

ADR-0010 §4 fixes the key the same-product interval is measured on: the normalized in-scope
product URL until the identity is known, and ``(supplier_key, source_product_id)`` once it is.
Pacing on the raw URL string missed that transition — two accepted forms of one product's URL, and
the same product after a rename, are different strings and would each have got their own interval.

A run now records the key it paced on and, once the document has been read, the source identity it
turned out to be. The interval is measured across runs on that key, so alternate and renamed
accepted URLs for one product cannot each buy a fresh read.

Both pacing indexes lead with ``supplier_key``: a source product number is unique only inside the
supplier that issued it, so two suppliers that both number a product ``355`` are two products and
must never pace each other.

Additive: two nullable columns and one index, replacing the URL-keyed index 0009 added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_m3_same_product_pacing"
down_revision: str | None = "0009_m3_one_revision_per_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "collection_runs"
OLD_PACE = "ix_collection_runs_product_read"
NEW_PACE = "ix_collection_runs_pacing"
IDENTITY_PACE = "ix_collection_runs_pacing_identity"


def upgrade() -> None:
    op.add_column(RUNS, sa.Column("pacing_key", sa.Text, nullable=True))
    op.add_column(RUNS, sa.Column("source_product_id", sa.String(80), nullable=True))
    op.drop_index(OLD_PACE, table_name=RUNS)
    # Both lookups are supplier-first: a source product number is unique only inside the supplier
    # that issued it, so neither the URL nor the identity is ever matched across suppliers.
    op.create_index(NEW_PACE, RUNS, ["supplier_key", "pacing_key", "product_read_at"])
    op.create_index(IDENTITY_PACE, RUNS, ["supplier_key", "source_product_id", "product_read_at"])


def downgrade() -> None:
    op.drop_index(IDENTITY_PACE, table_name=RUNS)
    op.drop_index(NEW_PACE, table_name=RUNS)
    op.create_index(OLD_PACE, RUNS, ["supplier_key", "source_url", "product_read_at"])
    op.drop_column(RUNS, "source_product_id")
    op.drop_column(RUNS, "pacing_key")
