"""M6.5-B delivery read-back: the order's latest delivery state as the order read shows it, and the
delivery state each recorded status change was observed with (ADR-0025 §6).

Revision ID: 0053_m65_delivery_readback
Revises: 0052_m65_supplier_orders
Create Date: 2026-10-08
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0053_m65_delivery_readback"
down_revision: str | None = "0052_m65_supplier_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORDERS = "operate_orders"
HISTORY = "operate_order_status_history"
# ADR-0025 §6: exactly the delivery members the allow-list adds, all in clear (not personal data).
ORDER_COLUMNS: tuple[sa.Column[Any], ...] = (
    sa.Column("delivery_company", sa.String(length=40), nullable=True),
    sa.Column("tracking_number", sa.String(length=100), nullable=True),
    sa.Column("delivery_status", sa.String(length=40), nullable=True),
    sa.Column("sent_at", sa.DateTime(), nullable=True),
    sa.Column("picked_up_at", sa.DateTime(), nullable=True),
    sa.Column("delivered_at", sa.DateTime(), nullable=True),
    sa.Column("wrong_tracking_number", sa.Boolean(), nullable=True),
)
HISTORY_COLUMNS: tuple[sa.Column[Any], ...] = (
    sa.Column("delivery_company", sa.String(length=40), nullable=True),
    sa.Column("tracking_number", sa.String(length=100), nullable=True),
    sa.Column("delivery_status", sa.String(length=40), nullable=True),
)


def upgrade() -> None:
    # Adding nullable columns rewrites no row, so neither the resolution trigger of the orders nor
    # the append-only triggers of the history fire.
    for column in ORDER_COLUMNS:
        op.add_column(ORDERS, column)
    for column in HISTORY_COLUMNS:
        op.add_column(HISTORY, column)


def downgrade() -> None:
    for column in reversed(HISTORY_COLUMNS):
        op.drop_column(HISTORY, column.name)
    for column in reversed(ORDER_COLUMNS):
        op.drop_column(ORDERS, column.name)
