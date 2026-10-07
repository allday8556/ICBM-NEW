"""M6-D orders: ingest runs, product orders with their immutable resolution and encrypted shipping
record, and the append-only status history (ADR-0023 §5, §7).

Revision ID: 0050_m6_orders
Revises: 0049_m6_stock_recheck
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_m6_orders"
down_revision: str | None = "0049_m6_stock_recheck"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "operate_order_sync_runs"
ORDERS = "operate_orders"
HISTORY = "operate_order_status_history"
RESOLUTION = (
    "resolution",
    "registration_id",
    "registration_item_key",
    "item_id",
    "source_binding_id",
    "supplier_key",
    "source_product_id",
    "resolved_at",
)


def upgrade() -> None:
    op.create_table(
        RUNS,
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("window_from", sa.DateTime(), nullable=False),
        sa.Column("synced_until", sa.DateTime(), nullable=True),
        sa.Column("changes", sa.Integer(), nullable=False),
        sa.Column("orders_read", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("trigger IN ('AUTO', 'OPERATOR')", name="trigger_valid"),
        sa.CheckConstraint("state IN ('RUNNING', 'FINISHED')", name="state_valid"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('COMPLETED', 'SESSION_UNAVAILABLE', 'RATE_LIMITED',"
            " 'FAILED', 'INTERRUPTED')",
            name="outcome_valid",
        ),
        sa.CheckConstraint(
            "(state = 'FINISHED') = (finished_at IS NOT NULL AND outcome IS NOT NULL)",
            name="finished_has_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'FAILED') = (error_code IS NOT NULL)", name="failure_has_code"
        ),
        sa.CheckConstraint("changes >= 0 AND orders_read >= 0", name="counts_bounded"),
        sa.CheckConstraint("correlation_id <> ''", name="correlation_present"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ux_operate_order_sync_runs_one_running",
        RUNS,
        ["state"],
        unique=True,
        sqlite_where=sa.text("state = 'RUNNING'"),
    )
    op.create_index("ix_operate_order_sync_runs_started", RUNS, ["started_at"], unique=False)

    op.create_table(
        ORDERS,
        sa.Column("product_order_id", sa.String(length=40), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("order_id", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=True),
        sa.Column("claim_type", sa.String(length=40), nullable=True),
        sa.Column("claim_status", sa.String(length=40), nullable=True),
        sa.Column("place_order_status", sa.String(length=20), nullable=True),
        sa.Column("ordered_at", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("shipping_due_at", sa.DateTime(), nullable=True),
        sa.Column("channel_product_id", sa.String(length=40), nullable=True),
        sa.Column("original_product_id", sa.String(length=40), nullable=True),
        sa.Column("option_manage_code", sa.String(length=200), nullable=True),
        sa.Column("seller_product_code", sa.String(length=200), nullable=True),
        sa.Column("product_name", sa.String(length=400), nullable=True),
        sa.Column("product_option", sa.String(length=400), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("unit_price", sa.Integer(), nullable=True),
        sa.Column("total_payment_amount", sa.Integer(), nullable=True),
        sa.Column("delivery_method", sa.String(length=40), nullable=True),
        sa.Column("delivery_attribute_type", sa.String(length=40), nullable=True),
        sa.Column("delivery_status", sa.String(length=40), nullable=True),
        sa.Column("delivery_company", sa.String(length=40), nullable=True),
        sa.Column("tracking_number", sa.String(length=100), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("resolution", sa.String(length=20), nullable=False),
        sa.Column("registration_id", sa.String(length=36), nullable=True),
        sa.Column("registration_item_key", sa.String(length=200), nullable=True),
        sa.Column("item_id", sa.String(length=36), nullable=True),
        sa.Column("source_binding_id", sa.String(length=36), nullable=True),
        sa.Column("supplier_key", sa.String(length=40), nullable=True),
        sa.Column("source_product_id", sa.String(length=200), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=False),
        sa.Column("shipping_state", sa.String(length=20), nullable=False),
        sa.Column("shipping_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("recipient_masked", sa.String(length=40), nullable=True),
        sa.Column("phone_masked", sa.String(length=40), nullable=True),
        sa.Column("shipping_deleted_at", sa.DateTime(), nullable=True),
        sa.Column("terminal_at", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "resolution IN ('MATCHED', 'UNMATCHED', 'ITEM_UNMATCHED', 'CONFLICT')",
            name="resolution_valid",
        ),
        sa.CheckConstraint(
            "(resolution = 'MATCHED') = (registration_item_key IS NOT NULL AND item_id IS NOT NULL)",
            name="matched_names_item",
        ),
        sa.CheckConstraint(
            "resolution = 'UNMATCHED' OR registration_id IS NOT NULL",
            name="matched_names_registration",
        ),
        sa.CheckConstraint(
            "shipping_state IN ('STORED', 'NONE', 'DELETED')", name="shipping_state_valid"
        ),
        sa.CheckConstraint(
            "(shipping_state = 'STORED') = (shipping_ciphertext IS NOT NULL)",
            name="stored_has_ciphertext",
        ),
        sa.CheckConstraint(
            "(shipping_state = 'DELETED') = (shipping_deleted_at IS NOT NULL)",
            name="deleted_has_time",
        ),
        sa.CheckConstraint("marketplace_key <> ''", name="marketplace_present"),
        sa.ForeignKeyConstraint(["registration_id"], ["marketplace_registrations.registration_id"]),
        sa.PrimaryKeyConstraint("product_order_id"),
    )
    op.create_index("ix_operate_orders_changed", ORDERS, ["last_changed_at"], unique=False)
    op.create_index("ix_operate_orders_registration", ORDERS, ["registration_id"], unique=False)
    changed = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in RESOLUTION)
    op.execute(
        f"""
        CREATE TRIGGER {ORDERS}_resolution_immutable
        BEFORE UPDATE ON {ORDERS}
        WHEN {changed}
        BEGIN SELECT RAISE(ABORT, 'an order resolution is immutable once recorded'); END;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {ORDERS}_no_delete
        BEFORE DELETE ON {ORDERS}
        BEGIN SELECT RAISE(ABORT, 'orders are kept as business history'); END;
        """
    )

    op.create_table(
        HISTORY,
        sa.Column("entry_id", sa.String(length=36), nullable=False),
        sa.Column("product_order_id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("change_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=True),
        sa.Column("claim_type", sa.String(length=40), nullable=True),
        sa.Column("claim_status", sa.String(length=40), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("change_type <> ''", name="change_type_present"),
        sa.ForeignKeyConstraint(["product_order_id"], [f"{ORDERS}.product_order_id"]),
        sa.ForeignKeyConstraint(["run_id"], [f"{RUNS}.run_id"]),
        sa.PrimaryKeyConstraint("entry_id"),
    )
    op.create_index(
        "ux_operate_order_status_history_change",
        HISTORY,
        ["product_order_id", "changed_at", "change_type"],
        unique=True,
    )
    for verb in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER {HISTORY}_no_{verb.lower()}
            BEFORE {verb} ON {HISTORY}
            BEGIN SELECT RAISE(ABORT, 'order status history is append-only'); END;
            """
        )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {HISTORY}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {HISTORY}_no_update")
    op.drop_index("ux_operate_order_status_history_change", table_name=HISTORY)
    op.drop_table(HISTORY)
    op.execute(f"DROP TRIGGER IF EXISTS {ORDERS}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {ORDERS}_resolution_immutable")
    op.drop_index("ix_operate_orders_registration", table_name=ORDERS)
    op.drop_index("ix_operate_orders_changed", table_name=ORDERS)
    op.drop_table(ORDERS)
    op.drop_index("ix_operate_order_sync_runs_started", table_name=RUNS)
    op.drop_index("ux_operate_order_sync_runs_one_running", table_name=RUNS)
    op.drop_table(RUNS)
