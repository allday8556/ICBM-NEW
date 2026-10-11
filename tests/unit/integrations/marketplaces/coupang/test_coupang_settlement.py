"""Settlement read contracts are exact, fail-closed, and fake-only."""

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
from integrations.marketplaces.coupang.settlement import (
    CoupangSettlementClient,
    CoupangSettlementContractError,
    SettlementEndpoint,
)


@dataclass(frozen=True)
class Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _client(
    count: int, *, market: Market = Market.KR
) -> tuple[CoupangSettlementClient, FakeTransport]:
    transport = FakeTransport(
        tuple(
            FakeResponse(200, {"code": 200, "message": "OK", "data": [], "nextToken": "next"})
            for _ in range(count)
        )
    )
    context = AuthContext(Credentials("access", "secret", "A0001", 1), market)
    return CoupangSettlementClient(Authenticator(Clock()), context, transport), transport


def test_family_closes_both_official_read_endpoints() -> None:
    assert len(SettlementEndpoint) == 2


def test_sales_and_settlement_queries_preserve_exact_contracts() -> None:
    client, transport = _client(2)

    sales = client.list_sales("2026-01-01", "2026-01-31", token="next 1", max_per_page=50)
    settlements = client.list_settlements("2026-01")

    assert sales.endpoint is SettlementEndpoint.LIST_SALES
    assert sales.next_token == "next"
    assert settlements.endpoint is SettlementEndpoint.LIST_SETTLEMENTS
    assert [request.target.method for request in transport.requests] == [Method.GET, Method.GET]
    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/openapi/apis/api/v1/revenue-history",
        "/v2/providers/marketplace_openapi/apis/api/v1/settlement-histories",
    ]
    assert transport.requests[0].target.query == (
        "vendorId=A0001&recognitionDateFrom=2026-01-01&recognitionDateTo=2026-01-31"
        "&token=next+1&maxPerPage=50"
    )
    assert transport.requests[1].target.query == "revenueRecognitionYearMonth=2026-01"
    assert all(request.body == b"" for request in transport.requests)


def test_first_sales_page_retains_required_empty_token() -> None:
    client, transport = _client(1)

    client.list_sales("2026-01-01", "2026-01-01")

    assert transport.requests[0].target.query.endswith("&token=&maxPerPage=50")


def test_kr_only_scope_and_validation_fail_before_transport() -> None:
    with pytest.raises(CoupangSettlementContractError, match="KR only"):
        _client(1, market=Market.TW)

    client, transport = _client(1)
    with pytest.raises(CoupangSettlementContractError, match="at most 31 days"):
        client.list_sales("2026-01-01", "2026-02-01")
    with pytest.raises(CoupangSettlementContractError, match=r"1\.\.50"):
        client.list_sales("2026-01-01", "2026-01-02", max_per_page=0)
    with pytest.raises(CoupangSettlementContractError, match="valid month"):
        client.list_settlements("2026-13")

    assert transport.requests == []


def test_fake_response_failure_is_not_projected_as_financial_data() -> None:
    transport = FakeTransport((FakeResponse(500, {"code": 500, "data": []}),))
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.KR)
    client = CoupangSettlementClient(Authenticator(Clock()), context, transport)

    with pytest.raises(CoupangSettlementContractError, match="Coupang HTTP 500"):
        client.list_settlements("2026-01")
