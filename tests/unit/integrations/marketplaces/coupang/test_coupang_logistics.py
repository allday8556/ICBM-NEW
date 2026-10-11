"""Coupang Logistics contracts are exact, fail-closed, and fake-only."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from integrations.marketplaces.coupang.auth import AuthContext, Authenticator, Credentials, Market
from integrations.marketplaces.coupang.fake_transport import FakeResponse, FakeTransport
from integrations.marketplaces.coupang.logistics import (
    CoupangLogisticsClient,
    CoupangLogisticsContractError,
    LogisticsEndpoint,
    OutboundShippingPlaceCode,
    ReturnCenterCode,
)


@dataclass(frozen=True)
class Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _client(count: int) -> tuple[CoupangLogisticsClient, FakeTransport]:
    transport = FakeTransport(
        tuple(FakeResponse(200, {"code": 200, "message": "OK", "data": {}}) for _ in range(count))
    )
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.KR)
    return CoupangLogisticsClient(Authenticator(Clock()), context, transport), transport


def _address() -> dict[str, object]:
    return {
        "addressType": "JIBUN",
        "countryCode": "KR",
        "companyContactNumber": "02-123-4567",
        "phoneNumber2": "010-1234-5678",
        "returnZipCode": "12345",
        "returnAddress": "Seoul",
        "returnAddressDetail": "101",
    }


def _goodsflow() -> dict[str, object]:
    return {
        "deliverCode": "CJGLS",
        "deliverName": "CJ Logistics",
        "contractNumber": "contract-1",
        "vendorCreditFee05kg": 2500,
        "consumerCashFee10kg": 2500,
        "consumerCashFee20kg": 2500,
        "returnFee05kg": 2500,
        "returnFee10kg": 2500,
        "returnFee20kg": 2500,
    }


def test_all_seven_executable_logistics_endpoints_keep_exact_paths_queries_and_bodies() -> None:
    client, transport = _client(7)

    created_return = client.create_return_location(
        user_id="wing-user",
        shipping_place_name="Returns",
        goodsflow_info=_goodsflow(),
        place_addresses=(_address(),),
    )
    created_shipping = client.create_shipping_location(
        user_id="wing-user", shipping_place_name="Outbound", place_addresses=(_address(),)
    )
    listed_return = client.list_return_locations(page_num=2, page_size=50)
    listed_shipping = client.query_shipping_locations(page_num=1, page_size=50)
    read_return = client.read_return_locations(
        (ReturnCenterCode("1000554021"), ReturnCenterCode("1000554704"))
    )
    updated_return = client.update_return_location(
        ReturnCenterCode("1000554021"),
        user_id="wing-user",
        place_addresses=(_address(),),
        usable=True,
    )
    updated_shipping = client.update_shipping_location(
        OutboundShippingPlaceCode(123456),
        user_id="wing-user",
        place_addresses=(_address(),),
        shipping_place_name="Outbound 2",
        usable=True,
        global_=False,
    )

    assert [
        result.endpoint
        for result in (
            created_return,
            created_shipping,
            listed_return,
            listed_shipping,
            read_return,
            updated_return,
            updated_shipping,
        )
    ] == list(LogisticsEndpoint)
    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/openapi/apis/api/v5/vendors/A0001/returnShippingCenters",
        "/v2/providers/openapi/apis/api/v5/vendors/A0001/outboundShippingCenters",
        "/v2/providers/openapi/apis/api/v5/vendors/A0001/returnShippingCenters",
        "/v2/providers/marketplace_openapi/apis/api/v2/vendor/shipping-place/outbound",
        "/v2/providers/openapi/apis/api/v3/return/shipping-places/center-code",
        "/v2/providers/openapi/apis/api/v5/vendors/A0001/returnShippingCenters/1000554021",
        "/v2/providers/openapi/apis/api/v5/vendors/A0001/outboundShippingCenters/123456",
    ]
    assert transport.requests[2].target.query == "pageNum=2&pageSize=50"
    assert transport.requests[3].target.query == "pageNum=1&pageSize=50"
    assert transport.requests[4].target.query == ("returnCenterCodes=1000554021%2C1000554704")
    assert json.loads(transport.requests[0].body)["goodsflowInfoOpenApiDto"] == _goodsflow()
    assert json.loads(transport.requests[1].body)["shippingPlaceName"] == "Outbound"
    assert json.loads(transport.requests[5].body)["returnCenterCode"] == "1000554021"
    assert json.loads(transport.requests[6].body)["outboundShippingPlaceCode"] == 123456


def test_filtered_shipping_query_is_distinct_from_paged_query() -> None:
    client, transport = _client(1)

    client.query_shipping_locations(
        page_num=None,
        place_codes=(OutboundShippingPlaceCode(10), OutboundShippingPlaceCode(11)),
        place_names=("Seoul",),
    )

    assert transport.requests[0].target.query == ("placeCodes=10%2C11&placeNames=Seoul&pageSize=10")


def test_invalid_inputs_fail_before_fake_transport() -> None:
    client, transport = _client(1)

    with pytest.raises(CoupangLogisticsContractError, match="pageSize"):
        client.list_return_locations(page_size=51)
    with pytest.raises(CoupangLogisticsContractError, match="exclude pageNum"):
        client.query_shipping_locations(place_names=("Seoul",))
    with pytest.raises(CoupangLogisticsContractError, match=r"1\.\.100"):
        client.read_return_locations(())
    bad_address = _address() | {"returnZipCode": "abc"}
    with pytest.raises(CoupangLogisticsContractError, match=r"5\.\.6 digits"):
        client.create_shipping_location(
            user_id="wing-user", shipping_place_name="Outbound", place_addresses=(bad_address,)
        )
    assert transport.requests == []


def test_non_success_response_fails_closed() -> None:
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.TW)
    client = CoupangLogisticsClient(
        Authenticator(Clock()),
        context,
        FakeTransport((FakeResponse(200, {"code": 400, "message": "bad"}),)),
    )

    with pytest.raises(CoupangLogisticsContractError, match="not successful"):
        client.list_return_locations()
