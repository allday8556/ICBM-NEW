from app.review.counts import KindCount, ReviewCounts
from app.review.model import CountState, ReviewKind

__all__ = ["CountState", "KindCount", "ReviewKind", "ReviewService"]


class ReviewService:
    """The dashboard's and 품절's review counts (ARCHITECTURE.md §9, ADR-0016 §7, Gate 2 G2-C).

    Each kind's count comes from durable OPEN ReviewItems. It is authoritative only when every
    producer that can emit the kind is wired and current (``app.review.counts``); otherwise the
    kind says ``NOT_WIRED`` or ``NOT_CURRENT`` and carries no count, only the rows known now.
    Nothing here is a hard-coded zero.
    """

    def __init__(self, counts: ReviewCounts) -> None:
        self._counts = counts

    def open_counts(self) -> dict[ReviewKind, KindCount]:
        return self._counts.open_counts()
