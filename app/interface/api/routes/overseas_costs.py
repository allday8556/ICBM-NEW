"""The per-product landed-cost input routes of a 구매대행 source (ADR-0038 §4, §8 phase 1).

- ``GET``/``PUT /api/v1/products/by-source/{supplier_key}/{source_product_id}/overseas-costs``:
  one overseas source product's own overseas shipping and duty rate, each ``null`` when the
  settings default applies. A source product the Product DB does not hold is 404
  (``OVERSEAS_COSTS_SOURCE_UNKNOWN``); a save names the revision it was edited from, and a moved
  or unchanged one is refused with 409 (``OVERSEAS_COSTS_REVISION_MOVED``,
  ``OVERSEAS_COSTS_UNCHANGED``), never merged; a malformed body is 422.

A PUT carries the client header like every state-changing request (the global CSRF guard), and the
configured operator is the audited actor. Nothing here prices anything: a change makes the
product's current foreign price STALE through the pricing owner's dependency fingerprint, and the
next pricing records a new snapshot.
"""

from fastapi import APIRouter

from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.stages.products.overseas_costs import OverseasCostsView, SaveOverseasCostsRequest

router = APIRouter(prefix="/api/v1/products/by-source", tags=["products"])


@router.get("/{supplier_key}/{source_product_id}/overseas-costs")
def overseas_costs(
    supplier_key: str, source_product_id: str, container: ContainerDep
) -> OverseasCostsView:
    """The source product's inputs now: its newest revision, or none (the settings defaults)."""
    return container.overseas_costs.current(supplier_key, source_product_id)


@router.put("/{supplier_key}/{source_product_id}/overseas-costs")
def save_overseas_costs(
    supplier_key: str,
    source_product_id: str,
    body: SaveOverseasCostsRequest,
    container: ContainerDep,
) -> OverseasCostsView:
    """Append one audited revision, or change nothing."""
    return container.overseas_costs.save(
        supplier_key,
        source_product_id,
        body,
        actor=container.config.operator_actor,
        correlation_id=get_correlation_id() or new_correlation_id(),
    )
