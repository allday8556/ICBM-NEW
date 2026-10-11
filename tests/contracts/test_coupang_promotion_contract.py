"""Repository contract for the provider-zero Coupang Promotion slice."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "integrations/marketplaces/coupang/promotions.py"


def test_adapter_has_no_network_entitlement_or_live_execution_capability() -> None:
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
    assert source.count("FakeTransport") >= 2
    assert "no request was sent" in source
    assert "credential, provider-call, entitlement, or LIVE authority" in source


def test_all_twenty_one_paths_and_the_eighteen_account_gates_are_explicit() -> None:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    specs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_spec"
    ]

    assert "class PromotionEndpoint" in source
    assert "PROMOTION_ENDPOINTS" in source
    assert len(specs) == 21
    assert source.count("account_gated=False") == 3
    assert "sum(spec.account_gated" not in source
    for fragment in (
        "/contract/list",
        "/products/items/cashback",
        "/coupons/transactionStatus",
        "/coupons/expire",
        "/coupon-items",
        "/requested/{{requestedId}}",
        "/{{orderId}}/coupons",
        "/items/{{couponItemId}}",
        "/items/{{vendorItemId}}",
    ):
        assert fragment in source


def test_common_reads_and_sensitive_response_retention_are_explicit() -> None:
    source = MODULE.read_text(encoding="utf-8")

    assert "def list_contracts" in source
    assert "def read_contract" in source
    assert "def list_budgets" in source
    assert "targetMonth must use a valid YYYY-MM month" in source
    assert source.count("field(repr=False)") == 1
