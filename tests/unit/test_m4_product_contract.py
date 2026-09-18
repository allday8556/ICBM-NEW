"""Repository contract for M4 PR-A (Issue #80, ADR-0013), pinned before any M4 schema exists.

Each checker is a pure function. It runs against the real repository, and also against a small
synthetic violation, so a rule that could never fire is caught as surely as a rule that fails.
"""

import ast
import re
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import Column, Integer, MetaData, Table

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
ADR_0013 = DOCS / "adr" / "0013-m4-canonical-product-contract.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ARCHITECTURE_MD = DOCS / "ARCHITECTURE.md"
ROADMAP_MD = REPO_ROOT / "ROADMAP.md"
CODE_ROOTS = ("app", "integrations", "scripts")

# ADR-0013 §1: the canonical Product is the v3.1 ProductGroup. A table for a second product root
# beside it would be the "two competing canonical identities" Issue #80 §4 forbids.
SECOND_PRODUCT_ROOTS = frozenset({"product", "products", "canonical_product", "canonical_products"})
# ADR-0013 §4, Canonical v3.1 §9.4 and §12.1: never on the canonical product (group).
FORBIDDEN_COLUMNS = frozenset({"primary_source_id", "allow_duplicate"})


# ---------------------------------------------------------------- checkers


def canonical_pricing_rule(text: str) -> str | None:
    """The fenced block holding the canonical pricing rule, whitespace-normalized."""
    for block in re.findall(r"```(?:text)?\n(.*?)```", text, re.S):
        if "if minimum_sale_price exists:" in block:
            return "\n".join(line.strip() for line in block.strip().splitlines())
    return None


def max_pricing_calls(sources: Iterable[tuple[str, str]]) -> list[str]:
    """Calls of builtin ``max`` that mix a target-margin price with a minimum sale price."""
    offenders = []
    for where, source in sources:
        for node in ast.walk(ast.parse(source)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "max"
            ):
                continue
            text = " ".join(
                ast.unparse(arg) for arg in [*node.args, *(k.value for k in node.keywords)]
            )
            if "target_margin" in text and "minimum_sale" in text:
                offenders.append(f"{where}:{node.lineno}")
    return offenders


def product_root_problems(metadata: MetaData) -> list[str]:
    """A second product root, or a column the canonical product must never carry."""
    problems = [f"table {name}" for name in metadata.tables if name in SECOND_PRODUCT_ROOTS]
    problems += [
        f"{table.name}.{column.name}"
        for table in metadata.tables.values()
        for column in table.columns
        if column.name in FORBIDDEN_COLUMNS
    ]
    return sorted(problems)


def _code() -> list[tuple[str, str]]:
    return [
        (path.relative_to(REPO_ROOT).as_posix(), path.read_text("utf-8"))
        for root in CODE_ROOTS
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
    ]


# ---------------------------------------------------------------- the contract


def test_adr_0013_is_recorded_and_referenced_by_the_canonical_documents() -> None:
    adr = ADR_0013.read_text("utf-8")
    header = adr.split("\n## ", 1)[0]
    assert re.search(r"^Status: \*\*(PROPOSED|ACCEPTED)\*\*", header, re.M)
    # §1 decides the relationship in so many words, before any schema (Issue #80 §4).
    assert "the `Product` **is** the `ProductGroup`" in adr
    for document in (ARCHITECTURE_MD, ROADMAP_MD):
        assert "docs/adr/0013-m4-canonical-product-contract.md" in document.read_text("utf-8")


def test_the_canonical_pricing_rule_reads_the_same_in_every_owner_document() -> None:
    rules = {
        path.name: canonical_pricing_rule(path.read_text("utf-8"))
        for path in (CLAUDE_MD, ARCHITECTURE_MD, ADR_0013)
    }
    assert None not in rules.values(), rules
    assert len(set(rules.values())) == 1, rules
    rule = rules[CLAUDE_MD.name] or ""
    assert "final_sale_price = minimum_sale_price" in rule
    assert "price_basis = MINIMUM_SALE_PRICE" in rule


def test_a_drifted_pricing_rule_is_detected() -> None:
    drifted = "```text\nif minimum_sale_price exists:\n    final_sale_price = max(a, b)\n```"
    assert canonical_pricing_rule(drifted) != canonical_pricing_rule(ADR_0013.read_text("utf-8"))


def test_no_code_restores_the_max_pricing_rule() -> None:
    assert max_pricing_calls(_code()) == []


def test_the_max_pricing_rule_detector_fires() -> None:
    sources = [
        ("bad.py", "price = max(target_margin_price, minimum_sale_price)\n"),
        ("also_bad.py", "p = max(snapshot.minimum_sale_price, calc.target_margin(x))\n"),
        ("fine.py", "p = max(width, height)\nq = minimum_sale_price or target_margin_price\n"),
    ]
    assert max_pricing_calls(sources) == ["bad.py:1", "also_bad.py:1"]


def test_the_schema_has_no_second_product_root_and_no_forbidden_group_column() -> None:
    from app.db.metadata import metadata

    assert product_root_problems(metadata) == []


def test_the_product_root_detector_fires() -> None:
    synthetic = MetaData()
    Table("product_groups", synthetic, Column("id", Integer, primary_key=True))
    Table("products", synthetic, Column("id", Integer, primary_key=True))
    Table(
        "group_members",
        synthetic,
        Column("id", Integer, primary_key=True),
        Column("primary_source_id", Integer),
        Column("allow_duplicate", Integer),
    )
    assert product_root_problems(synthetic) == [
        "group_members.allow_duplicate",
        "group_members.primary_source_id",
        "table products",
    ]
