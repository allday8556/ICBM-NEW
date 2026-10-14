"""C-AUTH-1 stays provider-zero; reviewed live owners are closed bounded exceptions."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "documents" / "contracts" / "platforms" / "coupang" / "AUTH.md"
PACKAGE = ROOT / "integrations" / "marketplaces" / "coupang"
CLI = ROOT / "app" / "interface" / "cli.py"


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
    assert "C-LIVE-R1 bounded extension" in text
    assert "One exact KR `GET`" in text
    assert "Ambiguous create recovery" in text
    assert "zero results never authorize a repeated create" in text


def test_c_auth_1_contains_no_network_client_outside_reviewed_live_owners() -> None:
    imports_by_file: dict[str, set[str]] = {}
    sources: dict[str, str] = {}
    for path in PACKAGE.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        sources[path.name] = source
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        network = imported & {"httpx", "requests", "urllib.request", "socket"}
        if network:
            imports_by_file[path.name] = network

    assert imports_by_file == {
        "canary.py": {"httpx"},
        "live_mutations.py": {"httpx"},
    }
    assert sorted(name for name, source in sources.items() if "EGRESS.grant" in source) == [
        "canary.py",
        "live_mutations.py",
    ]
    canary = sources["canary.py"]
    assert 'client.stream("GET", path' in canary
    assert "Method.GET" in canary
    assert all(f"Method.{method}" not in canary for method in ("POST", "PUT", "PATCH", "DELETE"))
    assert canary.count("client.stream(") == 1
    assert "tenacity" not in canary
    mutations = sources["live_mutations.py"]
    assert "TEST_PRODUCT_BINDINGS" in mutations
    assert "COUPANG_MUTATION_RECOVERY_REQUIRED" in mutations
    assert "COUPANG_MUTATION_RECOVERY_NO_POSITIVE_MATCH" in mutations
    assert "tenacity" not in mutations


def test_cli_accepts_secrets_only_through_no_echo_prompts() -> None:
    source = CLI.read_text(encoding="utf-8")

    assert 'getpass("Coupang Access Key (hidden): ")' in source
    assert 'getpass("Coupang Secret Key (hidden): ")' in source
    assert 'add_argument("--access-key"' not in source
    assert 'add_argument("--secret-key"' not in source
    assert '("coupang", "save-credentials"): _coupang_credentials' in source
    assert '("coupang", "read-canary"): _coupang_read_canary' in source
    assert '("coupang", "mutate-test-products"): _coupang_mutate_test_products' in source
    assert (
        '("coupang", "recover-test-product-create"): _coupang_recover_test_product_create'
    ) in source
