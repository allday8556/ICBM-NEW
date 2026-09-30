"""The adopted SmartStore product search, read for positive-only reconcile only
(ADR-0020 §4 order 2; ADR-0014 §28.2-§28.4; Issue #89 architect resolution 5904349289, S1-S4).

Everything here uses a **fake transport only**: no provider, no network, no LIVE. It pins the
registry-gated seller-code request, the documented page shape read at its exact positions and
types, the local exact-match rule on the STOREFARM channel, the consistent bounded enumeration, and
the rule that never bends — no search result is ever remote absence or a CREATE authorization.
"""

import hashlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from integrations.marketplaces.smartstore import lookup, search
from integrations.marketplaces.smartstore.caller import (
    ProductSearchRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.execution import SmartStoreReconcileLookup
from integrations.marketplaces.smartstore.registry import (
    EndpointId,
    product_search_succeeded,
    resolve,
)
from integrations.marketplaces.smartstore.retention import retain

BEARER = "fixture-access-token-Qx7"
SEARCH_URL = "https://api.commerce.naver.com/external/v1/products/search"
IDENTITY = "icbm-0123456789abcdef0123456789abcdef"
CODE = hashlib.sha256(
    b"smartstore-seller-management-code/v1\0" + IDENTITY.encode("utf-8")
).hexdigest()[:30]
# Another code of the same shape: an exact match to anything but this listing's projection.
OTHER_CODE = CODE[:-1] + ("1" if CODE[-1] != "1" else "2")


class Bearer:
    access_token = BEARER
    credential_generation = 3
    session_generation = 7


def channel(
    origin: int, number: int, *, code: str | None = CODE, service: str = "STOREFARM"
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "originProductNo": origin,
        "channelProductNo": number,
        "channelServiceType": service,
        "name": "상품명은 보관되지 않는다",
    }
    if code is not None:
        entry["sellerManagementCode"] = code
    return entry


def item(origin: int, *channels: dict[str, Any]) -> dict[str, Any]:
    return {"originProductNo": origin, "channelProducts": list(channels), "name": "보관되지 않음"}


def page(
    items: list[dict[str, Any]], *, number: int = 1, total_pages: int = 1, total: int | None = None
) -> dict[str, Any]:
    return {
        "contents": items,
        "page": number,
        "size": search.MAX_PAGE_SIZE,
        "totalElements": len(items) if total is None else total,
        "totalPages": total_pages,
        "first": number == 1,
        "last": number == total_pages,
    }


class Provider:
    """A fake transport answering each request in turn, recording what it was handed."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers[min(len(self.requests), len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body)


def lookup_over(
    provider: Provider, bearer: Callable[[], object] = Bearer
) -> SmartStoreReconcileLookup:
    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))
    return SmartStoreReconcileLookup(caller=caller, bearer=bearer)


def find(provider: Provider, **kwargs: Any) -> dict[str, Any]:
    return dict(
        lookup_over(provider, **kwargs).find(
            marketplace_account_id="mpa-1", listing_identity=IDENTITY
        )
    )


# ---------------------------------------------------------------- the request (S1)


def test_the_search_goes_out_as_the_registry_contract_says_and_nothing_else() -> None:
    provider = Provider(ok(page([])))
    find(provider)
    (sent,) = provider.requests
    assert (sent.method, str(sent.url)) == ("POST", SEARCH_URL)
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["authorization"] == f"Bearer {BEARER}"
    # Exactly the documented seller-code search: the R1 projection, never the internal identity,
    # the first page, the maximum page size, and no invented filter.
    assert json.loads(sent.content) == {
        "searchKeywordType": "SELLER_CODE",
        "sellerManagementCode": CODE,
        "page": 1,
        "size": 500,
    }
    assert IDENTITY.encode() not in sent.content
    contract = resolve(EndpointId.SMARTSTORE_PRODUCT_SEARCH)
    assert contract.mutating is False and not contract.safe_query_keys


@pytest.mark.parametrize(
    ("code", "number", "size"),
    [
        (IDENTITY, 1, 500),
        ("상품명", 1, 500),
        (CODE.upper(), 1, 500),
        (CODE, 0, 500),
        (CODE, True, 500),
        (CODE, 1, 501),
        (CODE, 1, 0),
    ],
    ids=[
        "internal-identity",
        "free-text",
        "uppercase",
        "page-0",
        "page-bool",
        "size-501",
        "size-0",
    ],
)
def test_a_search_outside_the_documented_request_never_reaches_the_wire(
    code: str, number: int, size: int
) -> None:
    provider = Provider(ok(page([])))
    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))
    with pytest.raises(SmartStoreCallError) as refused:
        caller.call(
            EndpointId.SMARTSTORE_PRODUCT_SEARCH,
            ProductSearchRequest(BEARER, 3, 7, code, number, size),
        )
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"
    assert provider.requests == []


# ---------------------------------------------------------------- the response (S2)


@pytest.mark.parametrize(
    "body",
    [
        {"contents": "x", **{k: v for k, v in page([]).items() if k != "contents"}},
        {**page([]), "page": "1"},
        {**page([]), "totalElements": True},
        {**page([]), "last": 1},
        {k: v for k, v in page([]).items() if k != "totalPages"},
        {**page([]), "contents": [1]},
    ],
    ids=["contents-text", "page-text", "total-bool", "last-int", "no-total-pages", "item-scalar"],
)
def test_a_page_outside_the_documented_envelope_fails_the_predicate(body: dict[str, Any]) -> None:
    assert product_search_succeeded(200, body) is False
    assert product_search_succeeded(500, page([])) is False
    assert product_search_succeeded(200, page([])) is True


def test_retention_keeps_the_identities_the_code_and_the_envelope_only() -> None:
    kept = retain(resolve(EndpointId.SMARTSTORE_PRODUCT_SEARCH), page([item(11, channel(11, 21))]))
    assert "name" not in json.dumps(kept, ensure_ascii=False)
    parsed = search.read_page(kept)
    assert parsed.items[0].channels[0] == search.ChannelEntry(11, 21, "STOREFARM", CODE)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["contents"][0].update(originProductNo="11"),
        lambda b: b["contents"][0].update(originProductNo=True),
        lambda b: b["contents"][0]["channelProducts"][0].update(channelProductNo=2.5),
        lambda b: b["contents"][0]["channelProducts"][0].update(originProductNo=99),
        lambda b: b["contents"][0]["channelProducts"][0].update(channelServiceType="OTHER"),
        lambda b: b["contents"][0]["channelProducts"][0].update(sellerManagementCode=5),
        lambda b: b.update(page=0),
        lambda b: b.update(size=501),
    ],
    ids=[
        "origin-text",
        "origin-bool",
        "channel-float",
        "channel-names-other-origin",
        "undocumented-channel",
        "code-number",
        "page-zero",
        "size-over-max",
    ],
)
def test_a_page_is_read_at_its_documented_positions_and_types_only(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    body = page([item(11, channel(11, 21))])
    mutate(body)
    with pytest.raises(search.SearchContractError):
        search.read_page(body)


# ---------------------------------------------------------------- the local exact match (§28.2)


def test_only_an_exact_storefarm_code_is_a_candidate() -> None:
    parsed = search.read_page(
        page(
            [
                item(11, channel(11, 21), channel(11, 22, service="WINDOW")),
                item(12, channel(12, 23, code=CODE[:-1])),  # a partial match
                item(13, channel(13, 24, code=CODE + "0")),  # a similar match
                item(14, channel(14, 25, code=None)),  # no code at all
                item(15, channel(15, 26, code=CODE, service="AFFILIATE")),
            ]
        )
    )
    assert search.exact_candidates([parsed], CODE) == (search.Candidate(11, 21),)


@pytest.mark.parametrize(
    ("pages", "code"),
    [
        ([page([item(1, channel(1, 2))], number=2)], "SEARCH_RESPONSE_INCONSISTENT"),
        ([page([], total_pages=2)], "SEARCH_ENUMERATION_INCOMPLETE"),
        ([page([item(1, channel(1, 2))], total=2)], "SEARCH_RESPONSE_INCONSISTENT"),
        (
            [page([], total_pages=2, total=0), page([], number=2, total_pages=3, total=0)],
            "SEARCH_RESPONSE_INCONSISTENT",
        ),
        (
            # Drift: product 1 shifted onto page 2 as well, so another product was never read.
            [
                page([item(1, channel(1, 2))], total_pages=2, total=2),
                page([item(1, channel(1, 2))], number=2, total_pages=2, total=2),
            ],
            "SEARCH_RESPONSE_INCONSISTENT",
        ),
    ],
    ids=[
        "page-answers-as-another",
        "last-page-unread",
        "count-disagrees",
        "totals-moved",
        "product-read-twice",
    ],
)
def test_an_inconsistent_enumeration_is_never_a_count(
    pages: list[dict[str, Any]], code: str
) -> None:
    with pytest.raises(search.SearchContractError) as refused:
        search.check_enumeration([search.read_page(p) for p in pages])
    assert refused.value.code == code


# ---------------------------------------------------------------- the lookup (§28.2-§28.4)


def test_a_complete_enumeration_names_exactly_the_exact_candidates() -> None:
    first = page([item(11, channel(11, 21))], total_pages=2, total=2)
    second = page([item(12, channel(12, 22, code="0" * 30))], number=2, total_pages=2, total=2)
    provider = Provider(ok(first), ok(second))
    found = find(provider)
    assert found["status"] == "COMPLETE" and found["pages_read"] == 2
    assert found["candidates"] == [{"origin_product_no": "11", "channel_product_no": "21"}]
    assert [json.loads(r.content)["page"] for r in provider.requests] == [1, 2]


def test_zero_candidates_is_a_count_and_never_absence() -> None:
    found = find(Provider(ok(page([]))))
    # A trustworthy count of zero exact candidates. What it proves is decided by REGISTER, and
    # there it is never absence (§17.2): the evidence carries no absence claim of any kind.
    assert found["status"] == "COMPLETE" and found["candidates"] == []
    assert not any("absen" in str(key).lower() for key in found)


def test_a_result_larger_than_the_read_budget_is_unavailable_never_partial() -> None:
    pages = search.MAX_PAGES_PER_CHECK + 1
    answers = [ok(page([], number=n, total_pages=pages, total=0)) for n in range(1, pages + 1)]
    provider = Provider(*answers)
    found = find(provider)
    assert found["status"] == "UNAVAILABLE"
    assert found["code"] == "SMARTSTORE_SEARCH_READ_BUDGET_EXCEEDED"
    assert found["candidates"] == []
    assert len(provider.requests) == search.MAX_PAGES_PER_CHECK


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (httpx.Response(429, json={"code": "GW.RATE_LIMIT"}), "UNAVAILABLE"),
        (httpx.Response(429, json={"code": "GW.QUOTA_LIMIT"}), "UNAVAILABLE"),
        (httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"}), "ERROR"),
        (httpx.Response(200, json={"contents": "x"}), "ERROR"),
        (httpx.ReadTimeout("timed out"), "ERROR"),
    ],
    ids=["rate-limit", "quota", "server-error", "undocumented-page", "timeout"],
)
def test_a_failed_lookup_proves_nothing(answer: httpx.Response | Exception, status: str) -> None:
    found = find(Provider(answer))
    # §28.4: a rate or quota refusal defers; anything else is an error. Neither names a candidate.
    assert found["status"] == status
    assert found["candidates"] == []


def test_without_a_session_nothing_is_read() -> None:
    provider = Provider(ok(page([])))
    found = find(provider, bearer=lambda: None)
    assert (found["status"], found["code"]) == ("UNAVAILABLE", "SMARTSTORE_SESSION_UNAVAILABLE")
    assert provider.requests == []


def test_a_listing_identity_that_has_no_projection_is_an_error() -> None:
    provider = Provider(ok(page([])))
    found = dict(lookup_over(provider).find(marketplace_account_id="mpa-1", listing_identity=""))
    assert found["status"] == "ERROR" and provider.requests == []


def test_a_candidate_is_confirmed_only_by_a_readback_carrying_the_exact_code() -> None:
    reads = lookup_over(Provider(ok(page([]))))
    carrying = {
        "originProduct": {
            "name": "상품",
            "detailAttribute": {"sellerCodeInfo": {"sellerManagementCode": CODE}},
        }
    }
    other = {
        "originProduct": {
            "name": "상품",
            "detailAttribute": {"sellerCodeInfo": {"sellerManagementCode": OTHER_CODE}},
        }
    }
    assert reads.confirms_candidate(listing_identity=IDENTITY, retained_readback=carrying) is True
    assert reads.confirms_candidate(listing_identity=IDENTITY, retained_readback=other) is False
    assert reads.confirms_candidate(listing_identity=IDENTITY, retained_readback={}) is False


def test_the_search_is_never_duplicate_absence_evidence() -> None:
    # ADR-0014 §13, §17.2: adopting the search for positive reconcile adopts no duplicate lookup.
    duplicate = lookup.SmartStoreDuplicateLookup()
    assert duplicate.available() is False
    with pytest.raises(lookup.DuplicateLookupUnavailableError):
        duplicate.evidence(marketplace_account_id="mpa-1", listing_identity=IDENTITY)


def test_a_product_repeated_across_pages_is_never_a_unique_candidate() -> None:
    # Pagination drift must not turn one exact candidate seen twice into a unique positive result.
    first = page([item(11, channel(11, 21))], total_pages=2, total=2)
    second = page([item(11, channel(11, 21))], number=2, total_pages=2, total=2)
    found = find(Provider(ok(first), ok(second)))
    assert found["status"] == "ERROR" and found["candidates"] == []
    assert found["code"] == "SEARCH_RESPONSE_INCONSISTENT"
