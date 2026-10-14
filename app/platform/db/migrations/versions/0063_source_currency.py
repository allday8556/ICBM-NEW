"""ADR-0038 O1a: the source currency of a product facts revision.

Revision ID: 0063_source_currency
Revises: 0061_supplier_sales_policy
Create Date: 2026-10-14

ADR-0038 §1 (FX-01). One change, nothing else: **``product_facts_revisions.source_currency``**, the
one currency an overseas page states its prices in — ``CNY`` or ``JPY`` — kept as the page states
it. Nothing is converted in a revision.

- ``NULL`` on a KRW page, which is every revision written before this migration: none is rewritten
  and nothing is backfilled.
- ``currency`` stays ``'KRW'``: it is the unit of every ``*_krw`` amount, which a foreign revision
  states none of.
- The cross-row rule — a revision whose values state a source currency names exactly that one, and
  states no KRW money beside it — is enforced by the revision store, the only writer
  (``app.stages.collect.facts.revision_source_currency``).

**Provenance of the amounts, never identity.** The column enters no evidence digest and no field or
source fingerprint: each foreign value already carries its currency inside its hashed value JSON.

**Additive only** (precedent 0034). The column is added with ``ADD COLUMN`` and its column CHECK:
no ``batch_alter_table`` and no table rebuild, so the append-only UPDATE/DELETE triggers of
``product_facts_revisions`` stay exactly as they are.

**Downgrade fails closed** while any revision records a source currency: which currency a page
stated its prices in is source truth, and is never silently destroyed. A revision without one loses
nothing when the column is dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_source_currency"
down_revision: str | None = "0061_supplier_sales_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "product_facts_revisions"
COLUMN = "source_currency"
# Frozen with this revision (``app.stages.collect.facts.SOURCE_CURRENCIES``).
SOURCE_CURRENCIES = ("CNY", "JPY")


def upgrade() -> None:
    currencies = ", ".join(f"'{currency}'" for currency in SOURCE_CURRENCIES)
    op.execute(
        f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(3)"
        f" CONSTRAINT ck_{TABLE}_source_currency_valid"
        f" CHECK ({COLUMN} IS NULL OR {COLUMN} IN ({currencies}))"
    )


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {TABLE} WHERE {COLUMN} IS NOT NULL")
    ).scalar_one()
    if held:
        raise RuntimeError(
            f"{held} {TABLE} row(s) record a source currency: the currency a page stated its"
            " prices in is source truth and is never silently destroyed"
        )
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
