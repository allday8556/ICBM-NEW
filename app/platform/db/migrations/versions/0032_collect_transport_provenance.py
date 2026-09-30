"""COLLECT transport provenance (ADR-0019 §4; extension slice E1).

Revision ID: 0032_collect_transport_provenance
Revises: 0031_m5_registration_reconcile
Create Date: 2026-09-30

Issue #126 rulings ``5906290729`` (B-6) and ``5906712259`` (N-2), bound by the owner amendment
``5907095955`` to the E1 specification ``5907009512`` §7. It adds three nullable columns to
``collection_runs`` and the same three to ``product_facts_revisions``; it rewrites no row and
backfills nothing.

**What it records.**
- ``transport_kind``: how the run's document was acquired — ``EXTENSION`` or ``DIRECT_URL``.
- ``capture_policy_revision`` and ``capture_policy_digest``: the ``BrowserCapturePolicy`` an
  ``EXTENSION`` capture was cut with.

A run written before this migration keeps NULL in all three, and so does its revision. From this
slice on a new direct-URL run and its revision record ``DIRECT_URL``; E1 writes ``EXTENSION`` runs
only, and no ``EXTENSION`` revision exists before E2.

**Provenance, never identity.** None of these columns enters an evidence digest, a field or source
fingerprint, ``extraction_semantics_id`` or ``comparability_key`` (AC-08).

**Additive only (ruling N-2).** Every column is added with ``op.add_column``: no
``batch_alter_table`` and no table rebuild, so the append-only UPDATE/DELETE triggers of
``product_facts_revisions`` and every trigger of ``collection_runs`` stay exactly as they are. Each
column carries only the column-level check ``ADD COLUMN`` itself can carry — the closed transport
set, the 64-hex digest. The cross-column rule (a capture policy is named if and only if the
transport is ``EXTENSION``) is enforced by the owning store, the only writer
(``app.stages.collect.runs.RunProvenance``).

**Downgrade fails closed.** It refuses while any run or revision records the ``EXTENSION``
transport: that a document was captured by the extension, and under which policy, is evidence and
is never silently destroyed. ``DIRECT_URL`` states the one transport that existed before these
columns did, so a row recording only that loses no evidence when they are dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_collect_transport_provenance"
down_revision: str | None = "0031_m5_registration_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("collection_runs", "product_facts_revisions")
KIND = "transport_kind"
POLICY_REVISION = "capture_policy_revision"
POLICY_DIGEST = "capture_policy_digest"
COLUMNS = (KIND, POLICY_REVISION, POLICY_DIGEST)
TRANSPORT_KINDS = ("EXTENSION", "DIRECT_URL")


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    kinds = ", ".join(f"'{kind}'" for kind in TRANSPORT_KINDS)
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                KIND,
                sa.String(length=10),
                _check(table, f"{KIND} IS NULL OR {KIND} IN ({kinds})", "transport_kind_valid"),
                nullable=True,
            ),
        )
        op.add_column(table, sa.Column(POLICY_REVISION, sa.String(length=64), nullable=True))
        op.add_column(
            table,
            sa.Column(
                POLICY_DIGEST,
                sa.String(length=64),
                _check(
                    table,
                    f"{POLICY_DIGEST} IS NULL OR (length({POLICY_DIGEST}) = 64"
                    f" AND {POLICY_DIGEST} NOT GLOB '*[^0-9a-f]*')",
                    "capture_policy_digest_hex",
                ),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        held = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE {KIND} = 'EXTENSION'")
        ).scalar_one()
        if held:
            raise RuntimeError(
                f"{held} {table} row(s) record an EXTENSION capture: capture provenance is never"
                " silently destroyed"
            )
    for table in reversed(TABLES):
        for column in reversed(COLUMNS):
            op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
