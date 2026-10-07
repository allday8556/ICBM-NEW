"""The two adopted SmartStore order reads over the real caller (M6-C; ADR-0023 §5-§7).

Read-only; every buyer value is synthetic (rule 07 §7.3). Proven here: the documented requests
(KST window, at most 300, the provider's own continuation) and nothing else; the success
predicates; the order allow-list — the orderer, payment, claims and their addresses and the
seller's taking address never cross the caller, and the recipient survives only from the
product order's own shipping address; and the adapter's typed reading of what survived.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.platform.core.errors import PolicyBlockedError
from integrations.marketplaces.smartstore.caller import (
    OrderChangesRequest,
    OrderDetailsRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.orders import SmartStoreOrderSource, kst
from integrations.marketplaces.smartstore.registry import (
    EndpointId,
    order_changes_succeeded,
    order_details_succeeded,
    resolve,
)

FROM = "2026-10-07T09:00:00.000+09:00"
TO = "2026-10-08T09:00:00.000+09:00"

CHANGES = {
    "timestamp": "2026-10-07T10:00:00.000+09:00",
    "traceId": "trace-1",
    "data": {
        "count": 1,
        "lastChangeStatuses": [
            {
                "orderId": "o-1",
                "productOrderId": "po-1",
                "lastChangedType": "PAYED",
                "paymentDate": "2026-10-07T09:30:00.000+09:00",
                "lastChangedDate": "2026-10-07T09:31:00.000+09:00",
                "productOrderStatus": "PAYED",
                "receiverAddressChanged": False,
            }
        ],
        "more": {"moreFrom": "2026-10-07T09:31:00.000+09:00", "moreSequence": "0001"},
    },
}

DETAIL = {
    "order": {
        "orderId": "o-1",
        "orderDate": "2026-10-07T09:29:00.000+09:00",
        "paymentDate": "2026-10-07T09:30:00.000+09:00",
        "ordererId": "buyer-id",
        "ordererName": "주문자이름",
        "ordererTel": "010-9999-9999",
        "paymentMeans": "CARD",
        "generalPaymentAmount": 25000,
    },
    "productOrder": {
        "productOrderId": "po-1",
        "productOrderStatus": "PAYED",
        "placeOrderStatus": "NOT_YET",
        "productId": "11111",
        "originalProductId": "22222",
        "sellerProductCode": "code-1",
        "optionManageCode": "opt-1",
        "productName": "테스트 상품",
        "productOption": "색상: 빨강",
        "quantity": 2,
        "unitPrice": 12500,
        "totalPaymentAmount": 25000,
        "expectedDeliveryMethod": "DELIVERY",
        "shippingMemo": "문 앞",
        "saleCommission": 100,
        "shippingAddress": {
            "name": "가나다",
            "tel1": "010-0000-1234",
            "baseAddress": "테스트시 테스트로 1",
            "detailedAddress": "101호",
            "zipCode": "00000",
            "latitude": "37.0",
            "addressType": "DOMESTIC",
        },
        "takingAddress": {"name": "출고지이름", "tel1": "02-000-0000", "baseAddress": "출고지주소"},
        "appliedCoupons": [{"couponPublishNumber": "c-1", "couponDiscountAmount": 1000}],
        "hopeDelivery": {"changer": "누군가"},
    },
    "currentClaim": {
        "return": {"collectAddress": {"name": "반품자", "tel1": "010-7777-7777"}},
    },
    "delivery": {
        "deliveryMethod": "DELIVERY",
        "deliveryStatus": "DELIVERING",
        "deliveryCompany": "CJGLS",
        "trackingNumber": "123",
        "sendDate": "2026-10-07T12:00:00.000+09:00",
    },
}


def _bearer() -> Any:
    return SimpleNamespace(access_token="token", credential_generation=3, session_generation=5)


def _caller(body: object, status: int = 200, seen: list[httpx.Request] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    return SmartStoreEndpointCaller(transport=httpx.MockTransport(handler))


def test_both_reads_are_adopted_read_only() -> None:
    changes = resolve(EndpointId.SMARTSTORE_ORDER_CHANGES)
    details = resolve(EndpointId.SMARTSTORE_ORDER_DETAILS)
    assert (changes.method, changes.path, changes.mutating) == (
        "GET",
        "/v1/pay-order/seller/product-orders/last-changed-statuses",
        False,
    )
    assert (details.method, details.path, details.mutating, details.content_type) == (
        "POST",
        "/v1/pay-order/seller/product-orders/query",
        False,
        "application/json",
    )


def test_the_change_read_sends_the_documented_window_and_continuation() -> None:
    seen: list[httpx.Request] = []
    caller = _caller(CHANGES, seen=seen)
    caller.call(
        EndpointId.SMARTSTORE_ORDER_CHANGES,
        OrderChangesRequest("token", 3, 5, FROM, TO, "0001", 300),
    )
    (request,) = seen
    assert request.method == "GET"
    assert dict(request.url.params) == {
        "lastChangedFrom": FROM,
        "lastChangedTo": TO,
        "moreSequence": "0001",
        "limitCount": "300",
    }
    assert request.headers["authorization"] == "Bearer token"


@pytest.mark.parametrize(
    "request_",
    (
        OrderChangesRequest("token", 3, 5, "2026-10-07 09:00:00", TO),
        OrderChangesRequest("token", 3, 5, FROM, "2026-10-08T00:00:00.000Z"),
        OrderChangesRequest("token", 3, 5, TO, FROM),
        OrderChangesRequest("token", 3, 5, FROM, TO, "has space"),
        OrderChangesRequest("token", 3, 5, FROM, TO, None, 301),
        OrderChangesRequest("token", 3, 5, FROM, TO, None, True),  # type: ignore[arg-type]
    ),
)
def test_an_undocumented_change_request_is_refused_before_any_transport(
    request_: OrderChangesRequest,
) -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(SmartStoreCallError) as refused:
        _caller(CHANGES, seen=seen).call(EndpointId.SMARTSTORE_ORDER_CHANGES, request_)
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"
    assert seen == []


@pytest.mark.parametrize(
    "ids",
    ((), tuple(f"po-{n}" for n in range(301)), ("po-1", "po-1"), ("po/1",), ("",)),
)
def test_an_undocumented_detail_request_is_refused_before_any_transport(
    ids: tuple[str, ...],
) -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(SmartStoreCallError) as refused:
        _caller({"data": []}, seen=seen).call(
            EndpointId.SMARTSTORE_ORDER_DETAILS, OrderDetailsRequest("token", 3, 5, ids)
        )
    assert refused.value.code == "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"
    assert seen == []


def test_the_detail_read_keeps_only_the_order_allow_list() -> None:
    seen: list[httpx.Request] = []
    caller = _caller({"timestamp": "t", "traceId": "x", "data": [DETAIL]}, seen=seen)
    response = caller.call(
        EndpointId.SMARTSTORE_ORDER_DETAILS, OrderDetailsRequest("token", 3, 5, ("po-1",))
    )
    assert seen[0].content == b'{"productOrderIds":["po-1"]}'
    kept = str(response.retained)
    for never in (
        "buyer-id",
        "주문자이름",
        "010-9999-9999",
        "CARD",
        "출고지",
        "02-000-0000",
        "반품자",
        "010-7777-7777",
        "누군가",
        "c-1",
        "37.0",
        "saleCommission",
        "generalPaymentAmount",
        # Not in ADR-0023 §7: names, carrier, tracking and delivery state (GPT audit, PR #250).
        "테스트 상품",
        "색상: 빨강",
        "CJGLS",
        "trackingNumber",
        "DELIVERING",
        "sendDate",
    ):
        assert never not in kept, never
    (entry,) = response.retained["data"]
    assert entry["productOrder"]["shippingAddress"] == {
        "baseAddress": "테스트시 테스트로 1",
        "detailedAddress": "101호",
        "name": "가나다",
        "tel1": "010-0000-1234",
        "zipCode": "00000",
    }
    assert "가나다" not in repr(response)


def test_the_adapter_reads_what_survived_into_typed_values() -> None:
    source = SmartStoreOrderSource(
        _caller({"traceId": "x", "data": [DETAIL]}),
        _bearer,  # type: ignore[arg-type]
    )
    (facts,) = source.details(["po-1"])
    assert (facts.product_order_id, facts.order_id, facts.status) == ("po-1", "o-1", "PAYED")
    assert (facts.original_product_id, facts.channel_product_id) == ("22222", "11111")
    assert (facts.option_manage_code, facts.seller_product_code) == ("opt-1", "code-1")
    assert (facts.quantity, facts.unit_price, facts.total_payment_amount) == (2, 12500, 25000)
    assert facts.delivery_method == "DELIVERY"
    assert facts.paid_at == datetime(2026, 10, 7, 0, 30, tzinfo=UTC)
    assert (facts.shipping.recipient_name, facts.shipping.memo) == ("가나다", "문 앞")
    assert "가나다" not in repr(facts)
    page = SmartStoreOrderSource(_caller(CHANGES), _bearer).changes(  # type: ignore[arg-type]
        since=datetime(2026, 10, 7, 0, 0, tzinfo=UTC), until=datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    )
    (change,) = page.changes
    assert (change.product_order_id, change.change_type, change.status) == (
        "po-1",
        "PAYED",
        "PAYED",
    )
    assert page.more_sequence == "0001" and page.more_from is not None


def test_an_empty_window_is_a_documented_page_and_an_answer_without_data_is_refused() -> None:
    empty = {"traceId": "x", "data": {"count": 0, "lastChangeStatuses": []}}
    page = SmartStoreOrderSource(_caller(empty), _bearer).changes(  # type: ignore[arg-type]
        since=datetime(2026, 10, 7, tzinfo=UTC), until=datetime(2026, 10, 8, tzinfo=UTC)
    )
    assert page.changes == () and page.more_from is None
    # GPT audit (PR #250): an answer that names no window proves nothing was read.
    with pytest.raises(SmartStoreCallError) as refused:
        SmartStoreOrderSource(_caller({"traceId": "x"}), _bearer).changes(  # type: ignore[arg-type]
            since=datetime(2026, 10, 7, tzinfo=UTC), until=datetime(2026, 10, 8, tzinfo=UTC)
        )
    assert refused.value.code == "SMARTSTORE_SUCCESS_PREDICATE_FAILED"


def test_no_committed_session_calls_nothing() -> None:
    seen: list[httpx.Request] = []
    source = SmartStoreOrderSource(_caller(CHANGES, seen=seen), lambda: None)  # type: ignore[arg-type]
    assert source.available() is False
    with pytest.raises(PolicyBlockedError) as refused:
        source.details(["po-1"])
    assert refused.value.code == "SMARTSTORE_SESSION_UNAVAILABLE"
    assert seen == []


@pytest.mark.parametrize(
    "body",
    (
        {"data": {"count": 1}},
        {"data": {"count": "1", "lastChangeStatuses": []}},
        {"data": {"count": 1, "lastChangeStatuses": [{"productOrderId": "po-1"}]}},
        {"data": {"count": 0, "lastChangeStatuses": [], "more": {"moreFrom": "x"}}},
        {"data": []},
    ),
)
def test_an_undocumented_change_answer_fails_its_predicate(body: object) -> None:
    assert order_changes_succeeded(200, body) is False


@pytest.mark.parametrize(
    "body",
    ({"data": {}}, {"data": [{"order": {}}]}, {"data": [{"productOrder": {"productOrderId": 1}}]}),
)
def test_an_undocumented_detail_answer_fails_its_predicate(body: object) -> None:
    assert order_details_succeeded(200, body) is False
    assert order_details_succeeded(200, {"data": []}) is True
    assert order_changes_succeeded(200, {"traceId": "x"}) is False


def test_a_window_is_sent_in_kst() -> None:
    assert kst(datetime(2026, 10, 7, 0, 0, tzinfo=UTC)) == "2026-10-07T09:00:00.000+09:00"
    with pytest.raises(ValueError):
        kst(datetime(2026, 10, 7))


@pytest.mark.parametrize(
    "retained",
    ({}, {"data": {}}, {"data": {"count": "0"}}, {"data": {"count": True}}, {"data": []}),
)
def test_the_reader_itself_refuses_an_answer_that_names_no_window(retained: object) -> None:
    """GPT audit (PR #250): even past the caller, no answer without ``data.count`` is a page."""

    class Answer:
        def call(self, endpoint_id: object, request: object) -> Any:
            return SimpleNamespace(retained=retained, http_status=200)

    source = SmartStoreOrderSource(Answer(), _bearer)  # type: ignore[arg-type]
    with pytest.raises(PolicyBlockedError) as refused:
        source.changes(
            since=datetime(2026, 10, 7, tzinfo=UTC), until=datetime(2026, 10, 8, tzinfo=UTC)
        )
    assert refused.value.code == "SMARTSTORE_ORDER_RESPONSE_INVALID"
