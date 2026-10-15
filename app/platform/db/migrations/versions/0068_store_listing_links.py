"""ADR-0042 S2a: the ListingLinks owner — store listing ↔ common product links.

Revision ID: 0068_store_listing_links
Revises: 0067_store_catalog
Create Date: 2026-10-16

ADR-0042 §7, §12 (SCP-09 to SCP-12). Three tables, nothing else changes:

- **``store_listing_links``**: one store listing linked to one ``ProductGroup`` (the common
  product). Append-only: the only change is ``ACTIVE → ENDED`` with who ended it, when and why
  (trigger). At most one ``ACTIVE`` link per store listing (unique index). A link of an ICBM
  registration or an ADR-0024 adoption is never stored here: those are read as links from their
  own owners.
- **``store_listing_decisions``**: the seller's 「다른 상품」 — this listing is not that common
  product. Revocable: the only change is setting its revocation once (trigger).
- **``store_link_notices``**: one notice per automatic strong-key link (「자동 연결됨 · 되돌리기」).
  The only change is the seller's acknowledgement, once (trigger).

Nothing here is a product: a link points a provider listing at the one product root
(SCP-01).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068_store_listing_links"
down_revision: str | None = "0067_store_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINKS = "store_listing_links"
DECISIONS = "store_listing_decisions"
NOTICES = "store_link_notices"


def _no_delete(table: str, what: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER {table}_no_delete
        BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, '{what} are never deleted'); END;
        """
    )


def upgrade() -> None:
    op.create_table(
        LINKS,
        sa.Column("link_id", sa.String(length=36), nullable=False),
        sa.Column("listing_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("key_kind", sa.String(length=20), nullable=True),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("ended_by", sa.String(length=64), nullable=True),
        sa.Column("end_reason", sa.String(length=20), nullable=True),
        sa.CheckConstraint(
            "kind IN ('IMPORTED', 'STRONG_KEY', 'USER_CONFIRMED')", name="kind_valid"
        ),
        sa.CheckConstraint(
            "(kind = 'STRONG_KEY') = (key_kind IS NOT NULL)", name="strong_key_names_its_key"
        ),
        sa.CheckConstraint(
            "key_kind IS NULL OR key_kind IN ('SELLER_CODE', 'GTIN')", name="key_kind_valid"
        ),
        sa.CheckConstraint("state IN ('ACTIVE', 'ENDED')", name="state_valid"),
        sa.CheckConstraint(
            "(state = 'ENDED') = (ended_at IS NOT NULL)"
            " AND (ended_at IS NULL) = (ended_by IS NULL)"
            " AND (ended_at IS NULL) = (end_reason IS NULL)",
            name="ended_is_recorded",
        ),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('UNDO', 'UNLINK', 'MERGED')",
            name="end_reason_valid",
        ),
        sa.CheckConstraint(
            "marketplace_key <> '' AND marketplace_account_id <> '' AND decided_by <> ''"
            " AND correlation_id <> '' AND (ended_by IS NULL OR ended_by <> '')",
            name="identity_present",
        ),
        sa.ForeignKeyConstraint(["listing_id"], ["store_catalog_listings.listing_id"]),
        sa.ForeignKeyConstraint(["product_group_id"], ["product_groups.product_group_id"]),
        sa.PrimaryKeyConstraint("link_id"),
    )
    op.create_index(
        "ux_store_listing_links_one_active",
        LINKS,
        ["listing_id"],
        unique=True,
        sqlite_where=sa.text("state = 'ACTIVE'"),
    )
    op.create_index(
        "ix_store_listing_links_group",
        LINKS,
        ["product_group_id", "state"],
        unique=False,
    )
    # A link only ends, once, and nothing else of it ever changes.
    op.execute(
        f"""
        CREATE TRIGGER {LINKS}_only_ends
        BEFORE UPDATE ON {LINKS}
        WHEN NOT (OLD.state = 'ACTIVE' AND NEW.state = 'ENDED')
          OR NEW.link_id IS NOT OLD.link_id
          OR NEW.listing_id IS NOT OLD.listing_id
          OR NEW.marketplace_key IS NOT OLD.marketplace_key
          OR NEW.marketplace_account_id IS NOT OLD.marketplace_account_id
          OR NEW.product_group_id IS NOT OLD.product_group_id
          OR NEW.kind IS NOT OLD.kind
          OR NEW.key_kind IS NOT OLD.key_kind
          OR NEW.decided_by IS NOT OLD.decided_by
          OR NEW.correlation_id IS NOT OLD.correlation_id
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN SELECT RAISE(ABORT, 'a store listing link only ends, once'); END;
        """
    )
    _no_delete(LINKS, "store listing links")
    op.create_table(
        DECISIONS,
        sa.Column("decision_id", sa.String(length=36), nullable=False),
        sa.Column("listing_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)", name="revocation_recorded"
        ),
        sa.CheckConstraint(
            "decided_by <> '' AND correlation_id <> ''"
            " AND (revoked_by IS NULL OR revoked_by <> '')",
            name="identity_present",
        ),
        sa.ForeignKeyConstraint(["listing_id"], ["store_catalog_listings.listing_id"]),
        sa.ForeignKeyConstraint(["product_group_id"], ["product_groups.product_group_id"]),
        sa.PrimaryKeyConstraint("decision_id"),
    )
    op.create_index(
        "ux_store_listing_decisions_one_active",
        DECISIONS,
        ["listing_id", "product_group_id"],
        unique=True,
        sqlite_where=sa.text("revoked_at IS NULL"),
    )
    op.execute(
        f"""
        CREATE TRIGGER {DECISIONS}_only_revoked
        BEFORE UPDATE ON {DECISIONS}
        WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
          OR NEW.decision_id IS NOT OLD.decision_id
          OR NEW.listing_id IS NOT OLD.listing_id
          OR NEW.product_group_id IS NOT OLD.product_group_id
          OR NEW.decided_by IS NOT OLD.decided_by
          OR NEW.correlation_id IS NOT OLD.correlation_id
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN SELECT RAISE(ABORT, 'a different-product decision is only revoked, once'); END;
        """
    )
    _no_delete(DECISIONS, "different-product decisions")
    op.create_table(
        NOTICES,
        sa.Column("notice_id", sa.String(length=36), nullable=False),
        sa.Column("link_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "(acknowledged_at IS NULL) = (acknowledged_by IS NULL)"
            " AND (acknowledged_by IS NULL OR acknowledged_by <> '')",
            name="acknowledgement_recorded",
        ),
        sa.ForeignKeyConstraint(["link_id"], [f"{LINKS}.link_id"]),
        sa.PrimaryKeyConstraint("notice_id"),
        sa.UniqueConstraint("link_id"),
    )
    op.execute(
        f"""
        CREATE TRIGGER {NOTICES}_only_acknowledged
        BEFORE UPDATE ON {NOTICES}
        WHEN OLD.acknowledged_at IS NOT NULL OR NEW.acknowledged_at IS NULL
          OR NEW.notice_id IS NOT OLD.notice_id
          OR NEW.link_id IS NOT OLD.link_id
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN SELECT RAISE(ABORT, 'an automatic-link notice is only acknowledged, once'); END;
        """
    )
    _no_delete(NOTICES, "automatic-link notices")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {NOTICES}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {NOTICES}_only_acknowledged")
    op.drop_table(NOTICES)
    op.execute(f"DROP TRIGGER IF EXISTS {DECISIONS}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {DECISIONS}_only_revoked")
    op.drop_index("ux_store_listing_decisions_one_active", table_name=DECISIONS)
    op.drop_table(DECISIONS)
    op.execute(f"DROP TRIGGER IF EXISTS {LINKS}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {LINKS}_only_ends")
    op.drop_index("ix_store_listing_links_group", table_name=LINKS)
    op.drop_index("ux_store_listing_links_one_active", table_name=LINKS)
    op.drop_table(LINKS)
