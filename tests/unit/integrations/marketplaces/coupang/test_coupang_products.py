"""The full Coupang Products family remains executable through a fake transport only."""

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Credentials,
    Market,
    Method,
)
from integrations.marketplaces.coupang.fake_transport import FakeResponse, FakeTransport
from integrations.marketplaces.coupang.products import (
    CoupangProductClient,
    CoupangProductContractError,
    DeleteEligibility,
    FrozenDocument,
    ProductEndpoint,
    ProductListQuery,
    SellerProductId,
    VendorItemId,
)


@dataclass(frozen=True)
class Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _success(data: object = None, *, next_token: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {"code": "SUCCESS", "message": "", "data": data}
    if next_token is not None:
        value["nextToken"] = next_token
    return value


def _client(count: int) -> tuple[CoupangProductClient, FakeTransport]:
    transport = FakeTransport(tuple(FakeResponse(200, _success()) for _ in range(count)))
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.KR)
    return CoupangProductClient(Authenticator(Clock()), context, transport), transport


def _create_document(*, seller_product_id: int | None = None) -> FrozenDocument:
    value: dict[str, object] = {
        "displayCategoryCode": 78877,
        "sellerProductName": "테스트 상품",
        "vendorId": "A0001",
        "saleStartedAt": "2026-01-01T00:00:00",
        "saleEndedAt": "2099-01-01T23:59:59",
        "deliveryMethod": "SEQUENCIAL",
        "deliveryCompanyCode": "KDEXP",
        "deliveryChargeType": "FREE",
        "deliveryCharge": 0,
        "freeShipOverAmount": 0,
        "deliveryChargeOnReturn": 2500,
        "remoteAreaDeliverable": "N",
        "unionDeliveryType": "UNION_DELIVERY",
        "returnCenterCode": "100",
        "returnChargeName": "반품지",
        "companyContactNumber": "02-0000-0000",
        "returnZipCode": "00000",
        "returnAddress": "서울",
        "returnAddressDetail": "1층",
        "returnCharge": 2500,
        "outboundShippingPlaceCode": "200",
        "vendorUserId": "wing-user",
        "requested": False,
        "items": [
            {
                "itemName": "테스트 상품 1개",
                "originalPrice": 10000,
                "salePrice": 9000,
                "maximumBuyCount": 10,
                "maximumBuyForPerson": 0,
                "maximumBuyForPersonPeriod": 1,
                "outboundShippingTimeDay": 1,
                "unitCount": 1,
                "adultOnly": "EVERYONE",
                "taxType": "TAX",
                "parallelImported": "NOT_PARALLEL_IMPORTED",
                "overseasPurchased": "NOT_OVERSEAS_PURCHASED",
                "pccNeeded": "false",
                "images": [{"imageOrder": 0, "imageType": "REPRESENTATION", "cdnPath": "x"}],
                "attributes": [{"attributeTypeName": "수량", "attributeValueName": "1개"}],
                "contents": [
                    {
                        "contentsType": "TEXT",
                        "contentDetails": [{"content": "설명", "detailType": "TEXT"}],
                    }
                ],
            }
        ],
    }
    if seller_product_id is not None:
        value["sellerProductId"] = seller_product_id
    return FrozenDocument.from_mapping(value)


def test_endpoint_inventory_is_closed_at_twenty_two_product_operations() -> None:
    assert len(ProductEndpoint) == 22


def test_create_replace_and_partial_update_preserve_product_identity_boundaries() -> None:
    transport = FakeTransport(
        (
            FakeResponse(200, _success(123)),
            FakeResponse(200, _success()),
            FakeResponse(200, _success()),
        )
    )
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.KR)
    client = CoupangProductClient(Authenticator(Clock()), context, transport)

    created, _ = client.create(_create_document())
    client.replace(_create_document(seller_product_id=123))
    client.partial_update(
        SellerProductId(123),
        FrozenDocument.from_mapping({"sellerProductId": 123, "outboundShippingPlaceCode": "201"}),
    )

    assert created == SellerProductId(123)
    assert [request.target.method for request in transport.requests] == [
        Method.POST,
        Method.PUT,
        Method.PUT,
    ]
    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products",
        "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products",
        "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/123/partial",
    ]


def test_product_reads_lists_history_and_reconcile_queries_are_exact() -> None:
    client, transport = _client(9)

    client.read(SellerProductId(123))
    client.read(SellerProductId(123), partial=True)
    client.list(
        ProductListQuery(
            next_token=2,
            max_per_page=100,
            seller_product_id=SellerProductId(123),
            seller_product_name="테스트",
            status="APPROVED",
            manufacture="제조사",
            created_at="2026-01-01",
            violation_types=("ATTR", "MOTA_V2"),
            violation_type_and_or="OR",
        )
    )
    client.list_timeframe("2026-01-01T00:00:00", "2026-01-01T00:10:00")
    client.history(SellerProductId(123), next_token=2, max_per_page=20)
    client.inflow_status()
    client.by_external_sku("sku / 1")
    client.inventory(VendorItemId(456))
    client.request_approval(SellerProductId(123))

    assert transport.requests[0].target.path.endswith("/seller-products/123")
    assert transport.requests[1].target.path.endswith("/seller-products/123/partial")
    assert transport.requests[2].target.query == urlencode(
        [
            ("vendorId", "A0001"),
            ("nextToken", "2"),
            ("maxPerPage", "100"),
            ("sellerProductId", "123"),
            ("sellerProductName", "테스트"),
            ("status", "APPROVED"),
            ("manufacture", "제조사"),
            ("createdAt", "2026-01-01"),
            ("violationTypes", "ATTR"),
            ("violationTypes", "MOTA_V2"),
            ("violationTypeAndOr", "OR"),
        ]
    )
    assert transport.requests[3].target.query == (
        "vendorId=A0001&createdAtFrom=2026-01-01T00%3A00%3A00&createdAtTo=2026-01-01T00%3A10%3A00"
    )
    assert transport.requests[4].target.query == "nextToken=2&maxPerPage=20"
    assert transport.requests[6].target.path.endswith("/external-vendor-sku-codes/sku%20%2F%201")
    assert transport.requests[7].target.path.endswith("/vendor-items/456/inventories")
    assert transport.requests[8].target.path.endswith("/seller-products/123/approvals")


def test_item_price_quantity_status_and_delete_mutations_have_no_body() -> None:
    client, transport = _client(7)

    client.change_price(
        VendorItemId(456),
        49000,
        force=True,
        auto_min_sale_price=40000,
        auto_pricing_active=True,
    )
    client.change_original_price(VendorItemId(456), 50000)
    client.change_quantity(VendorItemId(456), 0)
    client.resume_sales(VendorItemId(456))
    client.stop_sales(VendorItemId(456))
    client.delete(
        SellerProductId(123),
        eligibility=DeleteEligibility(all_items_stopped=True, awaiting_approval=False),
    )
    client.request_approval(SellerProductId(123))

    assert transport.requests[0].target.path.endswith("/vendor-items/456/prices/49000")
    assert transport.requests[0].target.query == (
        "forceSalePriceUpdate=true&apMinSalePrice=40000&apActive=true"
    )
    assert transport.requests[1].target.path.endswith("/vendor-items/456/original-prices/50000")
    assert transport.requests[2].target.path.endswith("/vendor-items/456/quantities/0")
    assert transport.requests[3].target.path.endswith("/vendor-items/456/sales/resume")
    assert transport.requests[4].target.path.endswith("/vendor-items/456/sales/stop")
    assert transport.requests[5].target.method is Method.DELETE
    assert all(request.body == b"" for request in transport.requests)


def test_auto_generated_option_endpoints_are_separate_seller_and_item_contracts() -> None:
    client, transport = _client(4)

    client.seller_auto_option(enabled=True)
    client.seller_auto_option(enabled=False)
    client.item_auto_option(VendorItemId(456), enabled=True)
    client.item_auto_option(VendorItemId(456), enabled=False)

    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/seller_api/apis/api/v1/marketplace/seller/auto-generated/opt-in",
        "/v2/providers/seller_api/apis/api/v1/marketplace/seller/auto-generated/opt-out",
        "/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/456/auto-generated/opt-in",
        "/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/456/auto-generated/opt-out",
    ]
    assert transport.requests[0].body == b""
    assert transport.requests[2].body == b""


def test_auto_generated_option_processing_is_retained_as_nonterminal_success() -> None:
    transport = FakeTransport(
        (FakeResponse(200, {"code": "PROCESSING", "message": "queued", "data": None}),)
    )
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.KR)
    client = CoupangProductClient(Authenticator(Clock()), context, transport)

    result = client.seller_auto_option(enabled=True)

    assert result.code == "PROCESSING"


def test_create_and_partial_documents_fail_closed_before_fake_transport() -> None:
    client, transport = _client(1)
    value = _create_document().value()
    value["vendorId"] = "OTHER"

    with pytest.raises(CoupangProductContractError, match="signing vendor"):
        client.create(FrozenDocument.from_mapping(value))
    with pytest.raises(CoupangProductContractError, match="undocumented field"):
        client.partial_update(
            SellerProductId(123),
            FrozenDocument.from_mapping({"sellerProductId": 123, "salePrice": 1000}),
        )
    with pytest.raises(CoupangProductContractError, match="10-won"):
        client.change_price(VendorItemId(456), 999)
    with pytest.raises(CoupangProductContractError, match="at most 10 minutes"):
        client.list_timeframe("2026-01-01T00:00:00", "2026-01-01T00:10:01")
    with pytest.raises(CoupangProductContractError, match="must be stopped"):
        client.delete(
            SellerProductId(123),
            eligibility=DeleteEligibility(all_items_stopped=False, awaiting_approval=False),
        )
    with pytest.raises(CoupangProductContractError, match="awaiting approval"):
        client.delete(
            SellerProductId(123),
            eligibility=DeleteEligibility(all_items_stopped=True, awaiting_approval=True),
        )
    assert transport.requests == []


def test_kr_only_auto_option_family_is_blocked_for_tw_before_transport() -> None:
    transport = FakeTransport((FakeResponse(200, _success()),))
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.TW)
    client = CoupangProductClient(Authenticator(Clock()), context, transport)

    with pytest.raises(CoupangProductContractError, match="KR only"):
        client.seller_auto_option(enabled=True)
    assert transport.requests == []


def test_only_success_envelopes_cross_the_response_boundary() -> None:
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.KR)
    transport = FakeTransport((FakeResponse(200, {"code": "ERROR", "message": "no"}),))
    client = CoupangProductClient(Authenticator(Clock()), context, transport)

    with pytest.raises(CoupangProductContractError, match="not successful"):
        client.inflow_status()
