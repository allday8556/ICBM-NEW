"""C-P3 remains provider-zero and keeps the canonical roles separate."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "app/stages/register/marketplace_requirement_mapping.py"
ARCHITECTURE = ROOT / "documents/architecture/ARCHITECTURE.md"
ROADMAP = ROOT / "documents/roadmap/ROADMAP.md"
CI = ROOT / ".github/workflows/ci.yml"


def test_canonical_documents_define_the_c_p3_boundary() -> None:
    architecture = " ".join(ARCHITECTURE.read_text(encoding="utf-8").split())
    roadmap = " ".join(ROADMAP.read_text(encoding="utf-8").split())

    for phrase in (
        "Provider labels are display evidence only",
        "ListingComposition.quantity",
        "does not mutate Product Facts, Common Sales Options or AtomicSKUs",
    ):
        assert phrase in architecture
    assert "C-P3 Marketplace Requirement Metadata and Semantic Mapping" in roadmap


def test_mapper_has_no_provider_database_pricing_or_atomic_sku_dependency() -> None:
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
    assert not any("atomic_sku" in name for name in imported)


def test_mapper_never_uses_provider_label_as_a_match_key() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    comparisons = [node for node in ast.walk(tree) if isinstance(node, ast.Compare)]

    assert not any(
        isinstance(candidate.left, ast.Attribute) and candidate.left.attr == "provider_label"
        for candidate in comparisons
    )


def test_the_read_only_mapper_is_explicitly_provider_zero_in_ci() -> None:
    ci = CI.read_text(encoding="utf-8")
    provider_zero_exceptions = ci.split('case "$f" in', 1)[1].split(";;", 1)[0]

    assert "app/stages/register/marketplace_requirement_mapping.py" in provider_zero_exceptions
