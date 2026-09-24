"""The review read and resolve contract of the existing screens (Gate 2 G2-B, ADR-0016 §7, §8).

Every field is server-owned. The screen shows its own ReviewItems, and a coverage verdict that
says whether the list is current. It never renders a verdict of its own, and it never shows a
count as authoritative (counts are G2-C). A resolution names the scope and generation it was
shown; the server re-checks both.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.review.model import (
    ResolutionOutcome,
    ReviewBasis,
    ReviewDisposition,
    ReviewEvent,
    ReviewKind,
    ReviewState,
)


class ReviewEventView(BaseModel):
    event_no: int
    generation: int
    event: ReviewEvent
    to_state: ReviewState
    basis: ReviewBasis
    successor_item_id: str | None
    disposition: ReviewDisposition | None
    note: str | None
    evidence_reference: str | None
    actor: str
    occurred_at: datetime


class ReviewItemView(BaseModel):
    review_item_id: str
    kind: ReviewKind
    producer: str
    scope: dict[str, str]
    subject: str
    reason_code: str
    source_identity: str
    state: ReviewState
    generation: int
    opened_at: datetime
    changed_at: datetime
    history: tuple[ReviewEventView, ...] = ()


class ReviewCoverageView(BaseModel):
    """Whether one producer's items are current. Never a count."""

    producer: str
    current: bool
    reason: str | None
    watermark_at: datetime | None


class ReviewItemListView(BaseModel):
    items: tuple[ReviewItemView, ...]
    coverage: tuple[ReviewCoverageView, ...]
    dispositions: tuple[ReviewDisposition, ...] = tuple(ReviewDisposition)


class ResolveRequest(BaseModel):
    """One human resolution of the generation of one item the operator was shown (§5, §8)."""

    model_config = ConfigDict(extra="forbid")

    expected_scope: dict[str, str] = Field(min_length=1, max_length=10)
    expected_generation: int = Field(ge=1)
    disposition: ReviewDisposition
    note: str | None = Field(default=None, max_length=500)
    evidence_reference: str | None = Field(default=None, max_length=128)
    actor: str = Field(min_length=2, max_length=64)


class ResolutionView(BaseModel):
    item: ReviewItemView
    outcome: ResolutionOutcome
    replayed: bool
    successor_item_id: str | None
