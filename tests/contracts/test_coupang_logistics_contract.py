"""Repository contract for the provider-zero Coupang Logistics slice."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "integrations/marketplaces/coupang/logistics.py"


def test_logistics_adapter_has_no_network_or_live_execution_capability() -> None:
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
    assert "no socket" in source


def test_all_seven_logistics_paths_and_reference_versions_are_owned_here() -> None:
    source = MODULE.read_text(encoding="utf-8")

    expected_fragments = {
        "/returnShippingCenters",
        "/outboundShippingCenters",
        "/shipping-place/outbound",
        "/return/shipping-places/center-code",
        "COURIER_REFERENCE_VERSION",
        "POSTAL_REFERENCE_VERSION",
    }
    assert all(fragment in source for fragment in expected_fragments)
    assert "class LogisticsEndpoint" in source
    assert source.count(' = "') >= 7
