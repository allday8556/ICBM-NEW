"""Repository contract for the provider-zero Coupang CGF Lite slice."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "integrations/marketplaces/coupang/cgf.py"


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
    assert "credential, provider-call, or\nLIVE authority" in source


def test_single_account_gated_endpoint_spec_is_explicit() -> None:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    specs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CgfEndpointSpec"
    ]

    assert "class CgfEndpoint" in source
    assert "CGF_ENDPOINTS" in source
    assert len(specs) == 1
    assert "account_gated: bool = True" in source
    assert "/{{vendorId}}/label/printOrderLabel" in source


def test_blocked_state_requires_program_and_contract_proof() -> None:
    source = MODULE.read_text(encoding="utf-8")

    assert "class CgfBlockedState" in source
    assert 'code: str = "COUPANG_CGF_LITE_ACCOUNT_GATED"' in source
    assert "cgf_lite_program_enrollment" in source
    assert "seller_account_contract" in source
    assert "Credentials alone are not proof of entitlement" in source
    assert "can_execute: bool = False" in source
    assert "recommendation_provenance" in source
