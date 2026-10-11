"""The supplier sales-policy routes (ADR-0039 §1, §4).

They read and save one supplier's sales policy — whether ICBM applies the supplier's channel
restrictions and its minimum resale price — for the supplier card (수집관리 › 공급처 관리). A save
names the revision it was edited from; a moved policy is refused (``SALES_POLICY_REVISION_MOVED``),
never merged. A PUT carries the client header like every state-changing request (the global CSRF
guard), and the configured operator is the audited actor. Nothing here reaches a supplier or a
marketplace, and no fact changes: only whether REGISTER and pricing enforce it.
"""

from fastapi import APIRouter

from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.stages.products.sales_policy import SalesPolicyView, SaveSalesPolicyRequest

router = APIRouter(prefix="/api/v1/suppliers", tags=["suppliers"])


@router.get("/{supplier_key}/sales-policy")
def sales_policy(supplier_key: str, container: ContainerDep) -> SalesPolicyView:
    """The supplier's current policy: its newest revision, or the defaults (both applied)."""
    return container.sales_policies.policy(supplier_key)


@router.put("/{supplier_key}/sales-policy")
def save_sales_policy(
    supplier_key: str, body: SaveSalesPolicyRequest, container: ContainerDep
) -> SalesPolicyView:
    """Append one audited revision, or change nothing."""
    return container.sales_policies.save(
        supplier_key,
        body,
        actor=container.config.operator_actor,
        correlation_id=get_correlation_id() or new_correlation_id(),
    )
