"""The M6.5 fulfillment rows (ADR-0025 §3, §4; migration 0052)."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


class SupplierOrder(Base):
    """The supplier order the operator placed by hand for one product order, and its tracking
    (ADR-0025 §3, §4). One per product order.

    The canonical identity — the Item, supplier and source product the order resolved or was linked
    to — is copied once and never changes (trigger). The rest is revisioned: each write carries the
    revision it read and appends a history entry. The row is never deleted.
    """

    __tablename__ = "operate_supplier_orders"
    __table_args__ = (
        CheckConstraint("basis IN ('RESOLUTION', 'ADOPTION')", name="basis_valid"),
        CheckConstraint("supplier_order_ref <> ''", name="reference_present"),
        CheckConstraint("purchase_amount >= 0", name="amount_bounded"),
        CheckConstraint("currency = 'KRW'", name="currency_krw"),
        CheckConstraint(
            "(carrier_code IS NULL) = (tracking_number IS NULL)"
            " AND (carrier_code IS NULL) = (tracking_captured_at IS NULL)",
            name="tracking_complete",
        ),
        CheckConstraint("revision >= 1", name="revision_positive"),
    )

    product_order_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("operate_orders.product_order_id"), primary_key=True
    )
    marketplace_key: Mapped[str] = mapped_column(String(40))
    basis: Mapped[str] = mapped_column(String(20))
    item_id: Mapped[str] = mapped_column(String(36))
    supplier_key: Mapped[str] = mapped_column(String(40))
    source_product_id: Mapped[str] = mapped_column(String(200))
    supplier_order_ref: Mapped[str] = mapped_column(String(100))
    purchase_amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    ordered_at: Mapped[datetime] = mapped_column(UTCDateTime)
    carrier_code: Mapped[str | None] = mapped_column(String(40))
    tracking_number: Mapped[str | None] = mapped_column(String(50))
    tracking_captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revision: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SupplierOrderEntry(Base):
    """One write of a supplier order, as it stood after the write. Append-only (triggers)."""

    __tablename__ = "operate_supplier_order_history"
    __table_args__ = (
        Index(
            "ux_operate_supplier_order_history_revision",
            "product_order_id",
            "revision",
            unique=True,
        ),
        CheckConstraint(
            "action IN ('RECORDED', 'AMENDED', 'TRACKING_CAPTURED', 'TRACKING_AMENDED')",
            name="action_valid",
        ),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    entry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_order_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("operate_supplier_orders.product_order_id")
    )
    revision: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20))
    supplier_order_ref: Mapped[str] = mapped_column(String(100))
    purchase_amount: Mapped[int] = mapped_column(Integer)
    ordered_at: Mapped[datetime] = mapped_column(UTCDateTime)
    carrier_code: Mapped[str | None] = mapped_column(String(40))
    tracking_number: Mapped[str | None] = mapped_column(String(50))
    actor: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)
