"""C-AUTH-1 remains official-source-bound and provider-zero."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "documents" / "contracts" / "platforms" / "coupang" / "AUTH.md"
PACKAGE = ROOT / "integrations" / "marketplaces" / "coupang"


def test_contract_cites_the_official_auth_sources_and_safety_boundary() -> None:
    text = AUTH.read_text(encoding="utf-8")

    for source in (
        "https://developers.coupang.com/en/getting-started/creating-hmac-signature",
        "https://developers.coupang.com/en/getting-started/open-api-test-guide",
        "https://developers.coupang.com/en/getting-started/issue-open-api-keynew",
        "https://developers.coupang.com/en/faq/specified-signature-is-expired-401-error-return",
        "https://developers.coupang.com/en/faq/when-calling-the-api-a-403-forbidden-not-allowed-ip-error-occurs",
    ):
        assert source in text
    assert "Runtime endpoint adoption | `NONE`" in text
    assert "Real provider calls | `FORBIDDEN`" in text
    assert "credential readiness != endpoint adoption != LIVE authority" in text


def test_c_auth_1_contains_no_network_client_or_provider_send() -> None:
    sources = [path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py")]
    imported: set[str] = set()
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

    assert not {"httpx", "requests", "urllib.request", "socket"} & imported
    assert all("EGRESS.grant" not in source for source in sources)
