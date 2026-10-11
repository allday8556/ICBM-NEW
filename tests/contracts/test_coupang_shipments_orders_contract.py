"""Repository contract for the provider-zero Shipments & Orders slice."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "integrations/marketplaces/coupang/orders.py"


def test_orders_adapter_has_no_network_or_live_execution_capability() -> None:
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
    assert "POSTAL_CODES_RUNTIME_ADOPTED = False" in source


def test_all_twelve_implemented_paths_remain_owned_by_one_fake_only_module() -> None:
    source = MODULE.read_text(encoding="utf-8")

    expected_fragments = {
        "/orders/{identity}/cancel",
        "/ordersheets/acknowledgement",
        "/completeLongTermUndelivery",
        "/completedShipment",
        "/stoppedShipment",
        "/ordersheets",
        "/ordersheets/{identity}/history",
        "/ordersheets/{identity}",
        "/{identity}/ordersheets",
        "/orders/updateInvoices",
        "/orders/invoices",
    }
    assert all(fragment in source for fragment in expected_fragments)
    assert "class OrderEndpoint" in source
    assert source.count(' = "') >= 12


def test_market_scope_and_reference_only_boundaries_are_explicit() -> None:
    source = MODULE.read_text(encoding="utf-8")

    assert "Market.KR" in source
    assert "Market.TW" in source
    assert "buyer-market offset" in source
    assert "south-korean-cities-and-postal-codes" in source
    assert "Taiwan direct-courier pair remains out of product scope" in source
