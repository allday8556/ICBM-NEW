"""ADR-0039 P1: the per-supplier sales policy and the pricing snapshot's minimum pin.

Revision ID: 0061_supplier_sales_policy
Revises: 0060_supplier_onboarding
Create Date: 2026-10-11

ADR-0039 §1, §3. Two changes, nothing else. It follows ADR-0036 O1's ``0060_supplier_onboarding``.

- **``supplier_sales_policy_revisions``** (ADR-0039 §1). The append-only revisions of one
  supplier's sales policy, keyed by ``supplier_key``. Each holds the two switches —
  ``apply_channel_restrictions`` and ``apply_minimum_price`` — the server-computed fingerprint of
  its canonical content, who changed it and when. The number opens at one and moves by exactly one
  per supplier; the newest revision is the supplier's policy. A supplier with no revision uses the
  defaults (both applied, SP-02): nothing is seeded.
- **``pricing_snapshots.minimum_applied``** and **``pricing_snapshots.stated_minimum_sale_price_krw``**
  (ADR-0039 §3, SP-03). A snapshot priced while its supplier's minimum resale price was not applied
  records ``minimum_applied = 0`` and the minimum the source stated (``NULL`` when the source stated
  none it could read). Both columns are added in place (``ADD COLUMN``): no row is rewritten, and
  every existing snapshot reads ``minimum_applied = 1`` with no stated minimum — exactly what it
  priced. The CHECK ties them: an applied snapshot names no separate stated minimum, and a snapshot
  that did not apply the minimum priced without one (``minimum_sale_price_krw IS NULL``).

The database refuses an update or a delete of a policy revision and a gap in its numbering.

**Downgrade fails closed** while any policy revision exists, or any snapshot records a minimum that
was not applied: an operator's policy history and the pricing evidence are never silently dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061_supplier_sales_policy"
down_revision: str | None = "0060_supplier_onboarding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICIES = "supplier_sales_policy_revisions"
SNAPSHOTS = "pricing_snapshots"

# Frozen with this revision (``app.stages.products.models.MINIMUM_APPLIED_FLAG`` and
# ``MINIMUM_POLICY_RECORDED``).
MINIMUM_APPLIED_FLAG = "minimum_applied IN (0, 1)"
MINIMUM_POLICY_RECORDED = (
    "(minimum_applied = 1 AND stated_minimum_sale_price_krw IS NULL)"
    " OR (minimum_applied = 0 AND minimum_sale_price_krw IS NULL"
    " AND (stated_minimum_sale_price_krw IS NULL OR stated_minimum_sale_price_krw > 0))"
)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def upgrade() -> None:
    op.create_table(
        POLICIES,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=64), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("apply_channel_restrictions", sa.Boolean(), nullable=False),
        sa.Column("apply_minimum_price", sa.Boolean(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        _check(POLICIES, "supplier_key <> ''", "supplier_key_present"),
        _check(POLICIES, "revision_no >= 1", "revision_no_positive"),
        _check(
            POLICIES,
            "apply_channel_restrictions IN (0, 1) AND apply_minimum_price IN (0, 1)",
            "switches_are_flags",
        ),
        _check(POLICIES, _hex64("fingerprint"), "fingerprint_hex"),
        _check(POLICIES, "changed_by <> ''", "changed_by_present"),
        _check(POLICIES, "correlation_id <> ''", "correlation_present"),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{POLICIES}")),
        sa.UniqueConstraint(
            "supplier_key", "revision_no", name=op.f(f"uq_{POLICIES}_supplier_key_revision_no")
        ),
    )
    _trigger(
        POLICIES,
        "revision_follows",
        "INSERT",
        _raise(
            f"{POLICIES}: a revision follows the one before it",
            f"NEW.revision_no <> 1 + (SELECT COALESCE(MAX(r.revision_no), 0) FROM {POLICIES} r"
            " WHERE r.supplier_key = NEW.supplier_key)",
        ),
    )
    _trigger(POLICIES, "no_update", "UPDATE", _raise(f"a {POLICIES} row is never updated", "1"))
    _trigger(POLICIES, "no_delete", "DELETE", _raise(f"a {POLICIES} row is never deleted", "1"))
    # Two columns with column CHECKs: SQLite adds them in place and rewrites no row, so the
    # snapshot's append-only triggers never fire. Every existing snapshot applied the minimum.
    op.execute(
        f"ALTER TABLE {SNAPSHOTS} ADD COLUMN minimum_applied BOOLEAN NOT NULL DEFAULT 1"
        f" CONSTRAINT ck_{SNAPSHOTS}_minimum_applied_flag CHECK ({MINIMUM_APPLIED_FLAG})"
    )
    op.execute(
        f"ALTER TABLE {SNAPSHOTS} ADD COLUMN stated_minimum_sale_price_krw INTEGER"
        f" CONSTRAINT ck_{SNAPSHOTS}_minimum_policy_recorded CHECK ({MINIMUM_POLICY_RECORDED})"
    )


def downgrade() -> None:
    bind = op.get_bind()
    revisions = bind.execute(sa.text(f"SELECT COUNT(*) FROM {POLICIES}")).scalar_one()
    waived = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {SNAPSHOTS} WHERE minimum_applied = 0")
    ).scalar_one()
    if revisions or waived:
        raise RuntimeError(
            f"cannot remove the supplier sales policy: {revisions} policy revision(s) in"
            f" {POLICIES} and {waived} pricing snapshot(s) that did not apply the minimum are"
            " held; an operator's policy history and pricing evidence are never silently destroyed"
        )
    # The stated minimum's CHECK names minimum_applied, so it goes first.
    op.execute(f"ALTER TABLE {SNAPSHOTS} DROP COLUMN stated_minimum_sale_price_krw")
    op.execute(f"ALTER TABLE {SNAPSHOTS} DROP COLUMN minimum_applied")
    op.drop_table(POLICIES)
