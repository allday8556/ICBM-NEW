"""The adopted SmartStore product-search contract, read for positive-only reconcile only
(ADR-0014 §28.2–§28.4; ADR-0020 §4 order 2; Issue #89 architect resolution ``5904349289``).

**The request** (S1). ``POST /v1/products/search`` with an ``application/json`` body that carries
exactly the seller-code search the official reference and support record: ``searchKeywordType``
``SELLER_CODE``, ``sellerManagementCode`` — always the ``smartstore-seller-management-code/v1``
projection of the ICBM listing identity (ruling R1), never the 37-character internal identity —
and the pagination fields ``page`` (``int32``, first page 1) and ``size`` (``int32``, at most 500).
No other filter is sent: narrowing a result with an invented condition is not a documented
contract.

**The response** (S2). HTTP 200 with a JSON object whose top level carries ``contents`` (an array
of objects), ``page``, ``size``, ``totalElements``, ``totalPages``, ``first`` and ``last``. Each
``contents[n]`` carries ``originProductNo`` (``int64``) and ``channelProducts`` (an array); each
channel entry carries ``originProductNo`` and ``channelProductNo`` (``int64``), the
``channelServiceType`` (``STOREFARM | WINDOW | AFFILIATE``) and the ``sellerManagementCode``.
:func:`read_page` reads exactly that shape and refuses anything else: a boolean, a string or a
float where an integer is documented, a missing member, or a channel entry that names another
origin product than its own item. A page it cannot read proves nothing.

**What a lookup may conclude** (§28.2). The provider's seller-code match is similar, partial or
exact (``OFFICIAL_SUPPORT`` #1828), so it is never trusted: only a ``STOREFARM`` channel entry whose
``sellerManagementCode`` is **exactly** the projection is an ICBM candidate, compared here, locally.
Every page must be enumerated consistently for a count to be trustworthy; an enumeration that
cannot complete within the bounded page budget, or whose pages disagree, is never a count at all.
And no count is ever absence: zero exact candidates leaves an ``UNKNOWN`` CREATE ``UNKNOWN``, more
than one is ``REVIEW_REQUIRED``, and exactly one is only an identity-recovery candidate until the
adopted origin read-back proves it carries the same code (``execution.py``, REGISTER §28.2).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

SEARCH_CONTRACT_VERSION: Final = "smartstore-product-search/v1"

SEARCH_KEYWORD_TYPE: Final = "SELLER_CODE"
# S1: the first page is 1; the documented maximum page size is 500.
FIRST_PAGE: Final = 1
MAX_PAGE_SIZE: Final = 500
# ICBM policy, never a provider fact: the most pages one reconcile check may read. A seller-code
# search that needs more cannot be enumerated within the bounded read budget, so it is
# LOOKUP_UNAVAILABLE — a partial enumeration is never a unique positive result.
MAX_PAGES_PER_CHECK: Final = 4

# S2: the SmartStore channel. WINDOW and AFFILIATE are other channels and are never candidates.
STOREFARM: Final = "STOREFARM"
CHANNEL_SERVICE_TYPES: Final = frozenset({"STOREFARM", "WINDOW", "AFFILIATE"})

INT32_MAX: Final = 2**31 - 1
INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1

FIELD_CONTENTS: Final = "contents"
FIELD_ORIGIN_PRODUCT_NO: Final = "originProductNo"
FIELD_CHANNEL_PRODUCTS: Final = "channelProducts"
FIELD_CHANNEL_PRODUCT_NO: Final = "channelProductNo"
FIELD_CHANNEL_SERVICE_TYPE: Final = "channelServiceType"
FIELD_SELLER_MANAGEMENT_CODE: Final = "sellerManagementCode"
PAGE_FIELDS: Final = ("page", "size", "totalElements", "totalPages", "first", "last")


class SearchContractError(ValueError):
    """A search response that is not the documented shape. It proves nothing either way."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def request_body(seller_management_code: str, page: int, size: int) -> dict[str, Any]:
    """The documented seller-code search body (S1), and nothing else."""
    return {
        "searchKeywordType": SEARCH_KEYWORD_TYPE,
        "sellerManagementCode": seller_management_code,
        "page": page,
        "size": size,
    }


def _integer(value: object, path: str, low: int, high: int) -> int:
    # bool is an int subclass: it is refused explicitly, like any other type.
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SearchContractError("SEARCH_RESPONSE_MALFORMED", f"{path} is not an integer")
    return value


def _flag(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise SearchContractError("SEARCH_RESPONSE_MALFORMED", f"{path} is not a boolean")
    return value


@dataclass(frozen=True)
class ChannelEntry:
    origin_product_no: int
    channel_product_no: int
    channel_service_type: str
    seller_management_code: str | None


@dataclass(frozen=True)
class SearchItem:
    origin_product_no: int
    channels: tuple[ChannelEntry, ...]


@dataclass(frozen=True)
class SearchPage:
    page: int
    size: int
    total_elements: int
    total_pages: int
    first: bool
    last: bool
    items: tuple[SearchItem, ...]


def _channel(value: object, path: str, origin: int) -> ChannelEntry:
    if not isinstance(value, Mapping):
        raise SearchContractError("SEARCH_RESPONSE_MALFORMED", f"{path} is not an object")
    channel_origin = _integer(
        value.get(FIELD_ORIGIN_PRODUCT_NO), f"{path}.originProductNo", 1, INT64_MAX
    )
    if channel_origin != origin:
        raise SearchContractError(
            "SEARCH_RESPONSE_INCONSISTENT", f"{path} names another origin product"
        )
    service = value.get(FIELD_CHANNEL_SERVICE_TYPE)
    if service not in CHANNEL_SERVICE_TYPES:
        raise SearchContractError(
            "SEARCH_RESPONSE_MALFORMED", f"{path}.channelServiceType is not documented"
        )
    code = value.get(FIELD_SELLER_MANAGEMENT_CODE)
    if code is not None and not isinstance(code, str):
        raise SearchContractError(
            "SEARCH_RESPONSE_MALFORMED", f"{path}.sellerManagementCode is not a string"
        )
    return ChannelEntry(
        origin_product_no=channel_origin,
        channel_product_no=_integer(
            value.get(FIELD_CHANNEL_PRODUCT_NO), f"{path}.channelProductNo", 1, INT64_MAX
        ),
        channel_service_type=str(service),
        seller_management_code=code,
    )


def read_page(retained: Mapping[str, Any]) -> SearchPage:
    """One documented search page (S2), read at exactly its documented positions and types."""
    if not isinstance(retained, Mapping):
        raise SearchContractError("SEARCH_RESPONSE_MALFORMED", "the page is not an object")
    contents = retained.get(FIELD_CONTENTS, [])
    # Retention drops an empty array, so an absent ``contents`` is an empty page; any other type
    # is a shape the evidence does not describe.
    if not isinstance(contents, Sequence) or isinstance(contents, str | bytes):
        raise SearchContractError("SEARCH_RESPONSE_MALFORMED", "contents is not an array")
    items: list[SearchItem] = []
    for index, raw in enumerate(contents):
        path = f"contents[{index}]"
        if not isinstance(raw, Mapping):
            raise SearchContractError("SEARCH_RESPONSE_MALFORMED", f"{path} is not an object")
        origin = _integer(raw.get(FIELD_ORIGIN_PRODUCT_NO), f"{path}.originProductNo", 1, INT64_MAX)
        channels = raw.get(FIELD_CHANNEL_PRODUCTS, [])
        if not isinstance(channels, Sequence) or isinstance(channels, str | bytes):
            raise SearchContractError(
                "SEARCH_RESPONSE_MALFORMED", f"{path}.channelProducts is not an array"
            )
        items.append(
            SearchItem(
                origin_product_no=origin,
                channels=tuple(
                    _channel(entry, f"{path}.channelProducts[{n}]", origin)
                    for n, entry in enumerate(channels)
                ),
            )
        )
    page = SearchPage(
        page=_integer(retained.get("page"), "page", FIRST_PAGE, INT32_MAX),
        size=_integer(retained.get("size"), "size", 1, MAX_PAGE_SIZE),
        total_elements=_integer(retained.get("totalElements"), "totalElements", 0, INT64_MAX),
        total_pages=_integer(retained.get("totalPages"), "totalPages", 0, INT32_MAX),
        first=_flag(retained.get("first"), "first"),
        last=_flag(retained.get("last"), "last"),
        items=tuple(items),
    )
    if len(page.items) > page.size:
        raise SearchContractError("SEARCH_RESPONSE_INCONSISTENT", "a page exceeds its size")
    return page


@dataclass(frozen=True)
class Candidate:
    """One exact ICBM candidate: the origin product and its SmartStore (``STOREFARM``) channel."""

    origin_product_no: int
    channel_product_no: int

    def canonical(self) -> dict[str, str]:
        return {
            "origin_product_no": str(self.origin_product_no),
            "channel_product_no": str(self.channel_product_no),
        }


def exact_candidates(pages: Sequence[SearchPage], expected_code: str) -> tuple[Candidate, ...]:
    """The ``STOREFARM`` channel entries whose ``sellerManagementCode`` is exactly
    ``expected_code``, over every enumerated page, in provider order. The enumeration check has
    already refused a product read twice, so every candidate here is a distinct listing.

    The provider's own match is similar, partial or exact; only local exact equality counts, and
    only on the SmartStore channel.
    """
    found: list[Candidate] = []
    for page in pages:
        for item in page.items:
            for channel in item.channels:
                if (
                    channel.channel_service_type == STOREFARM
                    and channel.seller_management_code == expected_code
                ):
                    found.append(Candidate(item.origin_product_no, channel.channel_product_no))
    return tuple(found)


def check_enumeration(pages: Sequence[SearchPage]) -> None:
    """Refuse an enumeration whose pages do not form one complete, consistent result.

    Page ``n`` must answer as page ``n``; every page must report the same totals; the last page
    read must be the documented last page; the items read must be exactly ``totalElements``; and
    no product or channel product may be read twice. Anything else is not a trustworthy count, so
    nothing may be concluded from it.
    """
    if not pages:
        raise SearchContractError("SEARCH_ENUMERATION_INCOMPLETE", "no page was read")
    first = pages[0]
    for number, page in enumerate(pages, start=FIRST_PAGE):
        if page.page != number:
            raise SearchContractError("SEARCH_RESPONSE_INCONSISTENT", f"page {number} answered")
        if (page.total_elements, page.total_pages) != (first.total_elements, first.total_pages):
            raise SearchContractError("SEARCH_RESPONSE_INCONSISTENT", "the totals moved")
        if page.first != (number == FIRST_PAGE):
            raise SearchContractError("SEARCH_RESPONSE_INCONSISTENT", "first is misplaced")
    if not pages[-1].last or len(pages) < max(first.total_pages, 1):
        raise SearchContractError("SEARCH_ENUMERATION_INCOMPLETE", "the last page was not read")
    if sum(len(page.items) for page in pages) != first.total_elements:
        raise SearchContractError(
            "SEARCH_RESPONSE_INCONSISTENT", "the items read are not totalElements"
        )
    # The same product, or the same channel product, read twice means the result moved between
    # pages: another product may have been displaced and never read. That is never a count.
    origins = [item.origin_product_no for page in pages for item in page.items]
    channels = [
        channel.channel_product_no
        for page in pages
        for item in page.items
        for channel in item.channels
    ]
    if len(set(origins)) != len(origins) or len(set(channels)) != len(channels):
        raise SearchContractError(
            "SEARCH_RESPONSE_INCONSISTENT", "a product was read twice across the enumeration"
        )


__all__ = [
    "CHANNEL_SERVICE_TYPES",
    "FIRST_PAGE",
    "MAX_PAGES_PER_CHECK",
    "MAX_PAGE_SIZE",
    "SEARCH_CONTRACT_VERSION",
    "SEARCH_KEYWORD_TYPE",
    "STOREFARM",
    "Candidate",
    "ChannelEntry",
    "SearchContractError",
    "SearchItem",
    "SearchPage",
    "check_enumeration",
    "exact_candidates",
    "read_page",
    "request_body",
]
