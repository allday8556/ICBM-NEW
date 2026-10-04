"""Store immutable marketplace leaf-category catalog snapshots.

Revision ID: 0041_category_catalog
Revises: 0040_image_auto_selection
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_category_catalog"
down_revision: str | None = "0040_image_auto_selection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SNAPSHOTS = "marketplace_category_catalog_snapshots"
ENTRIES = "marketplace_category_catalog_entries"


def _immutable(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table}: append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table}: append-only'); END"
    )


def upgrade() -> None:
    op.create_table(
        SNAPSHOTS,
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("taxonomy_revision", sa.String(length=64), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("snapshot_sequence", sa.Integer(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("endpoint_mapping_revision", sa.String(length=64), nullable=False),
        sa.Column("recorded_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("marketplace_key <> ''", name="marketplace_key_present"),
        sa.CheckConstraint("taxonomy_revision <> ''", name="taxonomy_revision_present"),
        sa.CheckConstraint(
            "length(content_fingerprint) = 64 AND content_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="content_fingerprint_hex",
        ),
        sa.CheckConstraint("entry_count >= 1", name="entry_count_positive"),
        sa.CheckConstraint("snapshot_sequence >= 1", name="snapshot_sequence_positive"),
        sa.CheckConstraint("recorded_by <> ''", name="recorded_by_present"),
        sa.CheckConstraint("correlation_id <> ''", name="correlation_present"),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint("marketplace_key", "content_fingerprint"),
        sa.UniqueConstraint("marketplace_key", "taxonomy_revision"),
        sa.UniqueConstraint("marketplace_key", "snapshot_sequence"),
    )
    op.create_table(
        ENTRIES,
        sa.Column("entry_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("category_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("whole_category_name", sa.String(length=1000), nullable=False),
        sa.Column("leaf", sa.Integer(), nullable=False),
        sa.CheckConstraint("category_id <> ''", name="category_id_present"),
        sa.CheckConstraint("name <> ''", name="name_present"),
        sa.CheckConstraint("whole_category_name <> ''", name="whole_name_present"),
        sa.CheckConstraint("leaf = 1", name="leaf_true"),
        sa.ForeignKeyConstraint(["snapshot_id"], [f"{SNAPSHOTS}.snapshot_id"]),
        sa.PrimaryKeyConstraint("entry_id"),
        sa.UniqueConstraint("snapshot_id", "category_id"),
    )
    op.create_index(
        "ix_marketplace_category_catalog_entries_snapshot_name",
        ENTRIES,
        ["snapshot_id", "name"],
        unique=False,
    )
    _immutable(SNAPSHOTS)
    _immutable(ENTRIES)


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {SNAPSHOTS}")).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot downgrade 0041: {held} category catalog snapshot(s) exist; provider evidence"
            " is append-only"
        )
    op.execute(f"DROP TRIGGER trg_{ENTRIES}_no_delete")
    op.execute(f"DROP TRIGGER trg_{ENTRIES}_no_update")
    op.execute(f"DROP TRIGGER trg_{SNAPSHOTS}_no_delete")
    op.execute(f"DROP TRIGGER trg_{SNAPSHOTS}_no_update")
    op.drop_index("ix_marketplace_category_catalog_entries_snapshot_name", table_name=ENTRIES)
    op.drop_table(ENTRIES)
    op.drop_table(SNAPSHOTS)
