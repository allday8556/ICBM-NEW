"""Stable Item identities for products whose sellable variants are Atomic SKUs."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class AtomicSKUProductItem(Base):
    """One product composition sold as one stable, source-proven AtomicSKU.

    The legacy ``product_items`` identity remains the no-option owner. This additive owner keeps
    AtomicSKU identity explicit instead of silently changing that table's established meaning.
    """

    __tablename__ = "atomic_sku_product_items"
    __table_args__ = (
        UniqueConstraint("product_group_id", "composition_signature", "atomic_sku_id"),
        UniqueConstraint(
            "product_group_id", "composition_signature", "atomic_sku_selection_signature"
        ),
        CheckConstraint(_hex64("composition_signature"), name="composition_signature_hex"),
        CheckConstraint(
            _hex64("atomic_sku_selection_signature"), name="atomic_sku_selection_signature_hex"
        ),
    )

    atomic_sku_item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    composition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listing_compositions.composition_id")
    )
    composition_signature: Mapped[str] = mapped_column(String(64))
    atomic_sku_id: Mapped[str] = mapped_column(String(36), ForeignKey("atomic_skus.atomic_sku_id"))
    atomic_sku_selection_signature: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


__all__ = ["AtomicSKUProductItem"]
