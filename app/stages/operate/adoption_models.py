"""The M6-E adopted-listing rows (ADR-0024; migration 0051)."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


class AdoptedListing(Base):
    """One SmartStore listing ICBM did not create, linked to a COLLECT source product and its
    canonical Item by an owner-declared seller-code convention (ADR-0024 §1–§3).

    The adoption columns never change (trigger); the only change is ``ACTIVE`` →
    ``EXTERNALLY_REMOVED`` on provider evidence. One ``ACTIVE`` adoption per marketplace product
    and per marketplace × source product.
    """

    __tablename__ = "operate_adopted_listings"
    __table_args__ = (
        Index(
            "ux_operate_adopted_listings_product",
            "marketplace_key",
            "marketplace_product_id",
            unique=True,
            sqlite_where=sql("state = 'ACTIVE'"),
        ),
        Index(
            "ux_operate_adopted_listings_source",
            "marketplace_key",
            "supplier_key",
            "source_product_id",
            unique=True,
            sqlite_where=sql("state = 'ACTIVE'"),
        ),
        CheckConstraint("state IN ('ACTIVE', 'EXTERNALLY_REMOVED')", name="state_valid"),
        CheckConstraint(
            "(state = 'EXTERNALLY_REMOVED')"
            " = (removed_at IS NOT NULL AND removal_evidence IS NOT NULL)",
            name="removed_has_evidence",
        ),
        CheckConstraint(
            "seller_code <> '' AND marketplace_product_id <> ''", name="identity_present"
        ),
    )

    adoption_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    marketplace_product_id: Mapped[str] = mapped_column(String(40))
    marketplace_channel_product_id: Mapped[str | None] = mapped_column(String(40))
    seller_code: Mapped[str] = mapped_column(String(40))
    convention: Mapped[str] = mapped_column(String(80))
    supplier_key: Mapped[str] = mapped_column(String(40))
    source_product_id: Mapped[str] = mapped_column(String(200))
    item_id: Mapped[str] = mapped_column(String(36))
    adopted_sale_status: Mapped[str | None] = mapped_column(String(32))
    adopted_display_status: Mapped[str | None] = mapped_column(String(32))
    adopted_by: Mapped[str] = mapped_column(String(64))
    adopted_at: Mapped[datetime] = mapped_column(UTCDateTime)
    correlation_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    removal_evidence: Mapped[str | None] = mapped_column(String(80))


class AdoptedObservation(Base):
    """What one listing-state read showed for one adopted listing. Append-only (triggers)."""

    __tablename__ = "operate_adopted_observations"
    __table_args__ = (
        Index("ix_operate_adopted_observations_adoption", "adoption_id", "observed_at"),
        CheckConstraint("result IN ('OBSERVED', 'NOT_FOUND', 'READ_FAILED')", name="result_valid"),
        CheckConstraint(
            "result = 'OBSERVED' OR (sale_status IS NULL AND display_status IS NULL"
            " AND sale_price IS NULL AND stock_quantity IS NULL AND seller_code_matches IS NULL)",
            name="only_observed_carries_fields",
        ),
        CheckConstraint(
            "(result = 'READ_FAILED') = (error_code IS NOT NULL)", name="failure_has_code"
        ),
    )

    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("operate_listing_sync_runs.run_id"))
    adoption_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("operate_adopted_listings.adoption_id")
    )
    result: Mapped[str] = mapped_column(String(20))
    sale_status: Mapped[str | None] = mapped_column(String(32))
    display_status: Mapped[str | None] = mapped_column(String(32))
    sale_price: Mapped[int | None] = mapped_column(Integer)
    stock_quantity: Mapped[int | None] = mapped_column(Integer)
    seller_code_matches: Mapped[bool | None] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
