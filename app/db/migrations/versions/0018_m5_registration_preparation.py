"""M5 PR-F: the REGISTER-owned durable registration preparation, revisioned and append-only.

Revision ID: 0018_m5_registration_preparation
Revises: 0017_m5_registration_execution_scope
Create Date: 2026-09-21

Issue #89 PR-F, under ADR-0014 §27 and the architect decision in comment `5751540323`, which
selected option (a) and authorized this migration. It adds exactly four tables and touches no
existing table, row, trigger or index.

**Why it exists.** A preflight is derived from the operator's own authored inputs — the category
selection, the listing values and the detail composition — and current owner truth (§3). Those
inputs had no application-owned durable source: they survived only inside a queued
`register.create` job's payload, which is execution/scheduler state. A Draft or Snapshot without a
job could therefore not be re-evaluated from durable server truth at all, and the application had
no preparation-authoring path. This owner is that source.

**What it owns, and what it does not.** Inputs only, for one exact provider-listing unit: the
Draft and Draft revision they were authored against, the exact Item membership, the category
selection with its mapping/taxonomy revisions and confirmation provenance, the listing values with
their provenance semantics, and the detail composition. It stores **no** readiness, status or
reason code, no Product, price, image or QA truth, no capability or auth truth, no provider
duplicate outcome, no marketplace asset identity, no Snapshot/Intent/Attempt/Registration truth and
no retry or queue state. A preflight stays derived, and no `REGISTERABLE` column exists anywhere.

**Revisions are append-only.** Editing a preparation appends the next revision; a revision that has
already frozen a Snapshot therefore keeps exactly the values it was authored with. The triggers
make that the database's rule: a revision and its Items are never updated or deleted, the revision
number opens at one and then moves by exactly one, and a preparation's account must be bound to its
committed provider identity like every other registration row (`ACCOUNT_IDENTITY` §4).

**The Snapshot link.** `registration_snapshot_preparations` records which exact revision, and which
inputs fingerprint, produced one immutable Snapshot. It is a separate table on purpose: the
Snapshot stays immutable with its own triggers intact, and a Snapshot frozen before this owner
existed keeps working with no provenance row.

**Downgrade fails closed.** It refuses while any of the four tables holds a row, exactly as 0016
and 0017 do: authored operator inputs and a Snapshot's provenance are never silently dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_m5_registration_preparation"
down_revision: str | None = "0017_m5_registration_execution_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREPARATIONS = "registration_preparations"
REVISIONS = "registration_preparation_revisions"
ITEMS = "registration_preparation_items"
LINKS = "registration_snapshot_preparations"
DRAFTS = "registration_drafts"
SNAPSHOTS = "registration_snapshots"
PRODUCT_ITEMS = "product_items"
ACCOUNTS = "marketplace_accounts"
CONNECTIONS = "marketplace_connections"

# Every table this migration creates, newest dependency last: the order a downgrade drops them.
CREATED = (PREPARATIONS, REVISIONS, ITEMS, LINKS)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _unique(table: str, *columns: str) -> sa.UniqueConstraint:
    """Named exactly as the metadata's naming convention names it (`uq_<table>_<columns>`)."""
    return sa.UniqueConstraint(*columns, name=op.f(f"uq_{table}_{'_'.join(columns)}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _json_object(column: str) -> str:
    return f"json_valid({column}) AND json_type({column}) = 'object'"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _immutable(table: str, what: str) -> None:
    """Append-only: an authored revision is history the moment it exists."""
    _trigger(table, "no_update", "UPDATE", _raise(f"{what} is never updated", "1"))
    _trigger(table, "no_delete", "DELETE", _raise(f"{what} is never deleted", "1"))


def upgrade() -> None:
    _create_preparations()
    _create_revisions()
    _create_items()
    _create_links()
    _install_triggers()


def _create_preparations() -> None:
    op.create_table(
        PREPARATIONS,
        sa.Column("preparation_id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(PREPARATIONS, "marketplace_key <> ''", "marketplace_key_present"),
        _check(PREPARATIONS, "created_by <> ''", "created_by_present"),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            [f"{DRAFTS}.draft_id"],
            name=op.f(f"fk_{PREPARATIONS}_draft_id_{DRAFTS}"),
        ),
        sa.ForeignKeyConstraint(
            ["marketplace_key", "marketplace_account_id"],
            [f"{ACCOUNTS}.marketplace_key", f"{ACCOUNTS}.marketplace_account_id"],
            name=op.f(f"fk_{PREPARATIONS}_marketplace_key_{ACCOUNTS}"),
        ),
        sa.PrimaryKeyConstraint("preparation_id", name=op.f(f"pk_{PREPARATIONS}")),
    )


def _create_revisions() -> None:
    op.create_table(
        REVISIONS,
        sa.Column("preparation_revision_id", sa.String(length=36), nullable=False),
        sa.Column("preparation_id", sa.String(length=36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("category_json", sa.Text(), nullable=True),
        sa.Column("listing_json", sa.Text(), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=True),
        sa.Column("inputs_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("authored_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("authored_at", sa.DateTime(), nullable=False),
        _check(REVISIONS, "revision_no >= 1", "revision_no_positive"),
        _check(REVISIONS, "draft_revision >= 1", "draft_revision_positive"),
        _check(
            REVISIONS,
            f"category_json IS NULL OR ({_json_object('category_json')})",
            "category_is_object",
        ),
        _check(REVISIONS, _json_object("listing_json"), "listing_is_object"),
        _check(
            REVISIONS, f"detail_json IS NULL OR ({_json_object('detail_json')})", "detail_is_object"
        ),
        _check(REVISIONS, _hex64("inputs_fingerprint"), "inputs_fingerprint_hex"),
        _check(REVISIONS, "authored_by <> ''", "authored_by_present"),
        _check(REVISIONS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["preparation_id"],
            [f"{PREPARATIONS}.preparation_id"],
            name=op.f(f"fk_{REVISIONS}_preparation_id_{PREPARATIONS}"),
        ),
        sa.PrimaryKeyConstraint("preparation_revision_id", name=op.f(f"pk_{REVISIONS}")),
        _unique(REVISIONS, "preparation_id", "revision_no"),
    )


def _create_items() -> None:
    op.create_table(
        ITEMS,
        sa.Column("preparation_item_id", sa.String(length=36), nullable=False),
        sa.Column("preparation_revision_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        _check(ITEMS, "ordinal >= 0", "ordinal_non_negative"),
        sa.ForeignKeyConstraint(
            ["preparation_revision_id"],
            [f"{REVISIONS}.preparation_revision_id"],
            name=op.f(f"fk_{ITEMS}_preparation_revision_id_{REVISIONS}"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            [f"{PRODUCT_ITEMS}.item_id"],
            name=op.f(f"fk_{ITEMS}_item_id_{PRODUCT_ITEMS}"),
        ),
        sa.PrimaryKeyConstraint("preparation_item_id", name=op.f(f"pk_{ITEMS}")),
        _unique(ITEMS, "preparation_revision_id", "item_id"),
        _unique(ITEMS, "preparation_revision_id", "ordinal"),
    )


def _create_links() -> None:
    op.create_table(
        LINKS,
        sa.Column("registration_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("preparation_revision_id", sa.String(length=36), nullable=False),
        sa.Column("inputs_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("identity_generation", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        _check(LINKS, _hex64("inputs_fingerprint"), "inputs_fingerprint_hex"),
        _check(LINKS, "identity_generation >= 0", "identity_generation_non_negative"),
        sa.ForeignKeyConstraint(
            ["registration_snapshot_id"],
            [f"{SNAPSHOTS}.registration_snapshot_id"],
            name=op.f(f"fk_{LINKS}_registration_snapshot_id_{SNAPSHOTS}"),
        ),
        sa.ForeignKeyConstraint(
            ["preparation_revision_id"],
            [f"{REVISIONS}.preparation_revision_id"],
            name=op.f(f"fk_{LINKS}_preparation_revision_id_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("registration_snapshot_id", name=op.f(f"pk_{LINKS}")),
    )


def _install_triggers() -> None:
    # ACCOUNT_IDENTITY §4: no registration state opens for an unbound or mismatched account.
    _trigger(
        PREPARATIONS,
        "account_bound",
        "INSERT",
        _raise(
            f"{PREPARATIONS}: the marketplace account is not bound to its identity",
            f"NOT EXISTS (SELECT 1 FROM {ACCOUNTS} a JOIN {CONNECTIONS} c"
            " ON c.marketplace_key = a.marketplace_key"
            " WHERE a.marketplace_account_id = NEW.marketplace_account_id"
            " AND a.marketplace_key = NEW.marketplace_key"
            " AND c.provider_account_uid = a.provider_account_uid)",
        ),
    )
    # A preparation prepares a unit **of its own Draft**, in its own account scope.
    _trigger(
        PREPARATIONS,
        "draft_scope",
        "INSERT",
        _raise(
            f"{PREPARATIONS}: the Draft belongs to another marketplace account",
            f"NOT EXISTS (SELECT 1 FROM {DRAFTS} d WHERE d.draft_id = NEW.draft_id"
            " AND d.marketplace_key = NEW.marketplace_key"
            " AND d.marketplace_account_id = NEW.marketplace_account_id)",
        ),
    )
    _immutable(PREPARATIONS, f"{PREPARATIONS}")
    # The revision counter opens at one and then moves by exactly one: no gap, no rewrite, no
    # second "current" revision.
    _trigger(
        REVISIONS,
        "revision_follows",
        "INSERT",
        _raise(
            f"{REVISIONS}: a revision follows the one before it",
            "NEW.revision_no <> 1 + (SELECT COALESCE(MAX(revision_no), 0)"
            f" FROM {REVISIONS} r WHERE r.preparation_id = NEW.preparation_id)",
        ),
    )
    _immutable(REVISIONS, f"an authored {REVISIONS} row")
    # An Item of a revision is that revision's exact membership, and belongs to its Draft.
    _trigger(
        ITEMS,
        "item_in_draft",
        "INSERT",
        _raise(
            f"{ITEMS}: the Item is not an open Item of the Draft being prepared",
            f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
            f" JOIN {PREPARATIONS} p ON p.preparation_id = r.preparation_id"
            " JOIN registration_draft_items i ON i.draft_id = p.draft_id"
            " WHERE r.preparation_revision_id = NEW.preparation_revision_id"
            " AND i.item_id = NEW.item_id AND i.removed_at IS NULL)",
        ),
    )
    _immutable(ITEMS, f"an authored {ITEMS} row")
    # One Snapshot has one provenance, recorded once: the Snapshot itself stays immutable.
    _trigger(
        LINKS,
        "fingerprint_is_its_revision",
        "INSERT",
        _raise(
            f"{LINKS}: the fingerprint is not the one the named revision holds",
            f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
            " WHERE r.preparation_revision_id = NEW.preparation_revision_id"
            " AND r.inputs_fingerprint = NEW.inputs_fingerprint)",
        ),
    )
    _immutable(LINKS, f"a {LINKS} row")


def downgrade() -> None:
    bind = op.get_bind()
    held = {
        table: bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in CREATED
    }
    if any(held.values()):
        raise RuntimeError(
            "cannot drop the registration preparation owner:"
            f" {_counted(held)} are held; authored operator inputs and a Snapshot's provenance"
            " are never silently destroyed"
        )
    for table in reversed(CREATED):
        op.drop_table(table)


def _counted(held: dict[str, int]) -> str:
    return ", ".join(f"{count} row(s) in {table}" for table, count in held.items() if count)
