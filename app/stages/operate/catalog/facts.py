"""What a marketplace catalog source hands the StoreCatalog owner (ADR-0042 §5): pure values.

The marketplace adapter enumerates a store's catalog and reads one listing's allowlisted fields;
OPERATE never imports the adapter. Nothing here is a product: a store listing is a provider-side
object (ADR-0042 §3, SCP-01).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

# The enumeration outcomes of ADR-0042 §5.2. Only COMPLETE concludes anything.
COMPLETE: Final = "COMPLETE"
INCOMPLETE: Final = "INCOMPLETE"
UNAVAILABLE: Final = "UNAVAILABLE"
ERROR: Final = "ERROR"

# The result of one listing read.
OBSERVED: Final = "OBSERVED"
# Provider evidence that the listing ended (ADR-0023 §3): the sale status DELETE or a 404.
REMOVED: Final = "REMOVED"
READ_FAILED: Final = "READ_FAILED"
# The read was refused by a rate or quota limit, or no session could read: the sync stops.
RATE_LIMITED: Final = "RATE_LIMITED"
SESSION_UNAVAILABLE: Final = "SESSION_UNAVAILABLE"

STATUS_DELETE: Final = "STATUS_DELETE"
HTTP_404: Final = "HTTP_404"


@dataclass(frozen=True)
class CatalogEntry:
    """One product the enumeration listed: its origin product and its own-store channel.

    ``channel_product_no`` and ``listed_seller_code`` come from the one SmartStore-channel entry of
    the product; with none, or more than one, both stay ``None`` and nothing is guessed.
    """

    origin_product_no: str
    channel_product_no: str | None
    listed_seller_code: str | None
    own_channels: int


@dataclass(frozen=True)
class Enumeration:
    """One whole enumeration of a store's catalog. ``entries`` is empty unless COMPLETE."""

    outcome: str
    contract_version: str
    entries: tuple[CatalogEntry, ...] = ()
    total_elements: int | None = None
    pages_read: int = 0
    error_code: str | None = None


@dataclass(frozen=True)
class ListingFields:
    """The allowlisted fields of one listing (ADR-0042 §5.3), read at their recorded paths.

    The typed members are the ones the catalog stores in columns; ``canonical`` is the whole
    allowlisted observation, grouped as §5.3 groups it. A field the provider evidence does not
    record is absent from it, never filled in.
    """

    fields_version: str
    sale_status: str | None
    display_status: str | None
    name: str | None
    sale_price: int | None
    stock_quantity: int | None
    seller_management_code: str | None
    seller_barcode: str | None
    leaf_category_id: str | None
    canonical: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ListingRead:
    """The answer of one listing read. Only ``OBSERVED`` carries fields; only ``REMOVED`` carries
    removal evidence; every failure carries its code and proves nothing."""

    result: str
    fields: ListingFields | None = None
    removal_evidence: str | None = None
    error_code: str | None = None


class CatalogSource(Protocol):
    """The adopted read-only catalog reads of one marketplace store (composition root)."""

    @property
    def marketplace_key(self) -> str: ...

    @property
    def contract_version(self) -> str:
        """The versioned enumeration and field contracts a sync is read under (on its run)."""
        ...

    def available(self) -> bool: ...

    def enumerate(
        self, *, pause: Callable[[], None], observe: Callable[[object], None] | None = None
    ) -> Enumeration:
        """``observe`` receives each raw parsed response before retention, for the shape-only
        runtime capture (ADR-0042 §5.1 item 3)."""
        ...

    def read(
        self, origin_product_no: str, *, observe: Callable[[object], None] | None = None
    ) -> ListingRead: ...


__all__ = [
    "COMPLETE",
    "ERROR",
    "HTTP_404",
    "INCOMPLETE",
    "OBSERVED",
    "RATE_LIMITED",
    "READ_FAILED",
    "REMOVED",
    "SESSION_UNAVAILABLE",
    "STATUS_DELETE",
    "UNAVAILABLE",
    "CatalogEntry",
    "CatalogSource",
    "Enumeration",
    "ListingFields",
    "ListingRead",
]
