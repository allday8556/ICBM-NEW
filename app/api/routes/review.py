"""The human review path of the existing screens (Gate 2 G2-B, ADR-0016 §5, §7, §8).

Two operations only: read the ReviewItems of one canonical scope with its coverage verdict, and
resolve one item generation. A resolution never changes an owner fact. The item closes only if
its owner no longer derives the condition, and the server decides that inside one locked unit.
There is no count here and no top-level review application.
"""

from fastapi import APIRouter

from app.api.deps import ContainerDep
from app.core.correlation import get_correlation_id, new_correlation_id
from app.core.errors import InputValidationError
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
from app.review.scopes import producers_of

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


REVIEW_SCOPE_UNSUPPORTED = "REVIEW_SCOPE_UNSUPPORTED"


@router.get("/items")
def items(
    container: ContainerDep,
    supplier_key: str | None = None,
    source_product_id: str | None = None,
    product_group_id: str | None = None,
    item_id: str | None = None,
    marketplace_key: str | None = None,
    marketplace_account_id: str | None = None,
    draft_id: str | None = None,
    intent_id: str | None = None,
    preparation_id: str | None = None,
    state: ReviewState | None = None,
) -> ReviewItemListView:
    """The ReviewItems of one canonical owner scope, as an existing screen holds it: a COLLECT
    source product, an M4 Product or Item, or a REGISTER account, Draft, Intent or preparation.
    It carries the coverage verdict of exactly the producers whose items that scope can name
    (``app.review.scopes``); another producer's coverage says nothing about this list."""
    given = {
        "supplier_key": supplier_key,
        "source_product_id": source_product_id,
        "product_group_id": product_group_id,
        "item_id": item_id,
        "marketplace_key": marketplace_key,
        "marketplace_account_id": marketplace_account_id,
        "draft_id": draft_id,
        "intent_id": intent_id,
        "preparation_id": preparation_id,
    }
    scope = canonical_scope({key: value for key, value in given.items() if value is not None})
    store = container.review_items
    producers = producers_of(scope, store.producers)
    if not producers:
        raise InputValidationError(
            REVIEW_SCOPE_UNSUPPORTED,
            "no review producer's items carry that combination of identifiers",
            details={"keys": sorted(scope)},
        )
    found = sorted(
        (
            i
            for producer in producers
            for i in store.items(scope=scope, producer=producer, state=state)
        ),
        key=lambda i: (i.changed_at, i.review_item_id),
        reverse=True,
    )
    return ReviewItemListView(
        items=tuple(_item(i, store.history(i.review_item_id)) for i in found),
        coverage=tuple(
            ReviewCoverageView(
                producer=c.producer, current=c.current, reason=c.reason, watermark_at=c.watermark_at
            )
            for c in container.review_reconciler.coverage()
            if c.producer in producers
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
