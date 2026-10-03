"""COLLECT: submit one product, and read back what was stored (ADR-0010 §3, §6–§9).

Submitting accepts exactly one product URL and answers with the run's durable identity; the work
itself happens in a ``collect.*`` job, never inline in a request. Everything else here is
read-only: what comes back is what the database holds, so the same revision can be compared field
for field through either path.

Gate 1 G1-E adds two reads for the COLLECT screen: the newest runs, so a reload follows the same
durable runs, and where a RECORDED run's revision stands in the Product DB now.
"""

from fastapi import APIRouter, status

from app.interface.api.deps import ContainerDep
from app.stages.collect.collection import RECENT_RUNS_DEFAULT
from app.stages.collect.contracts import (
    CollectionRequest,
    CollectionRunListView,
    CollectionRunView,
    RevisionHistoryView,
    RevisionView,
    SubmittedCollectionView,
)
from app.stages.collect.facts import FactsStatus
from app.stages.collect.models import CollectionOutcome
from app.stages.collect.runs import CollectionRunRecord
from app.stages.products.contracts import SourceHandoffView

router = APIRouter(tags=["collect"])


def _run_view(run: CollectionRunRecord) -> CollectionRunView:
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
        transport_kind=None if run.provenance is None else run.provenance.transport_kind,
        capture_policy_revision=(
            None if run.provenance is None else run.provenance.capture_policy_revision
        ),
        capture_policy_digest=(
            None if run.provenance is None else run.provenance.capture_policy_digest
        ),
    )


@router.post("/api/v1/collect/collections", status_code=status.HTTP_202_ACCEPTED)
def submit(request: CollectionRequest, container: ContainerDep) -> SubmittedCollectionView:
    """Accept one product URL. The response is an identity to follow, not a result."""
    submitted = container.collection.submit(request.supplier_key, request.product_url)
    return SubmittedCollectionView(
        collection_run_id=submitted.collection_run_id,
        job_id=submitted.job_id,
        correlation_id=submitted.correlation_id,
    )


@router.get("/api/v1/collect/collections")
def recent_runs(
    container: ContainerDep,
    limit: int | None = None,
    outcome: CollectionOutcome | None = None,
    facts_status: FactsStatus | None = None,
    before: str | None = None,
) -> CollectionRunListView:
    """The newest runs. A filter selects runs by what each run recorded before the list is
    ordered and bounded; ``before`` continues after a run already shown. Read-only."""
    size = RECENT_RUNS_DEFAULT if limit is None else limit
    runs = container.collection.recent_runs(
        limit, outcome=outcome, facts_status=facts_status, before=before
    )
    total = container.collection.run_count(outcome=outcome, facts_status=facts_status)
    more = len(runs) == size and bool(
        container.collection.recent_runs(
            1, outcome=outcome, facts_status=facts_status, before=runs[-1].collection_run_id
        )
    )
    return CollectionRunListView(
        runs=tuple(_run_view(run) for run in runs),
        limit=size,
        outcome=outcome,
        facts_status=facts_status,
        total=total,
        next_before=runs[-1].collection_run_id if more else None,
    )


@router.get("/api/v1/collect/collections/{collection_run_id}")
def collection_run(collection_run_id: str, container: ContainerDep) -> CollectionRunView:
    return _run_view(container.collection.run(collection_run_id))


@router.get("/api/v1/collect/collections/{collection_run_id}/product")
def collection_product(collection_run_id: str, container: ContainerDep) -> SourceHandoffView:
    """Where a RECORDED run's revision stands in the Product DB now. Read-only: it collects and
    materializes nothing."""
    recorded = container.collection.recorded_source(collection_run_id)
    return container.products.source_handoff(
        recorded.supplier_key, recorded.source_product_id, recorded.revision_id
    )


@router.get("/api/v1/collect/revisions/{revision_id}")
def revision(revision_id: str, container: ContainerDep) -> RevisionView:
    return container.source_truth.revision(revision_id)


@router.get("/api/v1/collect/products/{supplier_key}/{source_product_id}/revisions")
def history(
    supplier_key: str, source_product_id: str, container: ContainerDep
) -> RevisionHistoryView:
    return container.source_truth.history(supplier_key, source_product_id)
