"""The operator's decisions on supplier common images (Issue #219, owner decision 2026-10-03).

Revision ID: 0039_supplier_common_images
Revises: 0038_synthetic_test_products
Create Date: 2026-10-04

It creates one table and its triggers, and touches no other table, row, trigger or index.

``supplier_common_image_decisions`` is append-only. A supplier common image is a file — keyed by its
supplier and its SHA-256 — that the supplier repeats across products' detail pages: a shipping
notice, a seller warning, a contact card, a blank spacer or a brand banner. Each row is one
decision, ``BLOCK`` or ``KEEP``; the newest revision of a key is its current decision. The reserved
``icbm-synthetic`` namespace never holds one: a synthetic test product reads its template
supplier's decisions.

A fresh database holds no decision: the owner's own decisions of Issue #219 §1 are code
(``app.stages.products.common_images.OWNER_SEED``), and an operator decision recorded here
supersedes them.

**Downgrade fails closed.** It refuses while any decision exists: an operator's decision is never
silently destroyed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_supplier_common_images"
down_revision: str | None = "0038_synthetic_test_products"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "supplier_common_image_decisions"


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{TABLE}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("decision_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check("supplier_key <> ''", "supplier_present"),
        _check("supplier_key <> 'icbm-synthetic'", "supplier_is_collected"),
        _check(_hex64("sha256"), "sha256_hex"),
        _check("revision_no >= 1", "revision_no_positive"),
        _check("verdict IN ('BLOCK', 'KEEP')", "verdict_valid"),
        _check("reason IS NULL OR (reason <> '' AND length(reason) <= 200)", "reason_bounded"),
        _check("decided_by <> ''", "decided_by_present"),
        _check("correlation_id <> ''", "correlation_present"),
        sa.PrimaryKeyConstraint("decision_id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint(
            "supplier_key",
            "sha256",
            "revision_no",
            name=op.f(f"uq_{TABLE}_supplier_key_sha256_revision_no"),
        ),
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
            f"cannot downgrade 0039: {held} operator decision(s) on supplier common images exist;"
            " an operator's decision is never silently destroyed"
        )
    op.drop_table(TABLE)
