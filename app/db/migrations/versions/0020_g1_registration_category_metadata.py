"""Gate 1 G1-B: the durable operator-reviewed category metadata, revisioned and append-only.

Revision ID: 0020_g1_registration_category_metadata
Revises: 0019_g1_registration_target_policy
Create Date: 2026-09-23

Issue #89 Gate 1, slice G1-B, under ADR-0015 §3 and the implementation authorization in comment
`5788082735`. It adds exactly three tables and touches no existing table, row, trigger or index.

**Why it exists.** The REGISTER preflight reads the reviewed metadata of the selected category on
every evaluation (ADR-0014 §4), but production had nothing durable behind that read: an empty
in-memory source, so every evaluation stopped at `CATEGORY_METADATA_MISSING`. No provider category
endpoint is adopted (ADR-0015 §3), so this owner holds metadata an operator records from reviewed
evidence.

**What it owns, and what it does not.** One canonical content document per revision that
materializes the existing `CategoryMetadata` contract — leaf and registrable state, the name
length, the attribute rules, the notice type and its field rules, the option rule and the required
templates — plus the sanitized reference of the evidence it was reviewed from and whether it is
reviewed. It stores **no** raw provider payload, no readiness or reason code, no Product, price or
account truth, and no second category model.

**Revisions are append-only, and the current one is explicit.**
- `registration_category_metadata` is the identity of one `marketplace_key × taxonomy_revision ×
  category_id`.
- `registration_category_metadata_revisions` holds its revisions: server-created identity (the
  `CategoryMetadata.metadata_revision`), content fingerprint and recording time. `reviewed` is
  explicit, and a reviewed revision carries its reviewer and review time; an unreviewed one carries
  neither. A revision is never updated or deleted, and its number moves by exactly one.
- `registration_category_metadata_current` names the one current revision of each key. It only
  ever names the newest revision of its own key — never "the newest reviewed one" — and is never
  deleted.

**Downgrade fails closed.** It refuses while any of the three tables holds a row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_g1_registration_category_metadata"
down_revision: str | None = "0019_g1_registration_target_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

METADATA = "registration_category_metadata"
REVISIONS = "registration_category_metadata_revisions"
CURRENT = "registration_category_metadata_current"

# Every table this migration creates, newest dependency last: the order a downgrade drops them.
CREATED = (METADATA, REVISIONS, CURRENT)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _unique(table: str, *columns: str) -> sa.UniqueConstraint:
    """Named exactly as the metadata's naming convention names it (`uq_<table>_<columns>`)."""
    return sa.UniqueConstraint(*columns, name=op.f(f"uq_{table}_{'_'.join(columns)}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


# A reviewed revision names who reviewed it and when; an unreviewed one names neither.
REVIEW_PROVENANCE = (
    "reviewed IN (0, 1)"
    " AND (reviewed = 1) = (reviewed_by IS NOT NULL)"
    " AND (reviewed = 1) = (reviewed_at IS NOT NULL)"
    " AND (reviewed_by IS NULL OR reviewed_by <> '')"
)


def upgrade() -> None:
    _create_metadata()
    _create_revisions()
    _create_current()
    _install_triggers()


def _create_metadata() -> None:
    op.create_table(
        METADATA,
        sa.Column("metadata_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("taxonomy_revision", sa.String(length=64), nullable=False),
        sa.Column("category_id", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(METADATA, "marketplace_key <> ''", "marketplace_key_present"),
        _check(METADATA, "taxonomy_revision <> ''", "taxonomy_revision_present"),
        _check(METADATA, "category_id <> ''", "category_id_present"),
        _check(METADATA, "created_by <> ''", "created_by_present"),
        _check(METADATA, "correlation_id <> ''", "correlation_present"),
        sa.PrimaryKeyConstraint("metadata_id", name=op.f(f"pk_{METADATA}")),
        _unique(METADATA, "marketplace_key", "taxonomy_revision", "category_id"),
    )


def _create_revisions() -> None:
    op.create_table(
        REVISIONS,
        sa.Column("metadata_revision_id", sa.String(length=36), nullable=False),
        sa.Column("metadata_id", sa.String(length=36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reviewed", sa.Integer(), nullable=False),
        sa.Column("reviewed_by", sa.String(length=64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("recorded_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        _check(REVISIONS, "revision_no >= 1", "revision_no_positive"),
        _check(
            REVISIONS,
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            "content_is_object",
        ),
        _check(REVISIONS, _hex64("content_fingerprint"), "content_fingerprint_hex"),
        _check(REVISIONS, REVIEW_PROVENANCE, "review_provenance"),
        _check(REVISIONS, "recorded_by <> ''", "recorded_by_present"),
        _check(REVISIONS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["metadata_id"],
            [f"{METADATA}.metadata_id"],
            name=op.f(f"fk_{REVISIONS}_metadata_id_{METADATA}"),
        ),
        sa.PrimaryKeyConstraint("metadata_revision_id", name=op.f(f"pk_{REVISIONS}")),
        _unique(REVISIONS, "metadata_id", "revision_no"),
    )


def _create_current() -> None:
    op.create_table(
        CURRENT,
        sa.Column("metadata_id", sa.String(length=36), nullable=False),
        sa.Column("metadata_revision_id", sa.String(length=36), nullable=False),
        sa.Column("moved_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(CURRENT, "moved_by <> ''", "moved_by_present"),
        _check(CURRENT, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["metadata_id"],
            [f"{METADATA}.metadata_id"],
            name=op.f(f"fk_{CURRENT}_metadata_id_{METADATA}"),
        ),
        sa.ForeignKeyConstraint(
            ["metadata_revision_id"],
            [f"{REVISIONS}.metadata_revision_id"],
            name=op.f(f"fk_{CURRENT}_metadata_revision_id_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("metadata_id", name=op.f(f"pk_{CURRENT}")),
    )


# The named revision belongs to the pointer's own key and is that key's newest revision — never
# "the newest reviewed one". Revision numbers only grow, so the pointer only moves forward.
_NEWEST_OF_ITS_KEY = (
    f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
    " WHERE r.metadata_revision_id = NEW.metadata_revision_id AND r.metadata_id = NEW.metadata_id"
    f" AND r.revision_no = (SELECT MAX(m.revision_no) FROM {REVISIONS} m"
    " WHERE m.metadata_id = NEW.metadata_id))"
)


def _install_triggers() -> None:
    _trigger(METADATA, "no_update", "UPDATE", _raise(f"{METADATA} is never updated", "1"))
    _trigger(METADATA, "no_delete", "DELETE", _raise(f"{METADATA} is never deleted", "1"))
    # The revision counter opens at one and then moves by exactly one: no gap and no rewrite.
    _trigger(
        REVISIONS,
        "revision_follows",
        "INSERT",
        _raise(
            f"{REVISIONS}: a revision follows the one before it",
            "NEW.revision_no <> 1 + (SELECT COALESCE(MAX(revision_no), 0)"
            f" FROM {REVISIONS} r WHERE r.metadata_id = NEW.metadata_id)",
        ),
    )
    # A revision's content names exactly the marketplace, taxonomy and category of its own key.
    _trigger(
        REVISIONS,
        "content_scope",
        "INSERT",
        _raise(
            f"{REVISIONS}: the content names another marketplace, taxonomy or category",
            f"NOT EXISTS (SELECT 1 FROM {METADATA} k WHERE k.metadata_id = NEW.metadata_id"
            " AND json_extract(NEW.content_json, '$.marketplace_key') = k.marketplace_key"
            " AND json_extract(NEW.content_json, '$.taxonomy_revision') = k.taxonomy_revision"
            " AND json_extract(NEW.content_json, '$.category_id') = k.category_id)",
        ),
    )
    _trigger(REVISIONS, "no_update", "UPDATE", _raise(f"a {REVISIONS} row is never updated", "1"))
    _trigger(REVISIONS, "no_delete", "DELETE", _raise(f"a {REVISIONS} row is never deleted", "1"))
    _trigger(
        CURRENT,
        "names_newest_on_insert",
        "INSERT",
        _raise(f"{CURRENT}: the current revision is the newest of its own key", _NEWEST_OF_ITS_KEY),
    )
    _trigger(
        CURRENT,
        "same_key",
        "UPDATE",
        _raise(f"{CURRENT}: a pointer never changes key", "NEW.metadata_id <> OLD.metadata_id"),
    )
    _trigger(
        CURRENT,
        "names_newest_on_update",
        "UPDATE",
        _raise(f"{CURRENT}: the current revision is the newest of its own key", _NEWEST_OF_ITS_KEY),
    )
    _trigger(CURRENT, "no_delete", "DELETE", _raise(f"a {CURRENT} row is never deleted", "1"))


def downgrade() -> None:
    bind = op.get_bind()
    held = {
        table: bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in CREATED
    }
    if any(held.values()):
        raise RuntimeError(
            "cannot drop the category-metadata owner:"
            f" {_counted(held)} are held; reviewed metadata history is never silently destroyed"
        )
    for table in reversed(CREATED):
        op.drop_table(table)


def _counted(held: dict[str, int]) -> str:
    return ", ".join(f"{count} row(s) in {table}" for table, count in held.items() if count)
