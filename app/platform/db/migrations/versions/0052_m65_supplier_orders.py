"""M6.5-A supplier orders: the supplier order the operator placed by hand for one product order,
its tracking, and their append-only history (ADR-0025 §3, §4).

Revision ID: 0052_m65_supplier_orders
Revises: 0051_m6_adopted_listings
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_m65_supplier_orders"
down_revision: str | None = "0051_m6_adopted_listings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORDERS = "operate_supplier_orders"
HISTORY = "operate_supplier_order_history"
# The canonical identity a supplier order copies once (ADR-0025 §3, M65-01).
IDENTITY = (
    "marketplace_key",
    "basis",
    "item_id",
    "supplier_key",
    "source_product_id",
    "created_by",
    "created_at",
)


def upgrade() -> None:
    op.create_table(
        ORDERS,
        sa.Column("product_order_id", sa.String(length=40), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("basis", sa.String(length=20), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("source_product_id", sa.String(length=200), nullable=False),
        sa.Column("supplier_order_ref", sa.String(length=100), nullable=False),
        sa.Column("purchase_amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("ordered_at", sa.DateTime(), nullable=False),
        sa.Column("carrier_code", sa.String(length=40), nullable=True),
        sa.Column("tracking_number", sa.String(length=50), nullable=True),
        sa.Column("tracking_captured_at", sa.DateTime(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("basis IN ('RESOLUTION', 'ADOPTION')", name="basis_valid"),
        sa.CheckConstraint("supplier_order_ref <> ''", name="reference_present"),
        sa.CheckConstraint("purchase_amount >= 0", name="amount_bounded"),
        sa.CheckConstraint("currency = 'KRW'", name="currency_krw"),
        sa.CheckConstraint(
            "(carrier_code IS NULL) = (tracking_number IS NULL)"
            " AND (carrier_code IS NULL) = (tracking_captured_at IS NULL)",
            name="tracking_complete",
        ),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        sa.ForeignKeyConstraint(["product_order_id"], ["operate_orders.product_order_id"]),
        sa.PrimaryKeyConstraint("product_order_id"),
    )
    changed = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in IDENTITY)
    op.execute(
        f"""
        CREATE TRIGGER {ORDERS}_identity_immutable
        BEFORE UPDATE ON {ORDERS}
        WHEN {changed} OR NEW.revision <> OLD.revision + 1
        BEGIN SELECT RAISE(ABORT, 'a supplier order keeps its identity and its revision order'); END;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {ORDERS}_no_delete
        BEFORE DELETE ON {ORDERS}
        BEGIN SELECT RAISE(ABORT, 'supplier orders are kept as history'); END;
        """
    )

    op.create_table(
        HISTORY,
        sa.Column("entry_id", sa.String(length=36), nullable=False),
        sa.Column("product_order_id", sa.String(length=40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("supplier_order_ref", sa.String(length=100), nullable=False),
        sa.Column("purchase_amount", sa.Integer(), nullable=False),
        sa.Column("ordered_at", sa.DateTime(), nullable=False),
        sa.Column("carrier_code", sa.String(length=40), nullable=True),
        sa.Column("tracking_number", sa.String(length=50), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "action IN ('RECORDED', 'AMENDED', 'TRACKING_CAPTURED', 'TRACKING_AMENDED')",
            name="action_valid",
        ),
        sa.CheckConstraint("correlation_id <> ''", name="correlation_present"),
        sa.ForeignKeyConstraint(["product_order_id"], [f"{ORDERS}.product_order_id"]),
        sa.PrimaryKeyConstraint("entry_id"),
    )
    op.create_index(
        "ux_operate_supplier_order_history_revision",
        HISTORY,
        ["product_order_id", "revision"],
        unique=True,
    )
    for verb in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER {HISTORY}_no_{verb.lower()}
            BEFORE {verb} ON {HISTORY}
            BEGIN SELECT RAISE(ABORT, 'supplier order history is append-only'); END;
            """
        )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {HISTORY}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {HISTORY}_no_update")
    op.drop_index("ux_operate_supplier_order_history_revision", table_name=HISTORY)
    op.drop_table(HISTORY)
    op.execute(f"DROP TRIGGER IF EXISTS {ORDERS}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {ORDERS}_identity_immutable")
    op.drop_table(ORDERS)
