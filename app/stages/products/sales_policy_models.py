"""Persistence of the per-supplier sales policy (ADR-0039 §1; migration 0060).

One table, ``supplier_sales_policy_revisions``: the append-only revisions of one supplier's sales
policy, keyed by ``supplier_key``. The newest revision is the supplier's policy; a supplier with no
revision uses the defaults (both switches applied, SP-02). A revision is never updated or deleted,
and its number opens at one and moves by exactly one per supplier.

``app.stages.products.sales_policy.SalesPolicyStore`` is the only writer.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class SupplierSalesPolicyRevision(Base):
    __tablename__ = "supplier_sales_policy_revisions"
    __table_args__ = (
        UniqueConstraint("supplier_key", "revision_no"),
        CheckConstraint("supplier_key <> ''", name="supplier_key_present"),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint(
            "apply_channel_restrictions IN (0, 1) AND apply_minimum_price IN (0, 1)",
            name="switches_are_flags",
        ),
        CheckConstraint(_hex64("fingerprint"), name="fingerprint_hex"),
        CheckConstraint("changed_by <> ''", name="changed_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(String(64))
    revision_no: Mapped[int] = mapped_column(Integer)
    apply_channel_restrictions: Mapped[bool] = mapped_column(Boolean)
    apply_minimum_price: Mapped[bool] = mapped_column(Boolean)
    # SHA-256 of the canonical content (``sales_policy.CONTENT_VERSION``), never of request bytes.
    fingerprint: Mapped[str] = mapped_column(String(64))
    changed_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime)
