"""PRODUCT DB's enrichment results (ADR-0026 §5; AIF-3).

A request reuses each task's fresh results or queues one ``enrich.tasks`` job; with no AI provider
it is refused before anything is written. The read shows every task's current result with its
derived staleness. No route here calls an AI provider itself.
"""

from fastapi import APIRouter

from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.stages.products.enrichment import EnrichmentRequest, RequestView, ResultsView

router = APIRouter(prefix="/api/v1/products/{product_group_id}/enrichment", tags=["enrichment"])


@router.post("")
def request_enrichment(
    product_group_id: str, request: EnrichmentRequest, container: ContainerDep
) -> RequestView:
    return container.enrichment.request(
        product_group_id, request, correlation_id=get_correlation_id() or new_correlation_id()
    )


@router.get("")
def enrichment_results(product_group_id: str, container: ContainerDep) -> ResultsView:
    return container.enrichment.results(product_group_id)
