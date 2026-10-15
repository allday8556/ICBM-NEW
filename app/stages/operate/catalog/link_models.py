"""The ListingLinks rows (ADR-0042 §7, §12; migration 0068).

- ``store_listing_links``: one store listing linked to one ``ProductGroup`` — the common product.
  Append-only: the only change is ``ACTIVE → ENDED`` (trigger). At most one ``ACTIVE`` link per
  store listing. ``IMPORTED`` links a listing to the common product created for it,
  ``STRONG_KEY`` is the server's automatic seller-code or barcode link, ``USER_CONFIRMED`` the
  seller's 「같은 상품」. An ICBM registration and an ADR-0024 adoption are read as links from
  their own owners and are never stored here.
- ``store_listing_decisions``: the seller's 「다른 상품」 for one listing and one common product;
  revocable once (trigger).
- ``store_link_notices``: one notice per automatic link; acknowledged once (trigger).
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


class StoreListingLink(Base):
    """One store listing → one common product. Ends once; never rewritten."""

    __tablename__ = "store_listing_links"
    __table_args__ = (
        Index(
            "ux_store_listing_links_one_active",
            "listing_id",
            unique=True,
            sqlite_where=sql("state = 'ACTIVE'"),
        ),
        Index("ix_store_listing_links_group", "product_group_id", "state"),
        CheckConstraint("kind IN ('IMPORTED', 'STRONG_KEY', 'USER_CONFIRMED')", name="kind_valid"),
        CheckConstraint(
            "(kind = 'STRONG_KEY') = (key_kind IS NOT NULL)", name="strong_key_names_its_key"
        ),
        CheckConstraint(
            "key_kind IS NULL OR key_kind IN ('SELLER_CODE', 'GTIN')", name="key_kind_valid"
        ),
        CheckConstraint("state IN ('ACTIVE', 'ENDED')", name="state_valid"),
        CheckConstraint(
            "(state = 'ENDED') = (ended_at IS NOT NULL)"
            " AND (ended_at IS NULL) = (ended_by IS NULL)"
            " AND (ended_at IS NULL) = (end_reason IS NULL)",
            name="ended_is_recorded",
        ),
        CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('UNDO', 'UNLINK', 'MERGED')",
            name="end_reason_valid",
        ),
        CheckConstraint(
            "marketplace_key <> '' AND marketplace_account_id <> '' AND decided_by <> ''"
            " AND correlation_id <> '' AND (ended_by IS NULL OR ended_by <> '')",
            name="identity_present",
        ),
    )

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("store_catalog_listings.listing_id")
    )
    marketplace_key: Mapped[str] = mapped_column(String(40))
    marketplace_account_id: Mapped[str] = mapped_column(String(40))
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    kind: Mapped[str] = mapped_column(String(20))
    key_kind: Mapped[str | None] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(10))
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ended_by: Mapped[str | None] = mapped_column(String(64))
    end_reason: Mapped[str | None] = mapped_column(String(20))


class StoreListingDecision(Base):
    """「다른 상품」: this listing is not that common product. Revoked once, never deleted."""

    __tablename__ = "store_listing_decisions"
    __table_args__ = (
        Index(
            "ux_store_listing_decisions_one_active",
            "listing_id",
            "product_group_id",
            unique=True,
            sqlite_where=sql("revoked_at IS NULL"),
        ),
        CheckConstraint("(revoked_at IS NULL) = (revoked_by IS NULL)", name="revocation_recorded"),
        CheckConstraint(
            "decided_by <> '' AND correlation_id <> ''"
            " AND (revoked_by IS NULL OR revoked_by <> '')",
            name="identity_present",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("store_catalog_listings.listing_id")
    )
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_by: Mapped[str | None] = mapped_column(String(64))


class StoreLinkNotice(Base):
    """The notice of one automatic link (「자동 연결됨 · 되돌리기」). Acknowledged once."""

    __tablename__ = "store_link_notices"
    __table_args__ = (
        UniqueConstraint("link_id"),
        CheckConstraint(
            "(acknowledged_at IS NULL) = (acknowledged_by IS NULL)"
            " AND (acknowledged_by IS NULL OR acknowledged_by <> '')",
            name="acknowledgement_recorded",
        ),
    )

    notice_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    link_id: Mapped[str] = mapped_column(String(36), ForeignKey("store_listing_links.link_id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
