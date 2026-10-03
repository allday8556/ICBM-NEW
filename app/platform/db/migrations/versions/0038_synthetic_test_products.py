"""The label of an operator's synthetic test product (owner decision 2026-10-03).

Revision ID: 0038_synthetic_test_products
Revises: 0037_g3_delete_stage
Create Date: 2026-10-03

It creates one table and its triggers, and touches no other table, row, trigger or index.

``synthetic_test_products`` is append-only. Each row says that one product in the reserved
``icbm-synthetic`` supplier namespace is a synthetic test product: a copy, unchanged, of one
collected revision's facts, under the operator's label. The reserved namespace is enforced, a
copy of a copy is refused, a label and a copied identity are each used once, and a row is never
changed or deleted.

**Downgrade fails closed.** It refuses while any row exists: the label of a test product that may
have been listed is never silently destroyed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_synthetic_test_products"
down_revision: str | None = "0037_g3_delete_stage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "synthetic_test_products"
SYNTHETIC = "icbm-synthetic"


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{TABLE}_{name}"))


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("synthetic_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("source_product_id", sa.String(length=200), nullable=False),
        sa.Column("template_revision_id", sa.String(length=36), nullable=False),
        sa.Column("template_supplier_key", sa.String(length=40), nullable=False),
        sa.Column("template_source_product_id", sa.String(length=200), nullable=False),
        sa.Column("collection_run_id", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(f"supplier_key = '{SYNTHETIC}'", "reserved_namespace"),
        _check("source_product_id <> ''", "source_product_present"),
        _check(f"template_supplier_key <> '{SYNTHETIC}'", "template_is_collected"),
        _check("length(label) BETWEEN 1 AND 40", "label_bounded"),
        _check("created_by <> ''", "actor_present"),
        _check("correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["template_revision_id"],
            ["product_facts_revisions.revision_id"],
            name=op.f(f"fk_{TABLE}_template_revision_id_product_facts_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["collection_run_id"],
            ["collection_runs.collection_run_id"],
            name=op.f(f"fk_{TABLE}_collection_run_id_collection_runs"),
        ),
        sa.PrimaryKeyConstraint("synthetic_id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint(
            "supplier_key",
            "source_product_id",
            name=op.f(f"uq_{TABLE}_supplier_key_source_product_id"),
        ),
        sa.UniqueConstraint("label", name=op.f(f"uq_{TABLE}_label")),
        sa.UniqueConstraint("collection_run_id", name=op.f(f"uq_{TABLE}_collection_run_id")),
    )
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{TABLE}_no_{event.lower()} BEFORE {event} ON {TABLE}"
            f" BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only'); END"
        )


def downgrade() -> None:
    held = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot downgrade 0038: {held} synthetic test product label(s) exist; the label of a"
            " test product is never silently destroyed"
        )
    op.drop_table(TABLE)
