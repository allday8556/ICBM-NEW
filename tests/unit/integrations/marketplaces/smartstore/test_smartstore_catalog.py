"""The SmartStore store-catalog source (ADR-0042 S1a, §5; ``ENDPOINT_MATRIX.md`` §4.1.2).

Everything here uses a **fake transport only** through the real registry-gated caller: no
provider, no network, synthetic values. Proven here: the catalog request is exactly the
paging-only body (E1) and nothing else reaches the wire; an enumeration is COMPLETE only when every
page reads and agrees (§5.2); a rate limit, a lost session or a catalog over 100,000 is
UNAVAILABLE; anything else concludes nothing; the origin read keeps only the recorded catalog
leaves and the normalizer reads each at exactly its path; DELETE and a 404 end a listing.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

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
)
from integrations.marketplaces.smartstore import search
from integrations.marketplaces.smartstore.caller import (
    CatalogSearchRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.catalog import (
    FIELDS_VERSION,
    SmartStoreCatalogSource,
    read_fields,
)
from integrations.marketplaces.smartstore.registry import EndpointId, resolve
from integrations.marketplaces.smartstore.retention import retain

BEARER = "fixture-access-token-Cq3"
SEARCH_PATH = "/external/v1/products/search"
READ_PREFIX = "/external/v2/products/origin-products/"
IMAGE = "https://shop-phinf.pstatic.net/fixture/main.jpg"


class Bearer:
    access_token = BEARER
    credential_generation = 2
    session_generation = 5


def item(origin: int, channel: int | None = None, code: str = "S-1") -> dict[str, Any]:
    channels = []
    if channel is not None:
        channels.append(
            {
                "originProductNo": origin,
                "channelProductNo": channel,
                "channelServiceType": "STOREFARM",
                "sellerManagementCode": code,
            }
        )
    return {"originProductNo": origin, "channelProducts": channels}


def page(
    items: list[dict[str, Any]], *, number: int = 1, total_pages: int = 1, total: int | None = None
) -> dict[str, Any]:
    return {
        "contents": items,
        "page": number,
        "size": search.CATALOG_PAGE_SIZE,
        "totalElements": len(items) if total is None else total,
        "totalPages": total_pages,
        "first": number == 1,
        "last": number == total_pages,
    }


def origin_read(status: str = "SALE") -> dict[str, Any]:
    """A synthetic origin read: every recorded catalog leaf, and unrecorded ones beside them."""
    return {
        "originProduct": {
            "statusType": status,
            "name": "합성 상품 A",
            "salePrice": 12900,
            "stockQuantity": 7,
            "leafCategoryId": "50000123",
            "detailContent": "<p>본문은 보관되지 않는다</p>",
            "customerBenefit": {"immediateDiscountPolicy": {"value": 1000, "unitType": "WON"}},
            "images": {
                "representativeImage": {"url": IMAGE},
                "optionalImages": [
                    {"url": "https://shop-phinf.pstatic.net/fixture/b.jpg"},
                    {"url": "https://example.test/x.jpg?token=abc"},
                ],
            },
            "deliveryInfo": {
                "deliveryType": "DELIVERY",
                "deliveryAttributeType": "NORMAL",
                "deliveryFee": {
                    "deliveryFeeType": "CONDITIONAL_FREE",
                    "baseFee": 3000,
                    "freeConditionalAmount": 30000,
                    "deliveryFeePayType": "PREPAID",
                },
                "claimDeliveryInfo": {"returnDeliveryFee": 3000, "returnAddressId": 77},
            },
            "detailAttribute": {
                "sellerCodeInfo": {
                    "sellerManagementCode": "S-1",
                    "sellerBarcode": "8801234567893",
                    "sellerCustomCode1": "C1",
                },
                "afterServiceInfo": {
                    "afterServiceTelephoneNumber": "02-0000-0000",
                    "afterServiceGuideContent": "합성 안내문",
                },
                "naverShoppingSearchInfo": {"modelName": "모델 X", "brandName": "브랜드 Y"},
                "optionInfo": {
                    "optionCombinationGroupNames": {"optionGroupName1": "색상"},
                    "optionCombinations": [
                        {
                            "id": 9001,
                            "optionName1": "빨강",
                            "stockQuantity": 3,
                            "price": -500,
                            "usable": True,
                            "sellerManagerCode": "S-1-R",
                        }
                    ],
                },
            },
        },
        "smartstoreChannelProduct": {"channelProductDisplayStatusType": "ON"},
        "windowChannelProduct": {"channelProductDisplayStatusType": "SUSPENSION"},
    }


Answer = httpx.Response | Exception


class Provider:
    """A fake transport: search pages in turn, and origin reads by product number."""

    def __init__(self, pages: list[Answer], reads: dict[str, Answer] | None = None) -> None:
        self.pages = list(pages)
        self.reads = reads or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == SEARCH_PATH:
            searched = [r for r in self.requests if r.url.path == SEARCH_PATH]
            answer = self.pages[min(len(searched), len(self.pages)) - 1]
        else:
            answer = self.reads[request.url.path.removeprefix(READ_PREFIX)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body)


def source(provider: Provider, bearer: Callable[[], object] = Bearer) -> SmartStoreCatalogSource:
    return SmartStoreCatalogSource(
        SmartStoreEndpointCaller(transport=httpx.MockTransport(provider)), bearer
    )


def no_pause() -> None:
    return None


# ---------------------------------------------------------------- the request (E1)


def test_the_catalog_request_is_the_paging_only_body_and_nothing_else() -> None:
    provider = Provider([ok(page([]))])
    found = source(provider).enumerate(pause=no_pause)
    assert found.outcome == COMPLETE and found.entries == () and found.total_elements == 0
    (sent,) = provider.requests
    assert (sent.method, sent.url.path) == ("POST", SEARCH_PATH)
    assert sent.headers["authorization"] == f"Bearer {BEARER}"
    # E1: only the paging fields. E2: no status filter and no sort parameter is invented.
    assert json.loads(sent.content) == {"page": 1, "size": 500}
    assert not sent.url.query


@pytest.mark.parametrize(
    ("number", "size"),
    [(0, 500), (True, 500), (search.CATALOG_MAX_PAGES + 1, 500), (1, 501), (1, 0)],
    ids=["page-0", "page-bool", "page-over-budget", "size-501", "size-0"],
)
def test_a_catalog_request_outside_the_contract_never_reaches_the_wire(
    number: int, size: int
) -> None:
    provider = Provider([ok(page([]))])
    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))
    with pytest.raises(SmartStoreCallError) as refused:
        caller.call(
            EndpointId.SMARTSTORE_PRODUCT_SEARCH,
            CatalogSearchRequest(BEARER, 2, 5, number, size),
        )
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"
    assert provider.requests == []


def test_the_page_budget_covers_exactly_the_documented_limit() -> None:
    assert search.CATALOG_MAX_ELEMENTS == 100_000
    assert search.CATALOG_MAX_PAGES * search.CATALOG_PAGE_SIZE == search.CATALOG_MAX_ELEMENTS


# ---------------------------------------------------------------- the enumeration (§5.2)


def test_every_page_is_read_and_the_own_store_channel_is_named() -> None:
    first = page([item(11, 21, "A"), item(12, None)], number=1, total_pages=2, total=3)
    second = page([item(13, 23, "C")], number=2, total_pages=2, total=3)
    paused: list[int] = []
    provider = Provider([ok(first), ok(second)])
    found = source(provider).enumerate(pause=lambda: paused.append(1))
    assert found.outcome == COMPLETE
    assert (found.total_elements, found.pages_read) == (3, 2)
    assert [json.loads(r.content)["page"] for r in provider.requests] == [1, 2]
    assert len(paused) == 2  # the owner paces every provider call
    by_origin = {e.origin_product_no: e for e in found.entries}
    assert (by_origin["11"].channel_product_no, by_origin["11"].listed_seller_code) == ("21", "A")
    # No own-store channel: nothing is guessed.
    assert (by_origin["12"].channel_product_no, by_origin["12"].own_channels) == (None, 0)


@pytest.mark.parametrize(
    "pages",
    [
        # The totals move between pages.
        [
            page([item(1, 2)], number=1, total_pages=2, total=2),
            page([item(3, 4)], number=2, total_pages=2, total=3),
        ],
        # Page 2 answers as page 1.
        [
            page([item(1, 2)], number=1, total_pages=2, total=2),
            {**page([item(3, 4)], number=2, total_pages=2, total=2), "page": 1},
        ],
        # A product read twice across pages.
        [
            page([item(1, 2)], number=1, total_pages=2, total=2),
            page([item(1, 2)], number=2, total_pages=2, total=2),
        ],
        # Fewer distinct products than totalElements.
        [page([item(1, 2)], total=2)],
    ],
    ids=["totals-move", "page-repeated", "product-twice", "short"],
)
def test_an_inconsistent_enumeration_is_incomplete_and_lists_nothing(
    pages: list[dict[str, Any]],
) -> None:
    found = source(Provider([ok(body) for body in pages])).enumerate(pause=no_pause)
    assert found.outcome == INCOMPLETE and found.entries == ()
    assert found.error_code is not None


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (httpx.Response(429, json={"code": "GW.RATE_LIMIT"}), UNAVAILABLE),
        (httpx.Response(429, json={"code": "GW.QUOTA_LIMIT"}), UNAVAILABLE),
        # E6: the over-100,000 error has no documented code; any provider error proves nothing.
        (httpx.Response(400, json={"code": "BAD_REQUEST"}), ERROR),
        (httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"}), ERROR),
        (httpx.Response(200, json={"contents": "x"}), ERROR),
        (httpx.ReadTimeout("timed out"), ERROR),
    ],
    ids=["rate-limit", "quota", "bad-request", "server-error", "undocumented-page", "timeout"],
)
def test_a_failed_enumeration_concludes_nothing(answer: Answer, outcome: str) -> None:
    found = source(Provider([answer])).enumerate(pause=no_pause)
    assert (found.outcome, found.entries) == (outcome, ())


def test_a_catalog_over_the_documented_limit_is_unavailable_never_partial() -> None:
    provider = Provider([ok(page([item(1, 2)], total_pages=201, total=100_001))])
    found = source(provider).enumerate(pause=no_pause)
    assert (found.outcome, found.error_code) == (UNAVAILABLE, "SMARTSTORE_CATALOG_TOO_LARGE")
    assert found.entries == () and len(provider.requests) == 1


def test_without_a_session_nothing_is_read() -> None:
    provider = Provider([ok(page([]))])
    found = source(provider, bearer=lambda: None).enumerate(pause=no_pause)
    assert (found.outcome, found.error_code) == (UNAVAILABLE, "SMARTSTORE_SESSION_UNAVAILABLE")
    assert provider.requests == []
    assert source(provider, bearer=lambda: None).read("11").result == SESSION_UNAVAILABLE


# ---------------------------------------------------------------- one listing (§5.3, E4)


def test_the_origin_read_keeps_only_the_recorded_catalog_leaves() -> None:
    kept = retain(resolve(EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2), origin_read())
    text = json.dumps(kept, ensure_ascii=False)
    for dropped in (
        "detailContent",
        "본문",
        "customerBenefit",
        "immediateDiscountPolicy",
        "modelName",
        "brandName",
        "returnAddressId",
    ):
        assert dropped not in text
    assert kept["originProduct"]["detailAttribute"]["sellerCodeInfo"]["sellerBarcode"] == (
        "8801234567893"
    )


def test_each_field_is_read_at_exactly_its_recorded_path() -> None:
    kept = retain(resolve(EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2), origin_read())
    fields = read_fields(kept)
    assert fields.fields_version == FIELDS_VERSION
    # The SmartStore display status, never the window channel's.
    assert (fields.sale_status, fields.display_status) == ("SALE", "ON")
    assert (fields.name, fields.sale_price, fields.stock_quantity) == ("합성 상품 A", 12900, 7)
    assert (fields.seller_management_code, fields.seller_barcode) == ("S-1", "8801234567893")
    assert fields.leaf_category_id == "50000123"
    canonical = fields.canonical
    assert canonical["seller_keys"]["seller_custom_code1"] == "C1"
    assert canonical["seller_keys"]["seller_custom_code2"] is None
    assert canonical["shipping"] == {
        "delivery_type": "DELIVERY",
        "delivery_attribute_type": "NORMAL",
        "delivery_fee_type": "CONDITIONAL_FREE",
        "base_fee": 3000,
        "free_conditional_amount": 30000,
        "delivery_fee_pay_type": "PREPAID",
    }
    assert canonical["customer_service"] == {"telephone": "02-0000-0000", "guide": "합성 안내문"}
    assert canonical["options"]["group_names"] == ["색상", None, None, None]
    (combination,) = canonical["options"]["combinations"]
    assert combination == {
        "id": "9001",
        "names": ["빨강", None, None, None],
        "stock_quantity": 3,
        "price": -500,
        "usable": True,
        "seller_manager_code": "S-1-R",
    }
    # A signed or tokenized image reference is counted, never kept.
    assert canonical["media"] == {
        "representative": IMAGE,
        "optional": ["https://shop-phinf.pstatic.net/fixture/b.jpg"],
        "unsafe": 1,
    }
    # No field without a recorded path is ever present (E4 NOT_CAPTURED), not even as null.
    assert set(canonical) == {
        "fields_version",
        "state",
        "name",
        "price",
        "stock",
        "seller_keys",
        "category",
        "options",
        "shipping",
        "customer_service",
        "media",
    }
    assert "discount" not in json.dumps(canonical)


def test_a_leaf_elsewhere_or_of_another_type_is_never_taken() -> None:
    body = {
        "originProduct": {
            "statusType": "SALE",
            "salePrice": "12900",
            "stockQuantity": True,
            "sellerBarcode": "misplaced",
            "detailAttribute": {"sellerCodeInfo": {"sellerBarcode": 8801234567893}},
        },
        "smartstoreChannelProduct": {"channelProductDisplayStatusType": "SOMETHING"},
        "sellerManagementCode": "top-level is not the path",
    }
    fields = read_fields(body)
    assert fields.sale_price is None and fields.stock_quantity is None
    assert fields.seller_barcode is None and fields.seller_management_code is None
    assert fields.display_status is None


def test_secret_material_in_a_text_field_is_never_kept() -> None:
    body = origin_read()
    service = body["originProduct"]["detailAttribute"]["afterServiceInfo"]
    service["afterServiceGuideContent"] = "Bearer abcdefghijklmnopqrstuv"
    fields = read_fields(body)
    assert fields.canonical["customer_service"]["guide"] is None


@pytest.mark.parametrize(
    ("answer", "result", "evidence"),
    [
        (ok(origin_read()), OBSERVED, None),
        (ok(origin_read("SUSPENSION")), OBSERVED, None),
        (ok(origin_read("DELETE")), REMOVED, STATUS_DELETE),
        (httpx.Response(404, json={"code": "NOT_FOUND"}), REMOVED, HTTP_404),
        (httpx.Response(429, json={"code": "GW.RATE_LIMIT"}), RATE_LIMITED, None),
        (httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"}), READ_FAILED, None),
        (ok({"originProduct": {"name": "상태 없음"}}), READ_FAILED, None),
        (httpx.ReadTimeout("timed out"), READ_FAILED, None),
    ],
    ids=[
        "sale",
        "suspension",
        "delete",
        "404",
        "rate-limit",
        "server-error",
        "no-status",
        "timeout",
    ],
)
def test_only_provider_evidence_ends_a_listing(
    answer: Answer, result: str, evidence: str | None
) -> None:
    read = source(Provider([], {"11": answer})).read("11")
    assert (read.result, read.removal_evidence) == (result, evidence)
    assert (read.fields is not None) == (result == OBSERVED)
    assert (read.error_code is not None) == (result in (RATE_LIMITED, READ_FAILED))
