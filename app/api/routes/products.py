"""PRODUCT DB: read back one canonical Product (M4 PR-C, ADR-0013).

Read-only. There is no write endpoint: a Product is materialized from durably RECORDED source
truth by the product owner, never from a request. What comes back is persisted canonical state,
with no price, no readiness and no marketplace shape.
"""

from fastapi import APIRouter

from app.api.deps import ContainerDep
from app.products.contracts import ProductView, product_view

router = APIRouter(tags=["products"])


@router.get("/api/v1/products/{product_group_id}")
def product(product_group_id: str, container: ContainerDep) -> ProductView:
    return product_view(container.products.product(product_group_id))


@router.get("/api/v1/products/by-source/{supplier_key}/{source_product_id}")
def product_of_source(
    supplier_key: str, source_product_id: str, container: ContainerDep
) -> ProductView:
    return product_view(container.products.product_of_source(supplier_key, source_product_id))
