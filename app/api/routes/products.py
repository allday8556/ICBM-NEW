"""PRODUCT DB: read back canonical Products (M4 PR-C, ADR-0013; Gate 1 G1-C, ADR-0015 §5).

Read-only. There is no write endpoint: a Product is materialized from durably RECORDED source
truth by the product owner, never from a request. What comes back is persisted canonical state,
with no price, no readiness and no marketplace shape.

G1-C adds the operator's list, detail and registration-target check. Every one is a GET: the
selection check revalidates what the operator chose and records nothing.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ContainerDep
from app.products.contracts import (
    ProductDetailView,
    ProductPageView,
    ProductView,
    RegistrationTargetView,
    product_view,
)

router = APIRouter(tags=["products"])


@router.get("/api/v1/products")
def products(
    container: ContainerDep,
    q: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> ProductPageView:
    return container.products.page(query=q, cursor=cursor, limit=limit)


@router.get("/api/v1/products/{product_group_id}")
def product(product_group_id: str, container: ContainerDep) -> ProductView:
    return product_view(container.products.product(product_group_id))


@router.get("/api/v1/products/{product_group_id}/detail")
def product_detail(product_group_id: str, container: ContainerDep) -> ProductDetailView:
    return container.products.detail(product_group_id)


@router.get("/api/v1/products/{product_group_id}/registration-target")
def registration_target(
    product_group_id: str,
    container: ContainerDep,
    membership_revision_id: str | None = None,
    item_id: Annotated[list[str] | None, Query()] = None,
) -> RegistrationTargetView:
    return container.products.registration_target(product_group_id, membership_revision_id, item_id)


@router.get("/api/v1/products/by-source/{supplier_key}/{source_product_id}")
def product_of_source(
    supplier_key: str, source_product_id: str, container: ContainerDep
) -> ProductView:
    return product_view(container.products.product_of_source(supplier_key, source_product_id))
