"""Promotion contracts preserve three reads and fail closed for eighteen gated endpoints."""

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
from integrations.marketplaces.coupang.promotions import (
    PROMOTION_ENDPOINTS,
    CoupangPromotionAccountGate,
    CoupangPromotionClient,
    CoupangPromotionContractError,
    PromotionEndpoint,
)


@dataclass(frozen=True)
class Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _client(
    count: int, *, market: Market = Market.KR
) -> tuple[CoupangPromotionClient, FakeTransport]:
    transport = FakeTransport(
        tuple(
            FakeResponse(200, {"code": 200, "message": "OK", "data": {"success": True}})
            for _ in range(count)
        )
    )
    context = AuthContext(Credentials("access", "secret", "A0001", 1), market)
    return CoupangPromotionClient(Authenticator(Clock()), context, transport), transport


def test_family_closes_all_twenty_one_official_endpoints() -> None:
    assert len(PromotionEndpoint) == 21
    assert len(PROMOTION_ENDPOINTS) == 21
    assert len({spec.endpoint for spec in PROMOTION_ENDPOINTS}) == 21
    assert sum(spec.account_gated for spec in PROMOTION_ENDPOINTS) == 18
    assert sum(not spec.account_gated for spec in PROMOTION_ENDPOINTS) == 3


def test_common_contract_and_budget_reads_preserve_exact_routes() -> None:
    client, transport = _client(3)

    client.list_contracts()
    client.read_contract(9962)
    client.list_budgets(contract_id=-1, target_month="2026-01")

    assert [request.target.method for request in transport.requests] == [Method.GET] * 3
    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/fms/apis/api/v2/vendors/A0001/contract/list",
        "/v2/providers/fms/apis/api/v2/vendors/A0001/contract",
        "/v2/providers/fms/apis/api/v1/vendors/A0001/budgets",
    ]
    assert [request.target.query for request in transport.requests] == [
        "",
        "contractId=9962",
        "contractId=-1&targetMonth=2026-01",
    ]
    assert all(request.body == b"" for request in transport.requests)


def test_optional_budget_filters_remain_optional() -> None:
    client, transport = _client(1)

    client.list_budgets()

    assert transport.requests[0].target.query == ""


def test_all_eighteen_contract_bound_endpoints_fail_before_transport() -> None:
    client, transport = _client(0)
    gated = [spec.endpoint for spec in PROMOTION_ENDPOINTS if spec.account_gated]

    for endpoint in gated:
        with pytest.raises(CoupangPromotionAccountGate, match="no request was sent"):
            client.require_account_entitlement(endpoint)

    assert len(gated) == 18
    assert transport.requests == []


def test_scope_and_input_validation_fail_before_transport() -> None:
    with pytest.raises(CoupangPromotionContractError, match="KR only"):
        _client(0, market=Market.TW)

    client, transport = _client(0)
    with pytest.raises(CoupangPromotionContractError, match="positive integer"):
        client.read_contract(0)
    with pytest.raises(CoupangPromotionContractError, match="positive integer or -1"):
        client.list_budgets(contract_id=-2)
    with pytest.raises(CoupangPromotionContractError, match="valid YYYY-MM"):
        client.list_budgets(target_month="2026-13")
    with pytest.raises(CoupangPromotionContractError, match="common read"):
        client.require_account_entitlement(PromotionEndpoint.LIST_CONTRACTS)

    assert transport.requests == []


def test_fake_failure_is_not_projected_as_promotion_data() -> None:
    transport = FakeTransport((FakeResponse(200, {"code": 200, "data": {"success": False}}),))
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.KR)
    client = CoupangPromotionClient(Authenticator(Clock()), context, transport)

    with pytest.raises(CoupangPromotionContractError, match="not successful"):
        client.list_contracts()
