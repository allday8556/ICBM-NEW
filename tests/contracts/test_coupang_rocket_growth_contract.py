"""Repository contract for the provider-zero Coupang Rocket Growth slice."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "integrations/marketplaces/coupang/rocket_growth.py"


def test_adapter_has_no_network_request_signing_or_live_capability() -> None:
    source = MODULE.read_text(encoding="utf-8")

    for forbidden in (
        "import httpx",
        "import requests",
        "import socket",
        "urlopen",
        "COUPANG_BASE_URL",
        "ExecutionMode.LIVE",
        "Authenticator",
        "RequestTarget",
        "FakeRequest",
        "keyring",
    ):
        assert forbidden not in source
    assert source.count("FakeTransport") >= 2
    assert "no request was built or sent" in source
    assert "credential, provider-call, or LIVE authority" in source


def test_all_seven_account_gated_endpoint_specs_are_explicit() -> None:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    specs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "RocketGrowthEndpointSpec"
    ]

    assert "class RocketGrowthEndpoint" in source
    assert "ROCKET_GROWTH_ENDPOINTS" in source
    assert len(specs) == 7
    assert "account_gated: bool = True" in source
    for fragment in (
        "/meta/display-categories",
        "/seller-products",
        "/seller-products/{{sellerProductId}}",
        "/{{vendorId}}/rg/inventory/summaries",
        "/{{vendorId}}/rg/orders",
    ):
        assert fragment in source


def test_blocked_state_requires_program_and_contract_proof() -> None:
    source = MODULE.read_text(encoding="utf-8")

    assert "class RocketGrowthBlockedState" in source
    assert 'code: str = "COUPANG_ROCKET_GROWTH_ACCOUNT_GATED"' in source
    assert "rocket_growth_program_enrollment" in source
    assert "seller_account_contract" in source
    assert "Credentials alone are not proof of entitlement" in source
    assert "can_execute: bool = False" in source
    assert "recommendation_provenance" in source
