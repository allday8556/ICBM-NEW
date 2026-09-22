from enum import StrEnum


class ReviewKind(StrEnum):
    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    STOCK = "STOCK"
    SOURCE_CHANGE = "SOURCE_CHANGE"
    COMPLIANCE = "COMPLIANCE"
    REGISTRATION_ERROR = "REGISTRATION_ERROR"
    FULFILLMENT = "FULFILLMENT"


class ReviewService:
    """Owner of ReviewItem, with no ReviewItem yet (ARCHITECTURE.md §9).

    This class was written when nothing produced review work, so zero counts were the truth. That
    premise no longer holds: M3 is accepted and COLLECT already records ``REVIEW_REQUIRED`` source
    truth, and M4/M5 readiness carries that ambiguity further. There is still no ``ReviewItem``
    table, producer or persistence, so the counts below are a **placeholder, not evidence that
    nothing needs review**: such work is visible only in the screen that derives it. Building the
    durable owner is separately authorized work.
    """

    def open_counts(self) -> dict[ReviewKind, int]:
        """Zero per kind, because nothing is stored — never because nothing is open."""
        return dict.fromkeys(ReviewKind, 0)
