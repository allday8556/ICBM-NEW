"""The adopted SmartStore origin-product DELETE (ADR-0018 §3.5) against a fake transport.

APPLIED_PROVEN only for the documented success; NOT_APPLIED_PROVEN only for the
transmission-precluded whitelist; everything else UNKNOWN. Nothing is retained or resent.
"""

import httpx
import pytest

from app.stages.connect.marketplace.capability import RemoteOutcome
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from integrations.marketplaces.smartstore.deletion import SmartStoreDeleteSender
from integrations.marketplaces.smartstore.registry import (
    EndpointId,
    Method,
    product_delete_succeeded,
    resolve,
)
from tests.unit.integrations.marketplaces.smartstore.test_smartstore_create import Bearer, Provider


def _sender(provider: Provider, *, bearer: object = Bearer()) -> SmartStoreDeleteSender:
    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))
    return SmartStoreDeleteSender(caller, bearer=lambda: bearer)


def test_the_contract_is_the_documented_delete_of_one_origin_product() -> None:
    contract = resolve(EndpointId.SMARTSTORE_PRODUCT_DELETE_V2)
    assert contract.method is Method.DELETE
    assert contract.path == "/v2/products/origin-products/{originProductNo}"
    assert contract.mutating and contract.requires_bearer
    assert contract.required_groups == frozenset({"상품"})
    assert contract.content_type is None
    assert contract.retained_response_fields == frozenset()
    assert contract.safe_query_keys == frozenset()


@pytest.mark.parametrize(
    ("status", "body", "proven"),
    [
        (200, {"code": "SUCCESS", "message": "ok"}, True),
        (200, {}, True),
        (200, [], False),
        (200, None, False),
        (204, {}, False),
        (201, {}, False),
    ],
)
def test_only_the_documented_success_proves_a_deletion(
    status: int, body: object, proven: bool
) -> None:
    assert product_delete_succeeded(status, body) is proven


def test_a_documented_success_is_applied_and_sends_exactly_the_delete() -> None:
    provider = Provider(httpx.Response(200, json={"code": "SUCCESS"}))
    handoff = _sender(provider).send(marketplace_product_id="9900112233")
    assert handoff.remote_outcome is RemoteOutcome.APPLIED_PROVEN
    assert handoff.response_status == 200
    (request,) = provider.requests
    assert request.method == "DELETE"
    assert request.url.path == "/external/v2/products/origin-products/9900112233"
    assert request.url.query == b""
    assert request.content == b""
    assert request.headers["Authorization"].startswith("Bearer ")


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404, json={"code": "NOT_FOUND"}),
        httpx.Response(400, json={"code": "BAD_REQUEST"}),
        httpx.Response(500, json={"code": "INTERNAL_SERVER_ERROR"}),
        httpx.Response(308, headers={"Location": "https://example.invalid/"}),
        httpx.Response(200, content=b"not json"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_anything_after_the_handoff_is_unknown(answer: httpx.Response | Exception) -> None:
    provider = Provider(answer)
    handoff = _sender(provider).send(marketplace_product_id="9900112233")
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN
    # Exactly one request: nothing is ever retried or resent here.
    assert len(provider.requests) == 1


def test_no_session_is_refused_locally_before_any_transport() -> None:
    provider = Provider(httpx.Response(200, json={}))
    handoff = _sender(provider, bearer=None).send(marketplace_product_id="9900112233")
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert handoff.error_code == "SMARTSTORE_SESSION_UNAVAILABLE"
    assert provider.requests == []


def test_a_product_number_outside_the_safe_shape_never_reaches_the_wire() -> None:
    provider = Provider(httpx.Response(200, json={}))
    handoff = _sender(provider).send(marketplace_product_id="../v1/oauth2/token")
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert provider.requests == []
