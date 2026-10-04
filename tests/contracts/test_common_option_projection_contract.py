"""C-P1's provider-zero boundary stays smaller than readiness or provider projection."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "app/stages/register/marketplace_option_projection.py"
ARCHITECTURE = ROOT / "documents/architecture/ARCHITECTURE.md"
ROADMAP = ROOT / "documents/roadmap/ROADMAP.md"
CI = ROOT / ".github/workflows/ci.yml"


def test_canonical_documents_define_the_c_p1_boundary() -> None:
    architecture = " ".join(ARCHITECTURE.read_text(encoding="utf-8").split())
    roadmap = " ".join(ROADMAP.read_text(encoding="utf-8").split())

    for phrase in (
        "reviewed metadata's documented structure ranges",
        "does not mean registration-ready, sendable or provider-verified",
        "never creates a Cartesian combination",
    ):
        assert phrase in architecture
    assert "C-P1 Marketplace Option Structure Compatibility" in roadmap


def test_the_c_p1_module_has_no_provider_database_or_pricing_dependency() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )

    assert not any(name.startswith("integrations") for name in imported)
    assert not any("platform.db" in name for name in imported)
    assert not any("pricing" in name for name in imported)


def test_the_read_only_projection_is_explicitly_provider_zero_in_ci() -> None:
    ci = CI.read_text(encoding="utf-8")
    provider_zero_exceptions = ci.split('case "$f" in', 1)[1].split(";;", 1)[0]

    assert "app/stages/register/marketplace_option_projection.py" in provider_zero_exceptions
