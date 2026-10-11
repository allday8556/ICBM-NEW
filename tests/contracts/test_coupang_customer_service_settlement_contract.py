"""Repository contract for provider-zero Customer Service and Settlement slices."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CS_MODULE = ROOT / "integrations/marketplaces/coupang/customer_service.py"
SETTLEMENT_MODULE = ROOT / "integrations/marketplaces/coupang/settlement.py"


def test_adapters_have_no_network_or_live_execution_capability() -> None:
    source = CS_MODULE.read_text(encoding="utf-8") + SETTLEMENT_MODULE.read_text(encoding="utf-8")

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
    assert source.count("FakeTransport") >= 4
    assert source.count("provider-call, or LIVE") == 2


def test_all_eight_official_paths_remain_owned_by_fake_only_modules() -> None:
    customer_service = CS_MODULE.read_text(encoding="utf-8")
    settlement = SETTLEMENT_MODULE.read_text(encoding="utf-8")

    cs_fragments = {
        "/onlineInquiries/{identity}/replies",
        "/callCenterInquiries/{identity}/replies",
        "/callCenterInquiries/{identity}/confirms",
        "/onlineInquiries",
        "/callCenterInquiries",
        'f"{_V5}/callCenterInquiries/{identity}"',
    }
    assert all(fragment in customer_service for fragment in cs_fragments)
    assert "/revenue-history" in settlement
    assert "/settlement-histories" in settlement
    assert "class CustomerServiceEndpoint" in customer_service
    assert "class SettlementEndpoint" in settlement


def test_query_bounds_market_scope_and_sensitive_response_retention_are_explicit() -> None:
    customer_service = CS_MODULE.read_text(encoding="utf-8")
    settlement = SETTLEMENT_MODULE.read_text(encoding="utf-8")

    assert "include at most 7 days" in customer_service
    assert "pageSize must be 1..50" in customer_service
    assert "pageSize must be 1..30" in customer_service
    assert "include at most 31 days" in settlement
    assert "Settlement endpoints are documented for KR only" in settlement
    assert customer_service.count("field(repr=False)") == 1
    assert settlement.count("field(repr=False)") == 1
