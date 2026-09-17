"""COLLECT: submit one product, and read back what was stored (ADR-0010 §3, §6–§9).

Submitting accepts exactly one product URL and answers with the run's durable identity; the work
itself happens in a ``collect.*`` job, never inline in a request. Everything else here is
read-only: what comes back is what the database holds, so the same revision can be compared field
for field through either path.
"""

from fastapi import APIRouter, status

from app.api.deps import ContainerDep
from app.collect.contracts import (
    CollectionRequest,
    CollectionRunView,
    RevisionHistoryView,
    RevisionView,
    SubmittedCollectionView,
)

router = APIRouter(tags=["collect"])


@router.post("/api/v1/collect/collections", status_code=status.HTTP_202_ACCEPTED)
def submit(request: CollectionRequest, container: ContainerDep) -> SubmittedCollectionView:
    """Accept one product URL. The response is an identity to follow, not a result."""
    submitted = container.collection.submit(request.supplier_key, request.product_url)
    return SubmittedCollectionView(
        collection_run_id=submitted.collection_run_id,
        job_id=submitted.job_id,
        correlation_id=submitted.correlation_id,
    )


@router.get("/api/v1/collect/collections/{collection_run_id}")
def collection_run(collection_run_id: str, container: ContainerDep) -> CollectionRunView:
    run = container.collection.run(collection_run_id)
    return CollectionRunView(
        collection_run_id=run.collection_run_id,
        job_id=run.job_id,
        correlation_id=run.correlation_id,
        supplier_key=run.supplier_key,
        source_url=run.source_url,
        outcome=run.outcome,
        revision_id=run.revision_id,
        facts_status=run.facts_status,
        detail=run.detail,
        requested_at=run.requested_at,
        finished_at=run.finished_at,
    )


@router.get("/api/v1/collect/revisions/{revision_id}")
def revision(revision_id: str, container: ContainerDep) -> RevisionView:
    return container.source_truth.revision(revision_id)


@router.get("/api/v1/collect/products/{supplier_key}/{source_product_id}/revisions")
def history(
    supplier_key: str, source_product_id: str, container: ContainerDep
) -> RevisionHistoryView:
    return container.source_truth.history(supplier_key, source_product_id)
