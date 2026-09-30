"""M5 authoring-revision owners: the durable category-mapping and detail-composition revisions.

Revision ID: 0032_m5_registration_authoring_revisions
Revises: 0031_m5_registration_reconcile
Create Date: 2026-09-30

ADR-0014 §27.1, authorized concretely by the architect resolution on Issue #89 (comment
``5907626428``). It creates one table with its two partial unique indexes and its triggers. It
changes no existing row, table, trigger, index or constraint, and it backfills nothing: every
existing target-policy revision, preparation and Snapshot keeps the authoring revisions it holds,
``null`` included.

``registration_authoring_revisions`` — one row per server-owned authoring profile revision:
- ``kind`` is ``CATEGORY_MAPPING``, scoped to ``marketplace_key × taxonomy_revision``, or
  ``DETAIL_COMPOSITION``, scoped to ``marketplace_key`` alone (its ``taxonomy_revision`` is null);
- ``seq`` is the previous maximum of the exact scope plus one, so the current revision of a scope
  is its highest ``seq`` and no mutable pointer exists;
- ``content_json`` is the sanitized canonical content object and names its own kind and scope;
  ``content_fingerprint`` is the lowercase SHA-256 of it;
- a row is never updated and never deleted.

**Downgrade fails closed.** It refuses while any revision exists: a revision a target policy, a
preparation or a Snapshot may name is never silently destroyed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_m5_registration_authoring_revisions"
down_revision: str | None = "0031_m5_registration_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVISIONS = "registration_authoring_revisions"
UX_MAPPING = f"ux_{REVISIONS}_category_mapping"
UX_COMPOSITION = f"ux_{REVISIONS}_detail_composition"
TRIGGERS: tuple[str, ...] = (
    f"trg_{REVISIONS}_append",
    f"trg_{REVISIONS}_no_update",
    f"trg_{REVISIONS}_no_delete",
)


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{REVISIONS}_{name}"))


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _trigger(name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {REVISIONS} BEGIN {body} END")


def upgrade() -> None:
    op.create_table(
        REVISIONS,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("taxonomy_revision", sa.String(length=64), nullable=True),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        _check("kind IN ('CATEGORY_MAPPING', 'DETAIL_COMPOSITION')", "kind_valid"),
        _check("marketplace_key <> ''", "marketplace_key_present"),
        _check(
            "(kind = 'CATEGORY_MAPPING') = (taxonomy_revision IS NOT NULL)"
            " AND (taxonomy_revision IS NULL OR taxonomy_revision <> '')",
            "taxonomy_scope",
        ),
        _check("seq >= 1", "seq_positive"),
        _check(
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            "content_is_object",
        ),
        _check(
            "json_extract(content_json, '$.kind') IS kind"
            " AND json_extract(content_json, '$.marketplace_key') IS marketplace_key"
            " AND json_extract(content_json, '$.taxonomy_revision') IS taxonomy_revision",
            "content_names_its_scope",
        ),
        _check(
            "length(content_fingerprint) = 64 AND content_fingerprint NOT GLOB '*[^0-9a-f]*'",
            "content_fingerprint_hex",
        ),
        _check("created_by <> ''", "created_by_present"),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{REVISIONS}")),
    )
    # SQLite treats NULLs as distinct in a unique index, so each kind has its own.
    op.create_index(
        UX_MAPPING,
        REVISIONS,
        ["marketplace_key", "taxonomy_revision", "seq"],
        unique=True,
        sqlite_where=sa.text("kind = 'CATEGORY_MAPPING'"),
    )
    op.create_index(
        UX_COMPOSITION,
        REVISIONS,
        ["marketplace_key", "seq"],
        unique=True,
        sqlite_where=sa.text("kind = 'DETAIL_COMPOSITION'"),
    )
    _trigger(
        f"trg_{REVISIONS}_append",
        "INSERT",
        _raise(
            f"{REVISIONS}: revisions are appended in order within their scope",
            f"NEW.seq <> (SELECT COALESCE(MAX(seq), 0) + 1 FROM {REVISIONS}"
            " WHERE kind = NEW.kind AND marketplace_key = NEW.marketplace_key"
            " AND taxonomy_revision IS NEW.taxonomy_revision)",
        ),
    )
    _trigger(f"trg_{REVISIONS}_no_update", "UPDATE", _raise(f"{REVISIONS} is append-only", "1"))
    _trigger(f"trg_{REVISIONS}_no_delete", "DELETE", _raise(f"{REVISIONS} is never deleted", "1"))


def downgrade() -> None:
    count = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {REVISIONS}")).scalar_one()
    if count:
        raise RuntimeError(
            f"{count} authoring revision(s) exist: a revision a target policy, a preparation or a"
            " Snapshot may name is never silently destroyed"
        )
    for name in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
    op.drop_index(UX_COMPOSITION, table_name=REVISIONS)
    op.drop_index(UX_MAPPING, table_name=REVISIONS)
    op.drop_table(REVISIONS)
