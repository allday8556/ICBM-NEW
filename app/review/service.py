from enum import StrEnum


class ReviewKind(StrEnum):
    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    STOCK = "STOCK"
    SOURCE_CHANGE = "SOURCE_CHANGE"
    COMPLIANCE = "COMPLIANCE"
    REGISTRATION_ERROR = "REGISTRATION_ERROR"
    FULFILLMENT = "FULFILLMENT"


class ReviewService:
    """Owner of ReviewItem. The table arrives with the first producer of review work
    (collection evidence, M3); until then no item can be open."""

    def open_counts(self) -> dict[ReviewKind, int]:
        return dict.fromkeys(ReviewKind, 0)
