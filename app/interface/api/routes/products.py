"""PRODUCT DB: read back canonical Products (M4 PR-C, ADR-0013; Gate 1 G1-C, ADR-0015 §5).

Read-only. There is no write endpoint: a Product is materialized from durably RECORDED source
truth by the product owner, never from a request. What comes back is persisted canonical state,
with no price, no readiness and no marketplace shape.

G1-C adds the operator's list, detail and registration-target check. Every one is a GET: the
selection check revalidates what the operator chose and records nothing. The operator's image
decisions have their own routes (``product_images.py``). ADR-0039 adds the note a sales-policy
warning quotes: the supplier and its own words, read, never decided.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.interface.api.deps import ContainerDep
from app.stages.products.contracts import (
    ProductDetailView,
    ProductPageView,
    ProductView,
    RegistrationTargetView,
    product_view,
)
from app.stages.products.sales_policy_notes import SalesPolicyNotes, SalesPolicyNoteView

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


@router.get("/api/v1/products/items/{item_id}/sales-policy-note")
def sales_policy_note(item_id: str, container: ContainerDep) -> SalesPolicyNoteView:
    """ADR-0039 §2, §3: the supplier and its own words that a policy-not-applied warning on this
    Item quotes. A read only: it decides no warning and changes no preparation. Stateless over
    the Product store and the supplier catalog's names, so it is made per request."""
    return SalesPolicyNotes(container.product_store, container.supplier_names).note(item_id)
