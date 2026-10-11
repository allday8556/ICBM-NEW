"""The Returns & Exchanges family is exact, fail-closed, and fake-only."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Credentials,
    Market,
    Method,
)
from integrations.marketplaces.coupang.claims import (
    ClaimEndpoint,
    CoupangClaimClient,
    CoupangClaimContractError,
    ExchangeId,
    OrderId,
    ReceiptId,
    ShipmentBoxId,
)
from integrations.marketplaces.coupang.fake_transport import FakeResponse, FakeTransport


@dataclass(frozen=True)
class Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _response(*, next_token: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {"code": 200, "message": "OK", "data": []}
    if next_token is not None:
        value["nextToken"] = next_token
    return value


def _client(count: int, *, market: Market = Market.KR) -> tuple[CoupangClaimClient, FakeTransport]:
    transport = FakeTransport(tuple(FakeResponse(200, _response()) for _ in range(count)))
    context = AuthContext(Credentials("access", "secret", "A0001", 1), market)
    return CoupangClaimClient(Authenticator(Clock()), context, transport), transport


def test_family_closes_all_eleven_official_runtime_endpoints() -> None:
    assert len(ClaimEndpoint) == 11


def test_return_identity_and_state_mutations_preserve_exact_paths_and_documents() -> None:
    client, transport = _client(4)

    approval = client.approve_return(ReceiptId(363585), cancel_count=2)
    read = client.read_return(ReceiptId(363585))
    received = client.confirm_return_receipt(ReceiptId(363585))
    pickup = client.upload_pickup_waybill(
        "RETURN",
        ReceiptId(363585),
        delivery_company_code="KDEXP",
        invoice_number="201805291435",
        reg_number="1234568",
    )

    assert approval.endpoint is ClaimEndpoint.APPROVE_RETURN
    assert read.endpoint is ClaimEndpoint.READ_RETURN
    assert received.endpoint is ClaimEndpoint.CONFIRM_RETURN_RECEIPT
    assert pickup.endpoint is ClaimEndpoint.UPLOAD_PICKUP_WAYBILL
    assert [request.target.method for request in transport.requests] == [
        Method.PATCH,
        Method.GET,
        Method.PATCH,
        Method.POST,
    ]
    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/returnRequests/363585/approval",
        "/v2/providers/openapi/apis/api/v6/vendors/A0001/returnRequests/363585",
        (
            "/v2/providers/openapi/apis/api/v4/vendors/A0001/returnRequests/363585"
            "/receiveConfirmation"
        ),
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/return-exchange-invoices/manual",
    ]
    assert json.loads(transport.requests[0].body) == {
        "cancelCount": 2,
        "receiptId": 363585,
        "vendorId": "A0001",
    }
    assert json.loads(transport.requests[2].body) == {
        "receiptId": 363585,
        "vendorId": "A0001",
    }
    assert json.loads(transport.requests[3].body) == {
        "deliveryCompanyCode": "KDEXP",
        "invoiceNumber": "201805291435",
        "receiptId": 363585,
        "regNumber": "1234568",
        "returnExchangeDeliveryType": "RETURN",
    }


def test_return_withdrawal_queries_keep_official_query_and_batch_contracts() -> None:
    client, transport = _client(2)

    ranged = client.list_return_withdrawals(
        "2026-01-01", "2026-01-07", page_index=2, size_per_page=25
    )
    batched = client.read_return_withdrawals((ReceiptId(10), ReceiptId(11)))

    assert ranged.endpoint is ClaimEndpoint.LIST_RETURN_WITHDRAWALS
    assert batched.endpoint is ClaimEndpoint.READ_RETURN_WITHDRAWALS
    assert transport.requests[0].target.query == (
        "sizePerPage=25&pageIndex=2&dateFrom=2026-01-01&dateTo=2026-01-07"
    )
    assert json.loads(transport.requests[1].body) == {"cancelIds": [10, 11]}


def test_return_list_timeframe_and_paged_modes_are_not_interchangeable() -> None:
    client, transport = _client(2)

    timeframe = client.list_returns("2026-01-01T00:00", "2026-01-02T00:00", status="UC")
    paged = client.list_returns(
        "2026-01-01",
        "2026-01-31",
        search_type="day",
        status=None,
        order_id=OrderId(900),
        next_token="next 1",
        max_per_page=25,
    )

    assert timeframe.endpoint is ClaimEndpoint.LIST_RETURNS
    assert paged.endpoint is ClaimEndpoint.LIST_RETURNS
    assert transport.requests[0].target.query == (
        "searchType=timeFrame&createdAtFrom=2026-01-01T00:00&createdAtTo=2026-01-02T00:00"
        "&status=UC&cancelType=RETURN"
    )
    assert transport.requests[1].target.query == (
        "createdAtFrom=2026-01-01&createdAtTo=2026-01-31&cancelType=RETURN"
        "&nextToken=next+1&maxPerPage=25&orderId=900"
    )


def test_cancelled_order_return_query_excludes_status_and_order_identity() -> None:
    client, transport = _client(1, market=Market.TW)

    client.list_returns(
        "2026-01-01", "2026-01-02", search_type="day", status=None, cancel_type="CANCEL"
    )

    assert transport.requests[0].target.query == (
        "createdAtFrom=2026-01-01&createdAtTo=2026-01-02&cancelType=CANCEL"
    )


def test_exchange_family_preserves_exact_paths_queries_and_bodies() -> None:
    client, transport = _client(4)

    confirmed = client.confirm_exchange_receipt(ExchangeId(40362))
    listed = client.list_exchanges(
        "2026-01-01T00:00:00",
        "2026-01-07T23:59:59",
        status="PROGRESS",
        order_id=OrderId(800),
        next_token="next 2",
        max_per_page=15,
    )
    rejected = client.reject_exchange(ExchangeId(40362), reason="SOLDOUT")
    uploaded = client.upload_exchange_waybill(
        ExchangeId(40362),
        shipment_box_id=ShipmentBoxId(123456789012345678),
        goods_delivery_code="CJGLS",
        invoice_number="680405450931",
    )

    assert [result.endpoint for result in (confirmed, listed, rejected, uploaded)] == [
        ClaimEndpoint.CONFIRM_EXCHANGE_RECEIPT,
        ClaimEndpoint.LIST_EXCHANGES,
        ClaimEndpoint.REJECT_EXCHANGE,
        ClaimEndpoint.UPLOAD_EXCHANGE_WAYBILL,
    ]
    assert [request.target.path for request in transport.requests] == [
        (
            "/v2/providers/openapi/apis/api/v4/vendors/A0001/exchangeRequests/40362"
            "/receiveConfirmation"
        ),
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/exchangeRequests",
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/exchangeRequests/40362/rejection",
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/exchangeRequests/40362/invoices",
    ]
    assert transport.requests[1].target.query == (
        "createdAtFrom=2026-01-01T00:00:00&createdAtTo=2026-01-07T23:59:59"
        "&status=PROGRESS&orderId=800&nextToken=next+2&maxPerPage=15"
    )
    assert json.loads(transport.requests[0].body) == {
        "exchangeId": 40362,
        "vendorId": "A0001",
    }
    assert json.loads(transport.requests[2].body) == {
        "exchangeId": "40362",
        "exchangeRejectCode": "SOLDOUT",
        "vendorId": "A0001",
    }
    assert json.loads(transport.requests[3].body) == [
        {
            "exchangeId": "40362",
            "goodsDeliveryCode": "CJGLS",
            "invoiceNumber": "680405450931",
            "shipmentBoxId": "123456789012345678",
            "vendorId": "A0001",
        }
    ]


def test_request_validation_fails_before_the_fake_transport() -> None:
    client, transport = _client(1)

    with pytest.raises(CoupangClaimContractError, match="include at most 7 days"):
        client.list_return_withdrawals("2026-01-01", "2026-01-08")
    with pytest.raises(CoupangClaimContractError, match=r"1\.\.50"):
        client.read_return_withdrawals(tuple(ReceiptId(value) for value in range(1, 52)))
    with pytest.raises(CoupangClaimContractError, match="unique"):
        client.read_return_withdrawals((ReceiptId(1), ReceiptId(1)))
    with pytest.raises(CoupangClaimContractError, match="exclude status"):
        client.list_returns("2026-01-01", "2026-01-02", search_type="day", cancel_type="CANCEL")
    with pytest.raises(CoupangClaimContractError, match="orderId is required"):
        client.list_returns("2026-01-01", "2026-01-02", search_type="day", status=None)
    with pytest.raises(CoupangClaimContractError, match="official exchange status"):
        client.list_exchanges("2026-01-01T00:00:00", "2026-01-02T00:00:00", status="UNKNOWN")
    with pytest.raises(CoupangClaimContractError, match="SOLDOUT or WITHDRAW"):
        client.reject_exchange(ExchangeId(1), reason="UNKNOWN")
    with pytest.raises(CoupangClaimContractError, match="shipmentBoxId"):
        client.upload_exchange_waybill(
            ExchangeId(1),
            shipment_box_id=ShipmentBoxId(0),
            goods_delivery_code="CJGLS",
            invoice_number="123",
        )
    assert transport.requests == []


def test_kr_only_endpoints_are_blocked_for_tw_before_transport() -> None:
    client, transport = _client(1, market=Market.TW)

    with pytest.raises(CoupangClaimContractError, match="KR only"):
        client.list_return_withdrawals("2026-01-01", "2026-01-02")
    with pytest.raises(CoupangClaimContractError, match="KR only"):
        client.upload_pickup_waybill(
            "RETURN", ReceiptId(1), delivery_company_code="KDEXP", invoice_number="123"
        )
    with pytest.raises(CoupangClaimContractError, match="KR only"):
        client.upload_exchange_waybill(
            ExchangeId(1),
            shipment_box_id=ShipmentBoxId(1),
            goods_delivery_code="CJGLS",
            invoice_number="123",
        )
    assert transport.requests == []


def test_non_success_or_non_json_responses_fail_closed() -> None:
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.KR)
    failed = CoupangClaimClient(
        Authenticator(Clock()),
        context,
        FakeTransport((FakeResponse(200, {"code": 400, "message": "bad"}),)),
    )
    invalid = CoupangClaimClient(
        Authenticator(Clock()), context, FakeTransport((FakeResponse(200, ["not", "object"]),))
    )

    with pytest.raises(CoupangClaimContractError, match="not successful"):
        failed.read_return(ReceiptId(1))
    with pytest.raises(CoupangClaimContractError, match="Coupang HTTP"):
        invalid.read_return(ReceiptId(1))
