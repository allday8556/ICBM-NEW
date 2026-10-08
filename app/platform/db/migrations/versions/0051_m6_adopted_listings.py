"""M6-E adopted listings: SmartStore listings ICBM did not create, their listing-state
observations, and the immutable link of an order to one (ADR-0024).

Revision ID: 0051_m6_adopted_listings
Revises: 0050_m6_orders
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_m6_adopted_listings"
down_revision: str | None = "0050_m6_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADOPTED = "operate_adopted_listings"
OBSERVATIONS = "operate_adopted_observations"
LINKS = "operate_order_adoption_links"
IMMUTABLE = (
    "marketplace_key",
    "marketplace_product_id",
    "marketplace_channel_product_id",
    "seller_code",
    "convention",
    "supplier_key",
    "source_product_id",
    "item_id",
    "adopted_sale_status",
    "adopted_display_status",
    "adopted_by",
    "adopted_at",
    "correlation_id",
)


def _append_only(table: str, label: str) -> None:
    for verb in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_{verb.lower()}
            BEFORE {verb} ON {table}
            BEGIN SELECT RAISE(ABORT, '{label} are append-only'); END;
            """
        )


def upgrade() -> None:
    op.create_table(
        ADOPTED,
        sa.Column("adoption_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_product_id", sa.String(length=40), nullable=False),
        sa.Column("marketplace_channel_product_id", sa.String(length=40), nullable=True),
        sa.Column("seller_code", sa.String(length=40), nullable=False),
        sa.Column("convention", sa.String(length=80), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("source_product_id", sa.String(length=200), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("adopted_sale_status", sa.String(length=32), nullable=True),
        sa.Column("adopted_display_status", sa.String(length=32), nullable=True),
        sa.Column("adopted_by", sa.String(length=64), nullable=False),
        sa.Column("adopted_at", sa.DateTime(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.Column("removal_evidence", sa.String(length=80), nullable=True),
        sa.CheckConstraint("state IN ('ACTIVE', 'EXTERNALLY_REMOVED')", name="state_valid"),
        sa.CheckConstraint(
            "(state = 'EXTERNALLY_REMOVED')"
            " = (removed_at IS NOT NULL AND removal_evidence IS NOT NULL)",
            name="removed_has_evidence",
        ),
        sa.CheckConstraint(
            "seller_code <> '' AND marketplace_product_id <> ''", name="identity_present"
        ),
        sa.PrimaryKeyConstraint("adoption_id"),
    )
    op.create_index(
        "ux_operate_adopted_listings_product",
        ADOPTED,
        ["marketplace_key", "marketplace_product_id"],
        unique=True,
        sqlite_where=sa.text("state = 'ACTIVE'"),
    )
    op.create_index(
        "ux_operate_adopted_listings_source",
        ADOPTED,
        ["marketplace_key", "supplier_key", "source_product_id"],
        unique=True,
        sqlite_where=sa.text("state = 'ACTIVE'"),
    )
    changed = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in IMMUTABLE)
    op.execute(
        f"""
        CREATE TRIGGER {ADOPTED}_adoption_immutable
        BEFORE UPDATE ON {ADOPTED}
        WHEN {changed} OR OLD.state = 'EXTERNALLY_REMOVED'
        BEGIN SELECT RAISE(ABORT, 'an adoption is never rewritten'); END;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {ADOPTED}_no_delete
        BEFORE DELETE ON {ADOPTED}
        BEGIN SELECT RAISE(ABORT, 'adoptions are kept as history'); END;
        """
    )

    op.create_table(
        OBSERVATIONS,
        sa.Column("observation_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("adoption_id", sa.String(length=36), nullable=False),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("sale_status", sa.String(length=32), nullable=True),
        sa.Column("display_status", sa.String(length=32), nullable=True),
        sa.Column("sale_price", sa.Integer(), nullable=True),
        sa.Column("stock_quantity", sa.Integer(), nullable=True),
        sa.Column("seller_code_matches", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "result IN ('OBSERVED', 'NOT_FOUND', 'READ_FAILED')", name="result_valid"
        ),
        sa.CheckConstraint(
            "result = 'OBSERVED' OR (sale_status IS NULL AND display_status IS NULL"
            " AND sale_price IS NULL AND stock_quantity IS NULL AND seller_code_matches IS NULL)",
            name="only_observed_carries_fields",
        ),
        sa.CheckConstraint(
            "(result = 'READ_FAILED') = (error_code IS NOT NULL)", name="failure_has_code"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["operate_listing_sync_runs.run_id"]),
        sa.ForeignKeyConstraint(["adoption_id"], [f"{ADOPTED}.adoption_id"]),
        sa.PrimaryKeyConstraint("observation_id"),
    )
    op.create_index(
        "ix_operate_adopted_observations_adoption",
        OBSERVATIONS,
        ["adoption_id", "observed_at"],
        unique=False,
    )
    _append_only(OBSERVATIONS, "adopted listing observations")

    op.create_table(
        LINKS,
        sa.Column("product_order_id", sa.String(length=40), nullable=False),
        sa.Column("adoption_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("source_product_id", sa.String(length=200), nullable=False),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["product_order_id"], ["operate_orders.product_order_id"]),
        sa.ForeignKeyConstraint(["adoption_id"], [f"{ADOPTED}.adoption_id"]),
        sa.PrimaryKeyConstraint("product_order_id"),
    )
    _append_only(LINKS, "order adoption links")


def downgrade() -> None:
    for table in (LINKS, OBSERVATIONS):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
    op.drop_table(LINKS)
    op.drop_index("ix_operate_adopted_observations_adoption", table_name=OBSERVATIONS)
    op.drop_table(OBSERVATIONS)
    op.execute(f"DROP TRIGGER IF EXISTS {ADOPTED}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {ADOPTED}_adoption_immutable")
    op.drop_index("ux_operate_adopted_listings_source", table_name=ADOPTED)
    op.drop_index("ux_operate_adopted_listings_product", table_name=ADOPTED)
    op.drop_table(ADOPTED)
