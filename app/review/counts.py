"""Open review counts per kind, and when they are authoritative (Gate 2 G2-C, ADR-0016 §7).

A count comes from durable OPEN ReviewItems only, never from a hard-coded zero. Whether it is
**authoritative** is decided per kind, from every producer that can emit that kind:

- ``NOT_WIRED``: at least one of them has no production producer yet, or has never completed a
  full reconciliation (G2-14). The server says so and never reports a number as the count.
- ``NOT_CURRENT``: every one is wired, but at least one's coverage is not current (§4, §7): no
  complete pass this process run, a stale watermark, an unrecovered failure, or an owner that
  moved since the watermark. The durable OPEN rows are still shown, as what is *known*; never as
  the authoritative count, and never as an authoritative zero.
- ``CURRENT``: every one is wired and current, both before and after the rows were counted. Only
  then is ``open`` set, and only then may a screen rest an "empty" verdict on it.

**Producer-complete, not producer-present** (comment ``5808443224``). A kind whose emitters are
only partly implemented is ``NOT_WIRED``: STOCK is never authoritative on COLLECT's coverage alone,
because OPERATE's stock workflow (``docs/ARCHITECTURE.md`` §11) can emit it and has no producer.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from app.review.collect_producer import COLLECT_PRODUCER
from app.review.coverage import CoverageView
from app.review.model import CountState, ReviewKind
from app.review.owner import ReviewItemStore
from app.review.preflight_producer import PREFLIGHT_PRODUCER
from app.review.products_producer import PRODUCTS_PRODUCER
from app.review.reconciler import ReviewReconciler
from app.review.register_producer import REGISTER_PRODUCER

# Owner conditions that open review work of a kind but have no production producer in Gate 2.
# Each is named after the owner that derives, or will derive, the condition.
COLLECT_NO_REVISION_PRODUCER: Final = "collect.runs"
OPERATE_STOCK_PRODUCER: Final = "operate.stock"
SOURCE_DRIFT_PRODUCER: Final = "operate.source_drift"
COMPLIANCE_PRODUCER: Final = "compliance.gate"
FULFILLMENT_PRODUCER: Final = "operate.fulfillment"

# Every producer that can emit each kind. The table is reviewed with G2-C (ADR-0016 §6, §12).
EMITTERS: Final[Mapping[ReviewKind, tuple[str, ...]]] = {
    # A ProductFactsRevision's REVIEW_REQUIRED truth (G2-B) and M4's evidence reasons; and a
    # collection run that ended NO_REVISION. ADR-0010 leaves such a run to architecture review, and
    # no ADR yet defines a run-scoped ReviewItem identity or how its terminal condition would ever
    # clear (architect decision on PR #109, review 5810256789). No item is created for it and no
    # scope key is invented; it stays NOT_WIRED so COLLECT_EVIDENCE is never an authoritative zero.
    ReviewKind.COLLECT_EVIDENCE: (
        COLLECT_PRODUCER,
        PRODUCTS_PRODUCER,
        COLLECT_NO_REVISION_PRODUCER,
    ),
    # The source stock field (G2-B); and OPERATE's stock workflow, which does not exist yet.
    ReviewKind.STOCK: (COLLECT_PRODUCER, OPERATE_STOCK_PRODUCER),
    # M4's membership and binding reasons; and source drift, which has no production owner.
    ReviewKind.SOURCE_CHANGE: (PRODUCTS_PRODUCER, SOURCE_DRIFT_PRODUCER),
    # No Gate 2 producer: ComplianceGate is a later pre-LIVE gate (G2-13).
    ReviewKind.COMPLIANCE: (COMPLIANCE_PRODUCER,),
    # Execution states (UNKNOWN, MISMATCH, PAUSED) and each current preparation's candidate
    # preflight: an authoritative zero needs both current (review 5810256789 B2).
    ReviewKind.REGISTRATION_ERROR: (REGISTER_PRODUCER, PREFLIGHT_PRODUCER),
    # No Gate 2 producer: M6 (G2-13).
    ReviewKind.FULFILLMENT: (FULFILLMENT_PRODUCER,),
}

REVIEW_PRODUCER_NOT_IMPLEMENTED: Final = "REVIEW_PRODUCER_NOT_IMPLEMENTED"
REVIEW_PRODUCER_NO_FULL_PASS: Final = "REVIEW_PRODUCER_NO_FULL_PASS"


@dataclass(frozen=True)
class EmitterState:
    """One producer that can emit a kind, as the count sees it."""

    producer: str
    # Implemented, and its first full reconciliation is durably recorded (G2-14).
    wired: bool
    current: bool
    # Why it is not wired or not current, as a server code; None when it is both.
    reason: str | None


@dataclass(frozen=True)
class KindCount:
    kind: ReviewKind
    state: CountState
    # The authoritative count: set only when ``state`` is CURRENT.
    open: int | None
    # The durable OPEN rows of the kind now. A lower bound unless ``state`` is CURRENT.
    open_known: int
    emitters: tuple[EmitterState, ...]

    @property
    def authoritative_zero(self) -> bool:
        """The only reading on which a screen may say there is no review work of this kind."""
        return self.state is CountState.CURRENT and self.open == 0


class ReviewCounts:
    """Reads durable rows and coverage; writes nothing (a moved owner only requests a pass)."""

    def __init__(self, store: ReviewItemStore, reconciler: ReviewReconciler) -> None:
        self._store = store
        self._reconciler = reconciler

    def open_counts(self) -> dict[ReviewKind, KindCount]:
        # Coverage is read on both sides of the count: a producer is current for this count only
        # if it was current before the rows were read and still is after, against its owner now.
        before = {view.producer: view for view in self._reconciler.coverage()}
        known = self._store.open_counts()
        after = {view.producer: view for view in self._reconciler.coverage()}
        return {kind: _count(kind, known.get(kind, 0), before, after) for kind in ReviewKind}


def _emitter(
    producer: str, before: Mapping[str, CoverageView], after: Mapping[str, CoverageView]
) -> EmitterState:
    first, last = before.get(producer), after.get(producer)
    if first is None or last is None:
        return EmitterState(producer, False, False, REVIEW_PRODUCER_NOT_IMPLEMENTED)
    if last.full_passes < 1:
        return EmitterState(producer, False, False, REVIEW_PRODUCER_NO_FULL_PASS)
    if first.current and last.current:
        return EmitterState(producer, True, True, None)
    return EmitterState(producer, True, False, last.reason or first.reason)


def _count(
    kind: ReviewKind,
    known: int,
    before: Mapping[str, CoverageView],
    after: Mapping[str, CoverageView],
) -> KindCount:
    emitters = tuple(_emitter(p, before, after) for p in EMITTERS[kind])
    if not all(e.wired for e in emitters):
        state = CountState.NOT_WIRED
    elif not all(e.current for e in emitters):
        state = CountState.NOT_CURRENT
    else:
        state = CountState.CURRENT
    return KindCount(
        kind=kind,
        state=state,
        open=known if state is CountState.CURRENT else None,
        open_known=known,
        emitters=emitters,
    )
