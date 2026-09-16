"""COLLECT source-truth read-back (ADR-0010 §6–§9).

Read-only routes over the stored revisions: what comes back is what the database holds, so the
same revision can be compared field for field through either path. Nothing here collects,
persists or transforms anything.
"""

from fastapi import APIRouter

from app.api.deps import ContainerDep
from app.collect.contracts import RevisionHistoryView, RevisionView

router = APIRouter(tags=["collect"])


@router.get("/api/v1/collect/revisions/{revision_id}")
def revision(revision_id: str, container: ContainerDep) -> RevisionView:
    return container.source_truth.revision(revision_id)


@router.get("/api/v1/collect/products/{supplier_key}/{source_product_id}/revisions")
def history(
    supplier_key: str, source_product_id: str, container: ContainerDep
) -> RevisionHistoryView:
    return container.source_truth.history(supplier_key, source_product_id)
