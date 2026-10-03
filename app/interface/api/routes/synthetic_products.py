"""An operator's synthetic test products (owner decision 2026-10-03).

The route hands the operator's request to the owner (``SyntheticTestProductService``), which
copies one collected revision's facts, unchanged, under the reserved ``icbm-synthetic`` namespace
and labels the copy durably. Nothing here invents a fact or acquires a document.
"""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.stages.collect.synthetic import SyntheticRecord

router = APIRouter(prefix="/api/v1/collect/synthetic-test-products", tags=["collect"])


class SyntheticTestProductRequest(BaseModel):
    template_revision_id: str = Field(min_length=1, max_length=36)
    label: str = Field(min_length=1, max_length=40)
    reason: str | None = Field(default=None, max_length=500)
    actor: str = Field(min_length=1, max_length=64)


def _view(record: SyntheticRecord) -> dict[str, Any]:
    return {
        "synthetic_id": record.synthetic_id,
        "supplier_key": record.supplier_key,
        "source_product_id": record.source_product_id,
        "template_revision_id": record.template_revision_id,
        "template_supplier_key": record.template_supplier_key,
        "template_source_product_id": record.template_source_product_id,
        "collection_run_id": record.collection_run_id,
        "label": record.label,
        "reason": record.reason,
        "created_by": record.created_by,
        "created_at": record.created_at.isoformat(),
    }


@router.get("")
def list_synthetic_test_products(container: ContainerDep) -> list[dict[str, Any]]:
    return [_view(record) for record in container.synthetic_products.list()]


@router.post("")
def create_synthetic_test_product(
    request: SyntheticTestProductRequest, container: ContainerDep
) -> dict[str, Any]:
    record = container.synthetic_products.create(
        template_revision_id=request.template_revision_id,
        label=request.label,
        reason=request.reason,
        actor=request.actor,
        correlation_id=get_correlation_id() or new_correlation_id(),
    )
    return _view(record)
