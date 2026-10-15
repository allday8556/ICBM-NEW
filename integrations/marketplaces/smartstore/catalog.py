"""The SmartStore store-catalog source (ADR-0042 S1a, §5): read only, two adopted endpoints.

**Enumeration** — ``SMARTSTORE_PRODUCT_SEARCH`` with the paging-only body (E1;
``ENDPOINT_MATRIX.md`` §4.1.2 S1a amendment): page 1, 2, … of 500 until the provider's ``last``.
Each page is read by the adopted search contract (``search.read_page``) and the whole enumeration
by its consistency check (``search.check_enumeration``): every page from 1 to the last, page *n*
answering as page *n*, the same totals on every page, ``last`` on the last page only, and exactly
``totalElements`` distinct origin products, none read twice (ADR-0042 §5.2). Anything else is not
``COMPLETE`` and concludes nothing:

- a missing session, a ``429`` rate or quota refusal, a ``totalElements`` above 100,000 (E6) or a
  catalog beyond the page budget is ``UNAVAILABLE``;
- an enumeration that did not finish consistently is ``INCOMPLETE``;
- any other failure, an undocumented page or a provider error (the undocumented over-100,000 error
  included) is ``ERROR``.

No status filter and no sort parameter is sent: neither is documented (E2 ``NOT_CAPTURED``).

**One listing** — ``SMARTSTORE_ORIGIN_PRODUCT_READ_V2``, whose retention keeps only the recorded
store-catalog leaves (``registry._CATALOG_READ_FIELDS``). :func:`read_fields` reads each field at
exactly its recorded path (``PRODUCT_READ.md`` § store-catalog field paths, E4); the same leaf name
anywhere else is ignored, and a value of an unexpected type is left out, never coerced. Provider
evidence ends a listing (ADR-0023 §3): the sale status ``DELETE`` or an HTTP 404 of the read.

A failure never raises past this module: it becomes an outcome that concludes nothing.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from app.platform.core.errors import ErrorClass
from app.stages.operate.catalog.facts import (
    COMPLETE,
    ERROR,
    HTTP_404,
    INCOMPLETE,
    OBSERVED,
    RATE_LIMITED,
    READ_FAILED,
    REMOVED,
    SESSION_UNAVAILABLE,
    STATUS_DELETE,
    UNAVAILABLE,
    CatalogEntry,
    Enumeration,
    ListingFields,
    ListingRead,
)
from app.stages.register.sanitize import SECRET_MATERIAL, problems, safe_provider_reference
from integrations.marketplaces.smartstore import search
from integrations.marketplaces.smartstore.caller import (
    CatalogSearchRequest,
    ProductReadRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.product import MAX_SALE_PRICE, MAX_STOCK_QUANTITY
from integrations.marketplaces.smartstore.readback import DISPLAY_STATUSES, SALE_STATUSES
from integrations.marketplaces.smartstore.registry import EndpointId

FIELDS_VERSION: Final = "smartstore-catalog-fields/v1"
DELETE_STATUS: Final = "DELETE"
NOT_FOUND_STATUS: Final = 404
# A stored text value is bounded (ICBM policy): longer text is not a value this catalog keeps.
MAX_TEXT: Final = 2000
_INT64_MAX: Final = 2**63 - 1

# The recorded paths (PRODUCT_READ.md § store-catalog field paths). Every one starts at a
# documented top-level member of the origin read (2.90.0): ``originProduct`` or
# ``smartstoreChannelProduct``.
_ORIGIN: Final = "originProduct"
_CHANNEL: Final = "smartstoreChannelProduct"
_SELLER_CODES: Final = (_ORIGIN, "detailAttribute", "sellerCodeInfo")
_OPTION_INFO: Final = (_ORIGIN, "detailAttribute", "optionInfo")
_AFTER_SERVICE: Final = (_ORIGIN, "detailAttribute", "afterServiceInfo")
_DELIVERY: Final = (_ORIGIN, "deliveryInfo")
_DELIVERY_FEE: Final = (_ORIGIN, "deliveryInfo", "deliveryFee")
_IMAGES: Final = (_ORIGIN, "images")
_OPTION_NAMES: Final = ("optionName1", "optionName2", "optionName3", "optionName4")
_GROUP_NAMES: Final = (
    "optionGroupName1",
    "optionGroupName2",
    "optionGroupName3",
    "optionGroupName4",
)


# ---------------------------------------------------------------- exact-path readers


def _node(retained: Mapping[str, Any], path: Sequence[str]) -> Mapping[str, Any] | None:
    node: Any = retained
    for key in path:
        node = node.get(key) if isinstance(node, Mapping) else None
    return node if isinstance(node, Mapping) else None


def _text(node: Mapping[str, Any] | None, key: str) -> str | None:
    """A string value at exactly ``node[key]``: bounded, and never one carrying secret material."""
    value = node.get(key) if node is not None else None
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        return None
    if any(code == SECRET_MATERIAL for code, _ in problems(value)):
        return None
    return value


def _int(node: Mapping[str, Any] | None, key: str, low: int, high: int) -> int | None:
    value = node.get(key) if node is not None else None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def _flag(node: Mapping[str, Any] | None, key: str) -> bool | None:
    value = node.get(key) if node is not None else None
    return value if isinstance(value, bool) else None


def _identifier(node: Mapping[str, Any] | None, key: str) -> str | None:
    """An identifier whose value type the evidence does not record: a JSON integer (never a
    boolean) or a plain string, kept as text."""
    value = node.get(key) if node is not None else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= _INT64_MAX:
        return str(value)
    if isinstance(value, str) and 0 < len(value) <= 64 and value.isprintable():
        return value
    return None


def _enum(node: Mapping[str, Any] | None, key: str, allowed: frozenset[str]) -> str | None:
    value = node.get(key) if node is not None else None
    return value if isinstance(value, str) and value in allowed else None


def _options(retained: Mapping[str, Any]) -> dict[str, Any]:
    info = _node(retained, _OPTION_INFO)
    groups = _node(retained, (*_OPTION_INFO, "optionCombinationGroupNames"))
    raw = info.get("optionCombinations") if info is not None else None
    combinations = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            combinations.append(
                {
                    "id": _identifier(entry, "id"),
                    "names": [_text(entry, name) for name in _OPTION_NAMES],
                    "stock_quantity": _int(entry, "stockQuantity", 0, MAX_STOCK_QUANTITY),
                    # A combination's price is an amount added to the sale price; its sign is not
                    # recorded, so any integer within the documented magnitude is kept.
                    "price": _int(entry, "price", -MAX_SALE_PRICE, MAX_SALE_PRICE),
                    "usable": _flag(entry, "usable"),
                    "seller_manager_code": _text(entry, "sellerManagerCode"),
                }
            )
    return {
        "group_names": [_text(groups, name) for name in _GROUP_NAMES],
        "combinations": combinations,
    }


def _media(retained: Mapping[str, Any]) -> dict[str, Any]:
    images = _node(retained, _IMAGES)
    representative = _node(retained, (*_IMAGES, "representativeImage"))
    unsafe = 0
    head = representative.get("url") if representative is not None else None
    if isinstance(head, str) and not safe_provider_reference(head):
        head, unsafe = None, unsafe + 1
    optional: list[str] = []
    raw = images.get("optionalImages") if images is not None else None
    for entry in raw if isinstance(raw, list) else []:
        url = entry.get("url") if isinstance(entry, Mapping) else None
        if not isinstance(url, str):
            continue
        # A provider reference that cannot survive sanitation is counted, never kept.
        if safe_provider_reference(url):
            optional.append(url)
        else:
            unsafe += 1
    return {
        "representative": head if isinstance(head, str) else None,
        "optional": optional,
        "unsafe": unsafe,
    }


def read_fields(retained: Mapping[str, Any]) -> ListingFields:
    """The allowlisted §5.3 fields of one retained origin read, each at exactly its path."""
    origin = _node(retained, (_ORIGIN,))
    channel = _node(retained, (_CHANNEL,))
    codes = _node(retained, _SELLER_CODES)
    delivery = _node(retained, _DELIVERY)
    fee = _node(retained, _DELIVERY_FEE)
    service = _node(retained, _AFTER_SERVICE)
    sale_status = _enum(origin, "statusType", SALE_STATUSES)
    display_status = _enum(channel, "channelProductDisplayStatusType", DISPLAY_STATUSES)
    name = _text(origin, "name")
    sale_price = _int(origin, "salePrice", 0, MAX_SALE_PRICE)
    stock = _int(origin, "stockQuantity", 0, MAX_STOCK_QUANTITY)
    seller_code = _text(codes, "sellerManagementCode")
    barcode = _text(codes, "sellerBarcode")
    category = _identifier(origin, "leafCategoryId")
    canonical = {
        "fields_version": FIELDS_VERSION,
        "state": {"sale_status": sale_status, "display_status": display_status},
        "name": name,
        "price": {"sale_price": sale_price},
        "stock": {"stock_quantity": stock},
        "seller_keys": {
            "seller_management_code": seller_code,
            "seller_barcode": barcode,
            "seller_custom_code1": _text(codes, "sellerCustomCode1"),
            "seller_custom_code2": _text(codes, "sellerCustomCode2"),
        },
        "category": {"leaf_category_id": category},
        "options": _options(retained),
        "shipping": {
            "delivery_type": _text(delivery, "deliveryType"),
            "delivery_attribute_type": _text(delivery, "deliveryAttributeType"),
            "delivery_fee_type": _text(fee, "deliveryFeeType"),
            "base_fee": _int(fee, "baseFee", 0, MAX_SALE_PRICE),
            "free_conditional_amount": _int(fee, "freeConditionalAmount", 0, MAX_SALE_PRICE),
            "delivery_fee_pay_type": _text(fee, "deliveryFeePayType"),
        },
        "customer_service": {
            "telephone": _text(service, "afterServiceTelephoneNumber"),
            "guide": _text(service, "afterServiceGuideContent"),
        },
        "media": _media(retained),
    }
    return ListingFields(
        fields_version=FIELDS_VERSION,
        sale_status=sale_status,
        display_status=display_status,
        name=name,
        sale_price=sale_price,
        stock_quantity=stock,
        seller_management_code=seller_code,
        seller_barcode=barcode,
        leaf_category_id=category,
        canonical=canonical,
    )


# ---------------------------------------------------------------- the source


def _own_channel(item: search.SearchItem) -> CatalogEntry:
    own = [c for c in item.channels if c.channel_service_type == search.STOREFARM]
    single = own[0] if len(own) == 1 else None
    return CatalogEntry(
        origin_product_no=str(item.origin_product_no),
        channel_product_no=None if single is None else str(single.channel_product_no),
        listed_seller_code=None if single is None else single.seller_management_code,
        own_channels=len(own),
    )


class SmartStoreCatalogSource:
    marketplace_key: Final = "smartstore"
    contract_version: Final = f"{search.CATALOG_CONTRACT_VERSION}+{FIELDS_VERSION}"

    def __init__(self, caller: SmartStoreEndpointCaller, bearer: Any) -> None:
        self._caller = caller
        self._bearer = bearer

    def available(self) -> bool:
        return self._bearer() is not None

    def enumerate(
        self, *, pause: Callable[[], None], observe: Callable[[object], None] | None = None
    ) -> Enumeration:
        """Every page of the store's catalog, or an outcome that concludes nothing.

        ``observe`` receives each parsed page before retention, for the shape-only runtime
        capture of an operator's sync (ADR-0042 §5.1 item 3); it changes nothing read here."""
        version = search.CATALOG_CONTRACT_VERSION
        bearer = self._bearer()
        if bearer is None:
            return Enumeration(UNAVAILABLE, version, error_code="SMARTSTORE_SESSION_UNAVAILABLE")
        pages: list[search.SearchPage] = []
        number = search.FIRST_PAGE
        while True:
            pause()
            try:
                answer = self._caller.call(
                    EndpointId.SMARTSTORE_PRODUCT_SEARCH,
                    CatalogSearchRequest(
                        bearer.access_token,
                        bearer.credential_generation,
                        bearer.session_generation,
                        number,
                        search.CATALOG_PAGE_SIZE,
                    ),
                    body_sink=observe,
                )
                page = search.read_page(answer.retained)
            except SmartStoreCallError as failure:
                outcome = UNAVAILABLE if failure.error_class is ErrorClass.RATE_LIMITED else ERROR
                return Enumeration(outcome, version, pages_read=len(pages), error_code=failure.code)
            except search.SearchContractError as unreadable:
                return Enumeration(
                    ERROR, version, pages_read=len(pages), error_code=unreadable.code
                )
            pages.append(page)
            total = pages[0].total_elements
            if (
                total > search.CATALOG_MAX_ELEMENTS
                or pages[0].total_pages > search.CATALOG_MAX_PAGES
            ):
                return Enumeration(
                    UNAVAILABLE,
                    version,
                    total_elements=total,
                    pages_read=len(pages),
                    error_code="SMARTSTORE_CATALOG_TOO_LARGE",
                )
            if page.last:
                break
            if number >= search.CATALOG_MAX_PAGES:
                return Enumeration(
                    UNAVAILABLE,
                    version,
                    total_elements=total,
                    pages_read=len(pages),
                    error_code="SMARTSTORE_CATALOG_READ_BUDGET_EXCEEDED",
                )
            number += 1
        try:
            search.check_enumeration(pages)
        except search.SearchContractError as inconsistent:
            return Enumeration(
                INCOMPLETE,
                version,
                total_elements=pages[0].total_elements,
                pages_read=len(pages),
                error_code=inconsistent.code,
            )
        entries = tuple(_own_channel(item) for page in pages for item in page.items)
        return Enumeration(
            COMPLETE,
            version,
            entries=entries,
            total_elements=pages[0].total_elements,
            pages_read=len(pages),
        )

    def read(
        self, origin_product_no: str, *, observe: Callable[[object], None] | None = None
    ) -> ListingRead:
        """One listing's allowlisted fields, its provider removal, or a failure. ``observe``
        receives the parsed response before retention (shape-only capture, §5.1 item 3)."""
        bearer = self._bearer()
        if bearer is None:
            return ListingRead(SESSION_UNAVAILABLE, error_code="SMARTSTORE_SESSION_UNAVAILABLE")
        try:
            answer = self._caller.call(
                EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2,
                ProductReadRequest(
                    bearer.access_token,
                    bearer.credential_generation,
                    bearer.session_generation,
                    origin_product_no,
                ),
                body_sink=observe,
            )
        except SmartStoreCallError as failure:
            if failure.error_class is ErrorClass.RATE_LIMITED:
                return ListingRead(RATE_LIMITED, error_code=failure.code)
            if failure.http_status == NOT_FOUND_STATUS:
                return ListingRead(REMOVED, removal_evidence=HTTP_404)
            return ListingRead(READ_FAILED, error_code=failure.code)
        fields = read_fields(answer.retained)
        if fields.sale_status == DELETE_STATUS:
            return ListingRead(REMOVED, removal_evidence=STATUS_DELETE)
        if fields.sale_status is None:
            # Without its documented sale status a read cannot say the listing exists as what.
            return ListingRead(READ_FAILED, error_code="SMARTSTORE_CATALOG_READ_UNREADABLE")
        return ListingRead(OBSERVED, fields=fields)


__all__ = ["FIELDS_VERSION", "SmartStoreCatalogSource", "read_fields"]
