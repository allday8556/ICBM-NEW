"""M5 PR-D: the transport contract of the two adopted product read-backs.

The facts pinned here are the packet's (Issue #89 comment 5746489554) plus ICBM's own transport
policy: the exact method and URL, the bearer, no query at all, no redirect following, the
endpoint's own timeouts, the success predicate, and the retention boundary — REGISTER receives the
allow-listed leaves, never a provider body. Every call goes through a fake transport; no test in
this file can reach the network, and no adopted endpoint mutates anything.
"""

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.core import egress
from app.core.egress import EGRESS
from integrations.marketplaces.smartstore.caller import (
    ProductReadback,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.caller import (
    ProductReadRequest as ReadRequest,
)
from integrations.marketplaces.smartstore.registry import EndpointId, resolve

ORIGIN = EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2
CHANNEL = EndpointId.SMARTSTORE_CHANNEL_PRODUCT_READ_V2
BEARER = "fixture-access-token-Qx7"
PRODUCT_NO = "1234567890"
BASE = "https://api.commerce.naver.com/external"
IDENTITY = "icbm-0123456789abcdef0123456789abcdef"
REF = "https://shop-phinf.example/a/main.jpg"
BODY: dict[str, Any] = {
    "originProduct": {
        "name": "테스트 상품",
        "salePrice": 19900,
        "stockQuantity": 10,
        "detailContent": "<p>본문</p>",
        "images": [{"url": REF}],
        "sellerCodeInfo": {"sellerManagementCode": IDENTITY},
    },
    "traceId": "trace-1",
}

Respond = Callable[[httpx.Request], httpx.Response]


class Provider:
    """A fake SmartStore that records each request and the egress grant in force."""

    def __init__(self, respond: Respond) -> None:
        self._respond = respond
        self.requests: list[httpx.Request] = []
        self.grants: list[object] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.grants.append(egress._active_grant.get())
        return self._respond(request)


def _json(body: object, status: int = 200) -> Respond:
    return lambda request: httpx.Response(status, json=body)


def _read(
    provider: Provider, endpoint: EndpointId = ORIGIN, product_no: str = PRODUCT_NO
) -> ProductReadback:
    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))
    return caller.call(endpoint, ReadRequest(BEARER, 3, 7, product_no))


@pytest.mark.parametrize(
    ("endpoint", "url"),
    [
        (ORIGIN, f"{BASE}/v2/products/origin-products/{PRODUCT_NO}"),
        (CHANNEL, f"{BASE}/v2/products/channel-products/{PRODUCT_NO}"),
    ],
)
def test_the_read_goes_to_the_contract_url_with_the_bearer_and_no_query(
    endpoint: EndpointId, url: str
) -> None:
    provider = Provider(_json(BODY))
    _read(provider, endpoint)
    (request,) = provider.requests
    assert (request.method, str(request.url)) == ("GET", url)
    assert request.url.query == b""
    assert request.headers["authorization"] == f"Bearer {BEARER}"
    assert request.headers["accept"] == "application/json"
    assert "content-type" not in request.headers
    assert request.content == b""
    # The egress grant is open for the provider host alone, for this one call.
    (grant,) = provider.grants
    assert isinstance(grant, egress._Grant)
    assert (grant.owner, grant.hosts) == (
        "marketplace:smartstore",
        frozenset({"api.commerce.naver.com"}),
    )
    assert egress._active_grant.get() is None


def test_only_the_retained_leaves_cross_the_boundary() -> None:
    result = _read(Provider(_json(BODY)))
    assert isinstance(result, ProductReadback)
    assert (result.endpoint_id, result.product_no, result.http_status) == (ORIGIN, PRODUCT_NO, 200)
    assert result.retained == {
        "originProduct": {
            "images": [{"url": REF}],
            "name": "테스트 상품",
            "salePrice": 19900,
            "sellerCodeInfo": {"sellerManagementCode": IDENTITY},
            "stockQuantity": 10,
        }
    }
    # Neither an unlisted business field nor provider trace material reaches REGISTER.
    assert "detailContent" not in str(result.retained)
    assert "trace" not in str(result.retained)


def test_the_endpoint_timeouts_and_redirect_policy_are_the_contract_values() -> None:
    contract = resolve(ORIGIN)
    seen: dict[str, Any] = {}

    class Recording(httpx.MockTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            seen["timeout"] = request.extensions.get("timeout")
            return httpx.Response(200, json=BODY)

    caller = SmartStoreEndpointCaller(transport=Recording(lambda r: httpx.Response(200)))
    caller.call(ORIGIN, ReadRequest(BEARER, 3, 7, PRODUCT_NO))
    assert seen["timeout"] == {
        "connect": contract.connect_timeout_s,
        "read": contract.read_timeout_s,
        "write": contract.connect_timeout_s,
        "pool": contract.connect_timeout_s,
    }


def test_a_redirect_is_never_followed() -> None:
    provider = Provider(
        lambda request: httpx.Response(302, headers={"location": "https://evil.example/x"})
    )
    with pytest.raises(SmartStoreCallError) as refused:
        _read(provider)
    assert len(provider.requests) == 1
    assert refused.value.http_status == 302


@pytest.mark.parametrize("body", [None, [], "product", {"originProduct": None}])
def test_the_success_predicate_needs_a_json_object(body: object) -> None:
    # A 200 that is not a JSON object fails the predicate; a JSON object passes and the
    # normalizer, not the predicate, decides whether it carries a product.
    status = 200
    provider = Provider(lambda request: httpx.Response(status, json=body))
    if isinstance(body, dict):
        assert _read(provider).retained == {}
        return
    with pytest.raises(SmartStoreCallError):
        _read(provider)


def test_a_non_200_read_is_a_classified_failure_not_a_result() -> None:
    provider = Provider(_json({"code": "NOT_FOUND"}, status=404))
    with pytest.raises(SmartStoreCallError) as failed:
        _read(provider)
    assert failed.value.http_status == 404
    # A response came back, so the phase is RESPONSE_RECEIVED; a read never claims an outcome of
    # its own, and the adopted reads mutate nothing either way.
    assert failed.value.phase.value == "RESPONSE_RECEIVED"


@pytest.mark.parametrize(
    "product_no",
    [
        "",
        "../../v1/seller/account",
        "12345678901234567890123456789012345678901234567890123456789012345",
        "a b",
        "1/2",
    ],
)
def test_an_unusable_path_value_fails_before_the_transport(
    product_no: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached: list[str] = []
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: reached.append("client") or (_ for _ in ()).throw(AssertionError()),
    )
    monkeypatch.setattr(
        EGRESS,
        "grant",
        lambda *a, **k: reached.append("grant") or (_ for _ in ()).throw(AssertionError()),
    )
    caller = SmartStoreEndpointCaller()
    with pytest.raises(SmartStoreCallError) as refused:
        caller.call(ORIGIN, ReadRequest(BEARER, 3, 7, product_no))
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"
    assert reached == []


@pytest.mark.parametrize(
    "request_object",
    [object(), None, "1234567890"],
)
def test_a_foreign_request_object_is_a_local_contract_violation(request_object: object) -> None:
    caller = SmartStoreEndpointCaller()
    with pytest.raises(SmartStoreCallError) as refused:
        caller.call(ORIGIN, request_object)
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"


@pytest.mark.parametrize(("credentials", "session"), [(0, 7), (3, 0)])
def test_an_uncommitted_session_never_reads(credentials: int, session: int) -> None:
    caller = SmartStoreEndpointCaller()
    with pytest.raises(SmartStoreCallError) as refused:
        caller.call(ORIGIN, ReadRequest(BEARER, credentials, session, PRODUCT_NO))
    assert refused.value.code == "SMARTSTORE_SESSION_NOT_COMMITTED"


def test_the_evidence_line_names_the_endpoint_and_keeps_no_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="icbm.connect.smartstore"):
        _read(Provider(_json(BODY)))
    (record,) = [r for r in caplog.records if r.getMessage() == "smartstore.request"]
    assert record.endpoint_id == ORIGIN.value  # type: ignore[attr-defined]
    assert record.predicate_revision == "m5d-origin-read-r1"  # type: ignore[attr-defined]
    assert record.http_status == 200  # type: ignore[attr-defined]
    assert record.provider_trace_id == "trace-1"  # type: ignore[attr-defined]
    assert "테스트 상품" not in caplog.text and IDENTITY not in caplog.text
