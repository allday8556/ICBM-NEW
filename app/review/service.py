from app.review.model import ReviewKind

__all__ = ["ReviewKind", "ReviewService"]


class ReviewService:
    """The dashboard's review counts, still a placeholder (ARCHITECTURE.md §9, ADR-0016 §7).

    The durable ReviewItem owner exists (``app.review.owner``, Gate 2 G2-A), but no producer is
    wired to it yet, so no kind is ``WIRED`` and no count is authoritative. The counts below are a
    **placeholder, not evidence that nothing needs review**: such work is visible only in the screen
    that derives it. Counts from durable rows, with ``NOT_WIRED`` and current-coverage semantics,
    are separately authorized work (G2-C).
    """

    def open_counts(self) -> dict[ReviewKind, int]:
        """Zero per kind, because no producer is wired — never because nothing is open."""
        return dict.fromkeys(ReviewKind, 0)
