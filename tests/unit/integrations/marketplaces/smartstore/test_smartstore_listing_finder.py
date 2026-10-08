"""Finding one SmartStore listing by an exact seller code (M6-E; ADR-0024 §3).

The caller is a fake answering the adopted search and origin read with retained bodies. Proven
here: only an exact STOREFARM candidate is a candidate; zero is NOT_FOUND and more than one is
AMBIGUOUS; the read-back must carry the same code and not DELETE; a rate limit and any failure
adopt nothing; the owner's pause runs before every provider call; and the adoption-code shape is
the only other code a search may carry.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.stages.operate.adoption_facts import (
    AMBIGUOUS,
    DELETED,
    FAILED,
    FOUND,
    MISMATCH,
    NOT_FOUND,
    RATE_LIMITED,
    UNAVAILABLE,
)
from integrations.marketplaces.smartstore.caller import (
    ProductSearchRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.classify import Classification
from integrations.marketplaces.smartstore.listing_finder import SmartStoreListingFinder
from integrations.marketplaces.smartstore.registry import EndpointId
from integrations.marketplaces.smartstore.transmission import Phase


def _page(*entries: tuple[int, int, str, str]) -> dict[str, Any]:
    return {
        "contents": [
            {
                "originProductNo": origin,
                "channelProducts": [
                    {
                        "originProductNo": origin,
                        "channelProductNo": channel,
                        "channelServiceType": service,
                        "sellerManagementCode": code,
                    }
                ],
            }
            for origin, channel, service, code in entries
        ],
        "page": 1,
        "size": 500,
        "totalElements": len(entries),
        "totalPages": 1,
        "first": True,
        "last": True,
    }


def _read(code: str, status: str = "SALE") -> dict[str, Any]:
    return {
        "originProduct": {"statusType": status, "salePrice": 25000, "stockQuantity": 3},
        "smartstoreChannelProduct": {
            "channelProductDisplayStatusType": "ON",
            "sellerManagementCode": code,
        },
    }


class Caller:
    def __init__(self, search: Any, read: Any = None) -> None:
        self.search = search
        self.read = read
        self.calls: list[object] = []

    def call(self, endpoint_id: object, request: Any) -> Any:
        self.calls.append(endpoint_id)
        answer = self.search if endpoint_id is EndpointId.SMARTSTORE_PRODUCT_SEARCH else self.read
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(retained=answer, http_status=200)


def _bearer() -> Any:
    return SimpleNamespace(access_token="token", credential_generation=3, session_generation=5)


def _find(caller: Caller, code: str = "km287") -> Any:
    pauses: list[int] = []
    finder = SmartStoreListingFinder(caller, _bearer, pause=lambda: pauses.append(1))  # type: ignore[arg-type]
    result = finder.find(code)
    assert len(pauses) == len(caller.calls)
    return result


def test_one_exact_storefarm_candidate_with_a_matching_read_back_is_found() -> None:
    caller = Caller(_page((9001, 7001, "STOREFARM", "km287")), _read("km287"))
    found = _find(caller)
    assert (found.outcome, found.origin_product_no, found.channel_product_no) == (
        FOUND,
        "9001",
        "7001",
    )
    assert (found.sale_status, found.display_status) == ("SALE", "ON")
    assert caller.calls == [
        EndpointId.SMARTSTORE_PRODUCT_SEARCH,
        EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2,
    ]


@pytest.mark.parametrize(
    ("page", "outcome"),
    (
        (_page(), NOT_FOUND),
        (_page((9001, 7001, "STOREFARM", "km2870")), NOT_FOUND),
        (_page((9001, 7001, "WINDOW", "km287")), NOT_FOUND),
        (_page((9001, 7001, "STOREFARM", "km287"), (9002, 7002, "STOREFARM", "km287")), AMBIGUOUS),
    ),
)
def test_only_one_exact_storefarm_candidate_is_ever_read_back(page: Any, outcome: str) -> None:
    caller = Caller(page, _read("km287"))
    assert _find(caller).outcome == outcome
    assert caller.calls == [EndpointId.SMARTSTORE_PRODUCT_SEARCH]


@pytest.mark.parametrize(
    ("read", "outcome"),
    ((_read("km288"), MISMATCH), (_read("km287", status="DELETE"), DELETED), ({}, MISMATCH)),
)
def test_the_read_back_must_carry_the_code_and_not_delete(read: Any, outcome: str) -> None:
    caller = Caller(_page((9001, 7001, "STOREFARM", "km287")), read)
    assert _find(caller).outcome == outcome


def _error(code: str, error_class_rate: bool) -> SmartStoreCallError:
    from app.platform.core.errors import ErrorClass
    from integrations.marketplaces.smartstore.classify import Basis, FailureLayer

    classification = Classification(
        ErrorClass.RATE_LIMITED if error_class_rate else ErrorClass.UNKNOWN,
        FailureLayer.GATEWAY,
        Basis.DOCUMENTED_EXACT,
        code,
        None,
    )
    from app.stages.connect.marketplace.capability import RemoteOutcome

    return SmartStoreCallError(
        "SMARTSTORE_PRODUCT_SEARCH", classification, Phase.RESPONSE_RECEIVED, RemoteOutcome.UNKNOWN
    )


def test_a_failure_adopts_nothing() -> None:
    assert _find(Caller(_error("SMARTSTORE_RATE_LIMITED", True))).outcome == RATE_LIMITED
    assert _find(Caller(_error("SMARTSTORE_HTTP_400", False))).outcome == FAILED
    assert _find(Caller({"contents": "x"})).outcome == FAILED
    finder = SmartStoreListingFinder(Caller(_page()), lambda: None)  # type: ignore[arg-type]
    assert finder.available() is False
    assert finder.find("km287").outcome == UNAVAILABLE


@pytest.mark.parametrize("code", ["km287", "abc1"])
def test_an_adoption_code_may_be_searched(code: str) -> None:
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_page())

    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(handler))
    caller.call(
        EndpointId.SMARTSTORE_PRODUCT_SEARCH, ProductSearchRequest("token", 3, 5, code, 1, 500)
    )
    assert code.encode() in seen[0].content


@pytest.mark.parametrize(
    "code", ["KM287", "km", "287", "km-287", "k287", "km287 ", "km" + "1" * 16]
)
def test_any_other_code_is_refused_before_transport(code: str) -> None:
    import httpx

    seen: list[httpx.Request] = []
    caller = SmartStoreEndpointCaller(
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))
    )
    with pytest.raises(SmartStoreCallError) as refused:
        caller.call(
            EndpointId.SMARTSTORE_PRODUCT_SEARCH, ProductSearchRequest("token", 3, 5, code, 1, 500)
        )
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"
    assert seen == []
