"""The adopted SmartStore order dispatch (M6.5-C; ADR-0025 §5.1) against a fake transport.

One product order per call with delivery method DELIVERY, a documented carrier code, a tracking
number and a KST dispatch time; anything else is refused before any transport. The documented 200
answer is handed on with how it listed this order — SUCCESS, FAIL (with its documented code) or
NONE — and nothing of it is retained. NOT_APPLIED_PROVEN only for the transmission-precluded
whitelist; everything else UNKNOWN. Nothing is resent.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.stages.connect.marketplace.capability import RemoteOutcome
from integrations.marketplaces.smartstore.caller import (
    OrderDispatchRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.dispatch import SmartStoreDispatchSender
from integrations.marketplaces.smartstore.registry import (
    EndpointId,
    Method,
    order_dispatch_succeeded,
    resolve,
)
from tests.unit.integrations.marketplaces.smartstore.test_smartstore_create import Bearer, Provider

PO = "2026100800001"
WHEN = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)


def _sender(provider: Provider, *, bearer: object = Bearer()) -> SmartStoreDispatchSender:
    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))
    return SmartStoreDispatchSender(caller, bearer=lambda: bearer)


def _send(provider: Provider, **overrides: object) -> object:
    values: dict[str, object] = {
        "product_order_id": PO,
        "carrier_code": "CJGLS",
        "tracking_number": "6000-1111-2222",
        "dispatch_date": WHEN,
    }
    values.update(overrides)
    return _sender(provider).send(**values)  # type: ignore[arg-type]


def _answer(success: list[str] | None = None, fail: list[dict[str, str]] | None = None) -> object:
    data: dict[str, object] = {}
    if success is not None:
        data["successProductOrderIds"] = success
    if fail is not None:
        data["failProductOrderInfos"] = fail
    return httpx.Response(200, json={"timestamp": "t", "traceId": "trace-1", "data": data})


def test_the_contract_is_the_documented_dispatch_of_one_product_order() -> None:
    contract = resolve(EndpointId.SMARTSTORE_ORDER_DISPATCH)
    assert contract.method is Method.POST
    assert contract.path == "/v1/pay-order/seller/product-orders/dispatch"
    assert contract.mutating and contract.requires_bearer
    assert contract.required_groups == frozenset({"주문 판매자"})
    assert contract.content_type == "application/json"
    assert contract.retained_response_fields == frozenset()
    assert contract.safe_query_keys == frozenset()


def test_the_documented_body_names_one_order_and_nothing_else() -> None:
    provider = Provider(_answer(success=[PO]))
    handoff = _send(provider)
    (request,) = provider.requests
    assert request.method == "POST"
    assert request.url.path == "/external/v1/pay-order/seller/product-orders/dispatch"
    assert request.url.query == b""
    assert request.headers["Authorization"].startswith("Bearer ")
    assert json.loads(request.content) == {
        "dispatchProductOrders": [
            {
                "productOrderId": PO,
                "deliveryMethod": "DELIVERY",
                "deliveryCompanyCode": "CJGLS",
                "trackingNumber": "6000-1111-2222",
                "dispatchDate": "2026-10-08T12:00:00.000+09:00",
            }
        ]
    }
    assert handoff.remote_outcome is RemoteOutcome.APPLIED_PROVEN  # type: ignore[attr-defined]
    assert (handoff.listed, handoff.fail_code) == ("SUCCESS", None)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("answer", "listed", "code"),
    [
        (_answer(success=[PO]), "SUCCESS", None),
        (_answer(success=[], fail=[{"productOrderId": PO, "code": "104122"}]), "FAIL", "104122"),
        (
            _answer(fail=[{"productOrderId": PO, "code": "105306", "message": "같음"}]),
            "FAIL",
            "105306",
        ),
        # Another order listed, or this one in both lists: never a proven dispatch.
        (_answer(success=["other"]), "NONE", None),
        (_answer(success=[PO], fail=[{"productOrderId": PO, "code": "9999"}]), "NONE", None),
        (_answer(), "NONE", None),
    ],
)
def test_the_answer_is_read_only_for_this_order(
    answer: object, listed: str, code: str | None
) -> None:
    handoff = _send(Provider(answer))  # type: ignore[arg-type]
    assert handoff.remote_outcome is RemoteOutcome.APPLIED_PROVEN  # type: ignore[attr-defined]
    assert (handoff.listed, handoff.fail_code) == (listed, code)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("status", "body", "documented"),
    [
        (200, {"data": {"successProductOrderIds": ["a"], "failProductOrderInfos": []}}, True),
        (200, {"data": {}}, True),
        (200, {"data": {"successProductOrderIds": "a"}}, False),
        (200, {"data": {"successProductOrderIds": [1]}}, False),
        (200, {"data": {"failProductOrderInfos": [{"code": "x"}]}}, False),
        (200, {"data": []}, False),
        (200, {}, False),
        (200, None, False),
        (201, {"data": {}}, False),
    ],
)
def test_only_the_documented_answer_passes_the_predicate(
    status: int, body: object, documented: bool
) -> None:
    assert order_dispatch_succeeded(status, body) is documented


@pytest.mark.parametrize(
    "overrides",
    [
        {"carrier_code": "NOT_A_CARRIER"},
        {"carrier_code": "GS더프레시"},
        {"tracking_number": ""},
        {"tracking_number": "12 34"},
        {"tracking_number": "1" * 51},
        {"product_order_id": "po/1"},
    ],
)
def test_an_undocumented_request_is_refused_before_any_transport(
    overrides: dict[str, object],
) -> None:
    provider = Provider(_answer(success=[PO]))
    handoff = _send(provider, **overrides)
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN  # type: ignore[attr-defined]
    assert handoff.error_code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"  # type: ignore[attr-defined]
    assert provider.requests == []


def test_a_dispatch_date_outside_kst_is_refused_by_the_caller() -> None:
    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(Provider(_answer())))
    with pytest.raises(SmartStoreCallError) as refused:
        caller.call(
            EndpointId.SMARTSTORE_ORDER_DISPATCH,
            OrderDispatchRequest("t" * 20, 1, 1, PO, "CJGLS", "1", "2026-10-08T03:00:00Z"),
        )
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"


def test_without_a_session_nothing_is_sent() -> None:
    provider = Provider(_answer(success=[PO]))
    handoff = _sender(provider, bearer=None).send(
        product_order_id=PO, carrier_code="CJGLS", tracking_number="1", dispatch_date=WHEN
    )
    assert handoff.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert handoff.error_code == "SMARTSTORE_SESSION_UNAVAILABLE"
    assert provider.requests == []


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(400, json={"code": "104122", "message": "x"}),
        httpx.Response(500, json={"code": "9999"}),
        httpx.Response(429, json={"code": "GW.RATE_LIMIT"}),
        httpx.Response(308, headers={"Location": "https://example.invalid/"}),
        httpx.Response(200, content=b"not json"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_anything_else_after_the_handoff_is_unknown(answer: httpx.Response | Exception) -> None:
    handoff = _send(Provider(answer))
    assert handoff.remote_outcome is RemoteOutcome.UNKNOWN  # type: ignore[attr-defined]
    assert handoff.listed is None  # type: ignore[attr-defined]
