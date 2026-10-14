"""CGF Lite keeps its official order-label endpoint behind a provider-zero account gate."""

import pytest

from integrations.marketplaces.coupang.auth import AuthContext, Credentials, Market, Method
from integrations.marketplaces.coupang.cgf import (
    CGF_ENDPOINTS,
    CgfEffect,
    CgfEndpoint,
    CoupangCgfAccountGate,
    CoupangCgfClient,
)
from integrations.marketplaces.coupang.fake_transport import FakeTransport


def _client(*, market: Market = Market.KR) -> tuple[CoupangCgfClient, FakeTransport]:
    transport = FakeTransport(())
    context = AuthContext(Credentials("access", "secret", "A0001", 1), market)
    return CoupangCgfClient(context, transport), transport


def test_family_closes_the_single_official_endpoint() -> None:
    assert list(CgfEndpoint) == [CgfEndpoint.PRINT_ORDER_LABEL]
    assert len(CGF_ENDPOINTS) == 1
    assert CGF_ENDPOINTS[0].account_gated is True
    assert CGF_ENDPOINTS[0].effect is CgfEffect.DOCUMENT_READ
    assert CGF_ENDPOINTS[0].markets == (Market.KR, Market.TW)


def test_exact_method_and_route_are_retained_without_building_requests() -> None:
    spec = CGF_ENDPOINTS[0]
    assert (spec.endpoint, spec.method, spec.path_template) == (
        CgfEndpoint.PRINT_ORDER_LABEL,
        Method.POST,
        "/v2/providers/openapi/apis/api/v1/vendors/{vendorId}/label/printOrderLabel",
    )


@pytest.mark.parametrize("market", [Market.KR, Market.TW])
def test_indexed_markets_expose_the_same_operator_safe_blocked_state(market: Market) -> None:
    client, transport = _client(market=market)

    state = client.blocked_state(CgfEndpoint.PRINT_ORDER_LABEL)
    assert state.endpoint is CgfEndpoint.PRINT_ORDER_LABEL
    assert state.code == "COUPANG_CGF_LITE_ACCOUNT_GATED"
    assert state.can_execute is False
    assert state.required_evidence == (
        "cgf_lite_program_enrollment",
        "seller_account_contract",
    )
    assert "ENDPOINT_INVENTORY.md#cgf--cgf-lite--1" in state.recommendation_provenance
    assert transport.requests == []


@pytest.mark.parametrize("market", [Market.KR, Market.TW])
def test_endpoint_fails_before_request_building_or_transport(market: Market) -> None:
    client, transport = _client(market=market)

    with pytest.raises(CoupangCgfAccountGate, match="no request was built or sent"):
        client.require_program_entitlement(CgfEndpoint.PRINT_ORDER_LABEL)

    assert transport.requests == []
