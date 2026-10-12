"""Rocket Growth keeps all seven official endpoints behind a provider-zero account gate."""

import pytest

from integrations.marketplaces.coupang.auth import AuthContext, Credentials, Market, Method
from integrations.marketplaces.coupang.fake_transport import FakeTransport
from integrations.marketplaces.coupang.rocket_growth import (
    ROCKET_GROWTH_ENDPOINTS,
    CoupangRocketGrowthAccountGate,
    CoupangRocketGrowthClient,
    CoupangRocketGrowthContractError,
    RocketGrowthEffect,
    RocketGrowthEndpoint,
)


def _client(*, market: Market = Market.KR) -> tuple[CoupangRocketGrowthClient, FakeTransport]:
    transport = FakeTransport(())
    context = AuthContext(Credentials("access", "secret", "A0001", 1), market)
    return CoupangRocketGrowthClient(context, transport), transport


def test_family_closes_all_seven_official_endpoints() -> None:
    assert len(RocketGrowthEndpoint) == 7
    assert len(ROCKET_GROWTH_ENDPOINTS) == 7
    assert len({spec.endpoint for spec in ROCKET_GROWTH_ENDPOINTS}) == 7
    assert all(spec.account_gated for spec in ROCKET_GROWTH_ENDPOINTS)
    assert sum(spec.effect is RocketGrowthEffect.READ for spec in ROCKET_GROWTH_ENDPOINTS) == 5
    assert sum(spec.effect is RocketGrowthEffect.MUTATION for spec in ROCKET_GROWTH_ENDPOINTS) == 2


def test_exact_methods_and_routes_are_retained_without_building_requests() -> None:
    assert [
        (spec.endpoint, spec.method, spec.path_template) for spec in ROCKET_GROWTH_ENDPOINTS
    ] == [
        (
            RocketGrowthEndpoint.LIST_CATEGORIES,
            Method.GET,
            "/v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories",
        ),
        (
            RocketGrowthEndpoint.MODIFY_PRODUCT,
            Method.PUT,
            "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products",
        ),
        (
            RocketGrowthEndpoint.CREATE_PRODUCT,
            Method.POST,
            "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products",
        ),
        (
            RocketGrowthEndpoint.LIST_PRODUCTS,
            Method.GET,
            "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products",
        ),
        (
            RocketGrowthEndpoint.READ_PRODUCT,
            Method.GET,
            "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{sellerProductId}",
        ),
        (
            RocketGrowthEndpoint.LIST_INVENTORY,
            Method.GET,
            "/v2/providers/rg_open_api/apis/api/v1/vendors/{vendorId}/rg/inventory/summaries",
        ),
        (
            RocketGrowthEndpoint.LIST_ORDERS,
            Method.GET,
            "/v2/providers/rg_open_api/apis/api/v1/vendors/{vendorId}/rg/orders",
        ),
    ]


def test_every_endpoint_exposes_the_same_operator_safe_blocked_state() -> None:
    client, transport = _client()

    for endpoint in RocketGrowthEndpoint:
        state = client.blocked_state(endpoint)
        assert state.endpoint is endpoint
        assert state.code == "COUPANG_ROCKET_GROWTH_ACCOUNT_GATED"
        assert state.can_execute is False
        assert state.required_evidence == (
            "rocket_growth_program_enrollment",
            "seller_account_contract",
        )
        assert "ENDPOINT_INVENTORY.md#rocket-growth--7" in state.recommendation_provenance

    assert transport.requests == []


def test_all_seven_endpoints_fail_before_request_building_or_transport() -> None:
    client, transport = _client()

    for endpoint in RocketGrowthEndpoint:
        with pytest.raises(CoupangRocketGrowthAccountGate, match="no request was built or sent"):
            client.require_program_entitlement(endpoint)

    assert transport.requests == []


def test_market_scope_fails_closed_before_transport() -> None:
    with pytest.raises(CoupangRocketGrowthContractError, match="KR only"):
        _client(market=Market.TW)
