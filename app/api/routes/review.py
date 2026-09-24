"""The human review path of the existing screens (Gate 2 G2-B, ADR-0016 §5, §7, §8).

Two operations only: read the ReviewItems of one canonical scope with its coverage verdict, and
resolve one item generation. A resolution never changes an owner fact. The item closes only if
its owner no longer derives the condition, and the server decides that inside one locked unit.
There is no count here and no top-level review application.
"""

from fastapi import APIRouter

from app.api.deps import ContainerDep
from app.core.correlation import get_correlation_id, new_correlation_id
from app.review.contracts import (
    ResolutionView,
    ResolveRequest,
    ReviewCoverageView,
    ReviewEventView,
    ReviewItemListView,
    ReviewItemView,
)
from app.review.model import ReviewState, canonical_scope
from app.review.owner import ReviewEventRecord, ReviewItemRecord

router = APIRouter(prefix="/api/v1/review", tags=["review"])


def _event(record: ReviewEventRecord) -> ReviewEventView:
    return ReviewEventView(
        event_no=record.event_no,
        generation=record.generation,
        event=record.event,
        to_state=record.to_state,
        basis=record.basis,
        successor_item_id=record.successor_item_id,
        disposition=record.disposition,
        note=record.note,
        evidence_reference=record.evidence_reference,
        actor=record.actor,
        occurred_at=record.occurred_at,
    )


def _item(record: ReviewItemRecord, history: tuple[ReviewEventRecord, ...] = ()) -> ReviewItemView:
    return ReviewItemView(
        review_item_id=record.review_item_id,
        kind=record.kind,
        producer=record.producer,
        scope=dict(record.scope),
        subject=record.subject,
        reason_code=record.reason_code,
        source_identity=record.source_identity,
        state=record.state,
        generation=record.generation,
        opened_at=record.opened_at,
        changed_at=record.changed_at,
        history=tuple(_event(e) for e in history),
    )


@router.get("/items")
def items(
    container: ContainerDep,
    supplier_key: str,
    source_product_id: str,
    state: ReviewState | None = None,
) -> ReviewItemListView:
    """The ReviewItems of one COLLECT source identity, with every producer's coverage verdict."""
    scope = canonical_scope({"supplier_key": supplier_key, "source_product_id": source_product_id})
    store = container.review_items
    found = store.items(scope=scope, state=state)
    return ReviewItemListView(
        items=tuple(_item(i, store.history(i.review_item_id)) for i in found),
        coverage=tuple(
            ReviewCoverageView(
                producer=c.producer, current=c.current, reason=c.reason, watermark_at=c.watermark_at
            )
            for c in container.review_reconciler.coverage()
        ),
    )


@router.post("/items/{review_item_id}/resolve")
def resolve(
    review_item_id: str, request: ResolveRequest, container: ContainerDep
) -> ResolutionView:
    result = container.review_items.resolve(
        review_item_id,
        expected_scope=request.expected_scope,
        expected_generation=request.expected_generation,
        disposition=request.disposition,
        note=request.note,
        evidence=request.evidence_reference,
        actor=request.actor,
        correlation_id=get_correlation_id() or new_correlation_id(),
    )
    store = container.review_items
    return ResolutionView(
        item=_item(store.item(review_item_id), store.history(review_item_id)),
        outcome=result.outcome,
        replayed=result.replayed,
        successor_item_id=result.successor_item_id,
    )
