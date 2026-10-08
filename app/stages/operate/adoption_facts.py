"""What a marketplace listing search hands the adoption owner (ADR-0024 §3): pure values, no I/O.

The marketplace adapter finds a listing by an exact seller code and reads it back; OPERATE never
imports the adapter.
"""

from dataclasses import dataclass
from typing import Final, Protocol

FOUND: Final = "FOUND"
NOT_FOUND: Final = "NOT_FOUND"
AMBIGUOUS: Final = "AMBIGUOUS"
MISMATCH: Final = "MISMATCH"
DELETED: Final = "DELETED"
UNAVAILABLE: Final = "UNAVAILABLE"
RATE_LIMITED: Final = "RATE_LIMITED"
FAILED: Final = "FAILED"


@dataclass(frozen=True)
class FoundListing:
    """The answer for one seller code.

    ``FOUND`` names exactly one SmartStore listing whose search entry and origin read-back both
    carry the code and whose sale status is not ``DELETE``. Every other outcome proves nothing
    about a listing and adopts nothing.
    """

    outcome: str
    origin_product_no: str | None = None
    channel_product_no: str | None = None
    sale_status: str | None = None
    display_status: str | None = None
    error_code: str | None = None


class ListingFinder(Protocol):
    """The adopted read-only search and origin read of one marketplace (composition root)."""

    def available(self) -> bool: ...

    def find(self, seller_code: str) -> FoundListing: ...


__all__ = [
    "AMBIGUOUS",
    "DELETED",
    "FAILED",
    "FOUND",
    "MISMATCH",
    "NOT_FOUND",
    "RATE_LIMITED",
    "UNAVAILABLE",
    "FoundListing",
    "ListingFinder",
]
