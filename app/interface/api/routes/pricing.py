"""The latest recorded price of Products, for display (read-only).

The Product DB list and detail show what M4 last recorded for each Product: its newest pricing
snapshot per marketplace, as stored (``app.stages.products.latest_prices``). A GET only: nothing is
priced, pinned or chosen here.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.interface.api.deps import ContainerDep
from app.stages.products.latest_prices import LatestPrices, LatestPriceView

router = APIRouter(tags=["products"])


@router.get("/api/v1/pricing/latest")
def latest_prices(
    container: ContainerDep,
    product_group_id: Annotated[list[str] | None, Query()] = None,
) -> list[LatestPriceView]:
    return LatestPrices(container.product_store).latest(product_group_id or [])
