"""The Shipments & Orders family is exact, fail-closed, and fake-only."""

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
from integrations.marketplaces.coupang.fake_transport import FakeResponse, FakeTransport
from integrations.marketplaces.coupang.orders import (
    POSTAL_CODES_RUNTIME_ADOPTED,
    POSTAL_CODES_SOURCE_URL,
    CoupangOrderClient,
    CoupangOrderContractError,
    OrderEndpoint,
    OrderId,
    ReceiptId,
    ShipmentBoxId,
    VendorItemId,
    Waybill,
)


@dataclass(frozen=True)
class Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _response(*, next_token: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {"code": 200, "message": "OK", "data": []}
    if next_token is not None:
        value["nextToken"] = next_token
    return value


def _client(count: int, *, market: Market = Market.KR) -> tuple[CoupangOrderClient, FakeTransport]:
    responses = tuple(FakeResponse(200, _response()) for _ in range(count))
    transport = FakeTransport(responses)
    context = AuthContext(Credentials("access", "secret", "A0001", 1), market)
    return CoupangOrderClient(Authenticator(Clock()), context, transport), transport


def _waybill(*, split: bool = False, invoice: str = "123456", date: str = "") -> Waybill:
    return Waybill(
        ShipmentBoxId(100),
        OrderId(200),
        VendorItemId(300),
        "KDEXP",
        invoice,
        split,
        False,
        date,
    )


def test_family_closes_twelve_runtime_endpoints_and_one_reference_only_contract() -> None:
    assert len(OrderEndpoint) == 12
    assert POSTAL_CODES_RUNTIME_ADOPTED is False
    assert POSTAL_CODES_SOURCE_URL.endswith("south-korean-cities-and-postal-codes")


def test_reads_use_exact_v5_paths_and_distinct_endpoint_identities() -> None:
    client, transport = _client(5)

    minute = client.list_by_minute(
        "2026-01-01T00:00+09:00", "2026-01-01T23:59+09:00", status="DEPARTURE"
    )
    day = client.list_by_day(
        "2026-01-01+09:00",
        "2026-01-31+09:00",
        status="INSTRUCT",
        next_token="next 1",
        max_per_page=25,
    )
    client.delivery_history(ShipmentBoxId(100))
    client.by_shipment_box(ShipmentBoxId(100))
    client.by_order(OrderId(200))

    assert minute.endpoint is OrderEndpoint.LIST_BY_MINUTE
    assert day.endpoint is OrderEndpoint.LIST_BY_DAY
    assert [request.target.method for request in transport.requests] == [Method.GET] * 5
    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/openapi/apis/api/v5/vendors/A0001/ordersheets",
        "/v2/providers/openapi/apis/api/v5/vendors/A0001/ordersheets",
        "/v2/providers/openapi/apis/api/v5/vendors/A0001/ordersheets/100/history",
        "/v2/providers/openapi/apis/api/v5/vendors/A0001/ordersheets/100",
        "/v2/providers/openapi/apis/api/v5/vendors/A0001/200/ordersheets",
    ]
    assert transport.requests[0].target.query == (
        "createdAtFrom=2026-01-01T00:00%2B09:00&createdAtTo=2026-01-01T23:59%2B09:00"
        "&searchType=timeFrame&status=DEPARTURE"
    )
    assert transport.requests[1].target.query == (
        "createdAtFrom=2026-01-01%2B09:00&createdAtTo=2026-01-31%2B09:00"
        "&nextToken=next+1&maxPerPage=25&status=INSTRUCT"
    )


def test_cancellation_and_acknowledgement_preserve_path_body_identity() -> None:
    client, transport = _client(2)

    client.cancel_order(
        OrderId(200),
        vendor_item_ids=(VendorItemId(300), VendorItemId(301)),
        receipt_counts=(1, 2),
        middle_cancel_code="CCTTER",
        user_id="wing-user",
    )
    client.acknowledge((ShipmentBoxId(100), ShipmentBoxId(101)))

    assert transport.requests[0].target.path.endswith("/orders/200/cancel")
    assert transport.requests[0].target.method is Method.POST
    assert json.loads(transport.requests[0].body) == {
        "bigCancelCode": "CANERR",
        "middleCancelCode": "CCTTER",
        "orderId": 200,
        "receiptCounts": [1, 2],
        "userId": "wing-user",
        "vendorId": "A0001",
        "vendorItemIds": [300, 301],
    }
    assert transport.requests[1].target.path.endswith("/ordersheets/acknowledgement")
    assert transport.requests[1].target.method is Method.PATCH
    assert json.loads(transport.requests[1].body) == {
        "shipmentBoxIds": [100, 101],
        "vendorId": "A0001",
    }


def test_kr_only_mutations_use_exact_v4_paths_and_documents() -> None:
    client, transport = _client(5)

    client.complete_long_term_undelivery(ShipmentBoxId(100), "123456")
    client.already_released(ReceiptId(400), delivery_company_code="KDEXP", invoice_number="123456")
    client.release_stop(ReceiptId(400), cancel_count=2)
    client.update_waybills((_waybill(),))
    client.upload_waybills((_waybill(),))

    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/completeLongTermUndelivery",
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/returnRequests/400/completedShipment",
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/returnRequests/400/stoppedShipment",
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/orders/updateInvoices",
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/orders/invoices",
    ]
    assert [request.target.method for request in transport.requests] == [
        Method.POST,
        Method.PATCH,
        Method.PATCH,
        Method.POST,
        Method.POST,
    ]
    update = json.loads(transport.requests[3].body)["orderSheetInvoiceApplyDtos"][0]
    upload = json.loads(transport.requests[4].body)["orderSheetInvoiceApplyDtos"][0]
    assert update["invoiceNumber"] == 123456
    assert update["splitShipping"] == "False"
    assert upload["invoiceNumber"] == "123456"
    assert upload["splitShipping"] is False


def test_split_waybill_keeps_the_documented_invoice_or_date_exclusivity() -> None:
    dated = _waybill(split=True, invoice="", date="2026-01-04")
    numbered = _waybill(split=True, invoice="123456", date="")

    assert dated.upload_document()["estimatedShippingDate"] == "2026-01-04"
    assert numbered.upload_document()["invoiceNumber"] == "123456"
    with pytest.raises(CoupangOrderContractError, match="exactly one"):
        _waybill(split=True, invoice="123456", date="2026-01-04")
    with pytest.raises(CoupangOrderContractError, match="exactly one"):
        _waybill(split=True, invoice="", date="")
    with pytest.raises(CoupangOrderContractError, match="invoice number"):
        dated.update_document()


def test_request_validation_fails_before_the_fake_transport() -> None:
    client, transport = _client(1)

    with pytest.raises(CoupangOrderContractError, match="paired"):
        client.cancel_order(
            OrderId(200),
            vendor_item_ids=(VendorItemId(300),),
            receipt_counts=(1, 2),
            middle_cancel_code="CCTTER",
            user_id="wing-user",
        )
    with pytest.raises(CoupangOrderContractError, match=r"1\.\.50"):
        client.acknowledge(tuple(ShipmentBoxId(value) for value in range(1, 52)))
    with pytest.raises(CoupangOrderContractError, match="at most 24 hours"):
        client.list_by_minute("2026-01-01T00:00+09:00", "2026-01-02T00:01+09:00", status="ACCEPT")
    with pytest.raises(CoupangOrderContractError, match="buyer-market offset"):
        client.list_by_day("2026-01-01+08:00", "2026-01-02+08:00", status="ACCEPT")
    with pytest.raises(CoupangOrderContractError, match="official PO status"):
        client.list_by_day("2026-01-01+09:00", "2026-01-02+09:00", status="UNKNOWN")
    assert transport.requests == []


def test_kr_only_endpoints_are_blocked_for_tw_before_transport() -> None:
    client, transport = _client(1, market=Market.TW)

    with pytest.raises(CoupangOrderContractError, match="KR only"):
        client.upload_waybills((_waybill(),))
    with pytest.raises(CoupangOrderContractError, match="KR only"):
        client.release_stop(ReceiptId(400), cancel_count=1)
    assert transport.requests == []


def test_tw_reads_require_the_official_eight_hour_offset() -> None:
    client, transport = _client(1, market=Market.TW)

    client.list_by_minute("2026-01-01T00:00+08:00", "2026-01-01T23:59+08:00", status="ACCEPT")

    assert "%2B08:00" in transport.requests[0].target.query


def test_non_success_or_non_json_responses_fail_closed() -> None:
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.KR)
    failed = CoupangOrderClient(
        Authenticator(Clock()),
        context,
        FakeTransport((FakeResponse(200, {"code": 400, "message": "bad"}),)),
    )
    invalid = CoupangOrderClient(
        Authenticator(Clock()), context, FakeTransport((FakeResponse(200, ["not", "object"]),))
    )

    with pytest.raises(CoupangOrderContractError, match="not successful"):
        failed.by_order(OrderId(200))
    with pytest.raises(CoupangOrderContractError, match="Coupang HTTP"):
        invalid.by_order(OrderId(200))
