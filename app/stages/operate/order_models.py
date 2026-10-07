"""The M6-D order rows (ADR-0023 §5, §7; migration 0050)."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


class OrderSyncRun(Base):
    """One order ingest pass, automatic or operator-requested. One at a time.

    ``synced_until`` is how far the change windows were read completely; the next pass starts
    from the latest of them, minus an overlap, so a late change is never missed (ADR-0023 §5).
    """

    __tablename__ = "operate_order_sync_runs"
    __table_args__ = (
        Index(
            "ux_operate_order_sync_runs_one_running",
            "state",
            unique=True,
            sqlite_where=sql("state = 'RUNNING'"),
        ),
        Index("ix_operate_order_sync_runs_started", "started_at"),
        CheckConstraint("trigger IN ('AUTO', 'OPERATOR')", name="trigger_valid"),
        CheckConstraint("state IN ('RUNNING', 'FINISHED')", name="state_valid"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('COMPLETED', 'SESSION_UNAVAILABLE', 'RATE_LIMITED',"
            " 'FAILED', 'INTERRUPTED')",
            name="outcome_valid",
        ),
        CheckConstraint(
            "(state = 'FINISHED') = (finished_at IS NOT NULL AND outcome IS NOT NULL)",
            name="finished_has_outcome",
        ),
        CheckConstraint("(outcome = 'FAILED') = (error_code IS NOT NULL)", name="failure_has_code"),
        CheckConstraint("changes >= 0 AND orders_read >= 0", name="counts_bounded"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20))
    outcome: Mapped[str | None] = mapped_column(String(32))
    window_from: Mapped[datetime] = mapped_column(UTCDateTime)
    synced_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    changes: Mapped[int] = mapped_column(Integer)
    orders_read: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ProductOrder(Base):
    """One provider product order: its clear fields (kept as business history), its immutable
    resolution, and its encrypted shipping record with that record's lifecycle.

    The provider's product-order id is the key (M6-09). The row is never deleted; the resolution
    columns never change once written (trigger); only ciphertext ever holds the recipient.
    """

    __tablename__ = "operate_orders"
    __table_args__ = (
        Index("ix_operate_orders_changed", "last_changed_at"),
        Index("ix_operate_orders_registration", "registration_id"),
        CheckConstraint(
            "resolution IN ('MATCHED', 'UNMATCHED', 'ITEM_UNMATCHED', 'CONFLICT')",
            name="resolution_valid",
        ),
        CheckConstraint(
            "(resolution = 'MATCHED')"
            " = (registration_item_key IS NOT NULL AND item_id IS NOT NULL)",
            name="matched_names_item",
        ),
        CheckConstraint(
            "resolution = 'UNMATCHED' OR registration_id IS NOT NULL",
            name="matched_names_registration",
        ),
        CheckConstraint(
            "shipping_state IN ('STORED', 'NONE', 'DELETED')", name="shipping_state_valid"
        ),
        CheckConstraint(
            "(shipping_state = 'STORED') = (shipping_ciphertext IS NOT NULL)",
            name="stored_has_ciphertext",
        ),
        CheckConstraint(
            "(shipping_state = 'DELETED') = (shipping_deleted_at IS NOT NULL)",
            name="deleted_has_time",
        ),
        CheckConstraint("marketplace_key <> ''", name="marketplace_present"),
    )

    product_order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    order_id: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str | None] = mapped_column(String(40))
    claim_type: Mapped[str | None] = mapped_column(String(40))
    claim_status: Mapped[str | None] = mapped_column(String(40))
    place_order_status: Mapped[str | None] = mapped_column(String(20))
    ordered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    shipping_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    channel_product_id: Mapped[str | None] = mapped_column(String(40))
    original_product_id: Mapped[str | None] = mapped_column(String(40))
    option_manage_code: Mapped[str | None] = mapped_column(String(200))
    seller_product_code: Mapped[str | None] = mapped_column(String(200))
    product_name: Mapped[str | None] = mapped_column(String(400))
    product_option: Mapped[str | None] = mapped_column(String(400))
    quantity: Mapped[int | None] = mapped_column(Integer)
    unit_price: Mapped[int | None] = mapped_column(Integer)
    total_payment_amount: Mapped[int | None] = mapped_column(Integer)
    delivery_method: Mapped[str | None] = mapped_column(String(40))
    delivery_attribute_type: Mapped[str | None] = mapped_column(String(40))
    delivery_status: Mapped[str | None] = mapped_column(String(40))
    delivery_company: Mapped[str | None] = mapped_column(String(40))
    tracking_number: Mapped[str | None] = mapped_column(String(100))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The resolution (ADR-0013 order-line resolution), immutable once recorded.
    resolution: Mapped[str] = mapped_column(String(20))
    registration_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("marketplace_registrations.registration_id")
    )
    registration_item_key: Mapped[str | None] = mapped_column(String(200))
    item_id: Mapped[str | None] = mapped_column(String(36))
    source_binding_id: Mapped[str | None] = mapped_column(String(36))
    supplier_key: Mapped[str | None] = mapped_column(String(40))
    source_product_id: Mapped[str | None] = mapped_column(String(200))
    resolved_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # The shipping record (ADR-0023 §7): AES-256-GCM ciphertext only; the masks are for lists.
    shipping_state: Mapped[str] = mapped_column(String(20))
    shipping_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    recipient_masked: Mapped[str | None] = mapped_column(String(40))
    phone_masked: Mapped[str | None] = mapped_column(String(40))
    shipping_deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # When the order first reached purchase-decided, cancelled or returned: the 90 days start.
    terminal_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class OrderStatusChange(Base):
    """One change the provider listed for one product order. Append-only (triggers); a re-read
    of the same change is the same row (M6-09)."""

    __tablename__ = "operate_order_status_history"
    __table_args__ = (
        Index(
            "ux_operate_order_status_history_change",
            "product_order_id",
            "changed_at",
            "change_type",
            unique=True,
        ),
        CheckConstraint("change_type <> ''", name="change_type_present"),
    )

    entry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_order_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("operate_orders.product_order_id")
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("operate_order_sync_runs.run_id"))
    change_type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str | None] = mapped_column(String(40))
    claim_type: Mapped[str | None] = mapped_column(String(40))
    claim_status: Mapped[str | None] = mapped_column(String(40))
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
