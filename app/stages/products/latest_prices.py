"""The latest recorded price of each Product, per marketplace, for display only.

The Product DB's own read models carry no price (Gate 1 G1-C, ADR-0015 §5): they say what the
Product is. This is the pricing owner's separate read of what M4 last recorded for it: the newest
pricing snapshot of each Product for each marketplace, its amounts exactly as stored. It prices
nothing, chooses no snapshot for a registration and says nothing about whether that snapshot still
prices the current state — a Draft pins and checks its own price (``ProductPricingService``).
"""

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select

from app.platform.core.errors import InputValidationError
from app.stages.products.models import PricingSnapshot
from app.stages.products.store import ProductFoundationStore

# One screen page of Products at a time (the Product DB pages by 20).
MAX_PRODUCTS = 50


class LatestPriceView(BaseModel):
    """One Product's newest pricing snapshot for one marketplace, as recorded."""

    product_group_id: str
    marketplace_key: str
    item_id: str
    purchase_cost_krw: int
    supplier_shipping_krw: int
    minimum_sale_price_krw: int | None
    final_sale_price_krw: int
    price_basis: str
    expected_net_margin_bp: int
    source_currency: str | None
    source_price: str | None
    created_at: datetime


class LatestPrices:
    """Reads only: a read-only unit of work over the Product store's database."""

    def __init__(self, store: ProductFoundationStore) -> None:
        self._store = store

    def latest(self, product_group_ids: Sequence[str]) -> list[LatestPriceView]:
        wanted = sorted(set(product_group_ids))
        if len(wanted) > MAX_PRODUCTS:
            raise InputValidationError(
                "PRICING_LATEST_TOO_MANY", f"at most {MAX_PRODUCTS} products per read"
            )
        if not wanted:
            return []
        with self._store.reading() as unit:
            rows = unit.session.scalars(
                select(PricingSnapshot)
                .where(PricingSnapshot.product_group_id.in_(wanted))
                .order_by(
                    PricingSnapshot.created_at.desc(), PricingSnapshot.pricing_snapshot_id.desc()
                )
            ).all()
            seen: set[tuple[str, str]] = set()
            found = []
            for row in rows:
                key = (row.product_group_id, row.marketplace_key)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    LatestPriceView(
                        product_group_id=row.product_group_id,
                        marketplace_key=row.marketplace_key,
                        item_id=row.item_id,
                        purchase_cost_krw=row.purchase_cost_krw,
                        supplier_shipping_krw=row.supplier_shipping_krw,
                        minimum_sale_price_krw=row.minimum_sale_price_krw,
                        final_sale_price_krw=row.final_sale_price_krw,
                        price_basis=row.price_basis,
                        expected_net_margin_bp=row.expected_net_margin_bp,
                        source_currency=row.source_currency,
                        source_price=row.source_price,
                        created_at=row.created_at,
                    )
                )
        return found
