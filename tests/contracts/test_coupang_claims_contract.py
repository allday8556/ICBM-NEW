"""Repository contract for the provider-zero Returns & Exchanges slice."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "integrations/marketplaces/coupang/claims.py"


def test_claims_adapter_has_no_network_or_live_execution_capability() -> None:
    source = MODULE.read_text(encoding="utf-8")

    for forbidden in (
        "import httpx",
        "import requests",
        "import socket",
        "urlopen",
        "COUPANG_BASE_URL",
        "ExecutionMode.LIVE",
        "keyring",
    ):
        assert forbidden not in source
    assert "FakeTransport" in source
    assert "provider-call, or LIVE" in source


def test_all_eleven_official_paths_remain_owned_by_one_fake_only_module() -> None:
    source = MODULE.read_text(encoding="utf-8")

    expected_fragments = {
        "/returnRequests/{receipt}/approval",
        "/returnRequests/{receipt}",
        "/returnWithdrawRequests",
        "/returnWithdrawList",
        "/returnRequests",
        "/returnRequests/{receipt}/receiveConfirmation",
        "/return-exchange-invoices/manual",
        "/exchangeRequests/{identity}/receiveConfirmation",
        "/exchangeRequests",
        "/exchangeRequests/{identity}/rejection",
        "/exchangeRequests/{identity}/invoices",
    }
    assert all(fragment in source for fragment in expected_fragments)
    assert "class ClaimEndpoint" in source
    assert source.count(' = "') >= 11


def test_market_scope_date_windows_and_fake_response_retention_are_explicit() -> None:
    source = MODULE.read_text(encoding="utf-8")

    assert "Market.KR" in source
    assert "Market.TW" in source
    assert "include at most 7 days" in source
    assert 'days=31, name="return query"' in source
    assert "FrozenEnvelope" in source
    assert "field(repr=False)" in source
