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


def pricing_context_problems(metadata: MetaData) -> list[str]:
    """A table that holds a platform fee without the pricing context that fee depends on.

    ADR-0013 §7 (PR #81 review 5245152210, blocker 1): a fee varies by marketplace, account and
    policy, so a stored fee always travels with its context: a context reference, or at least the
    marketplace and the pricing policy version.
    """
    problems = []
    for table in metadata.tables.values():
        names = {column.name for column in table.columns}
        if not any("platform_fee" in name for name in names):
            continue
        if "pricing_context_id" in names or {"marketplace_key", "pricing_policy_version"} <= names:
            continue
        problems.append(table.name)
    return sorted(problems)


def decision_section(adr: str) -> str:
    """The binding part of the ADR: from "## Decision" up to the recorded rulings."""
    start = adr.index("\n## Decision")
    end = adr.index("\n## Rulings", start)
    return adr[start:end]


def snapshot_contract_problems(adr: str) -> list[str]:
    """The PricingSnapshot contract block must never list a platform fee without its context."""
    blocks = [
        block
        for block in re.findall(r"```text\n(.*?)```", adr, re.S)
        if block.lstrip().startswith("PricingSnapshot")
    ]
    if not blocks:
        return ["no PricingSnapshot contract block"]
    return [
        "platform_fee without pricing_context"
        for block in blocks
        if "platform_fee" in block and "pricing_context" not in block
    ]


# Ruling B: ABSENT options/tiers never become a source-side SKU or offer.
FABRICATION = re.compile(
    r"implicit\s+`?(SourceSKU|QuantityOffer)`?|quantity-1\s+`?QuantityOffer`?", re.I
)


def fabrication_problems(decision: str) -> list[str]:
    problems = [f"fabricates: {m.group(0)}" for m in FABRICATION.finditer(decision)]
    if "`BASE_PRODUCT`" not in decision:
        problems.append("no BASE_PRODUCT binding")
    if "creates and persists **no** `SourceSKU`" not in decision:
        problems.append("no explicit non-fabrication rule")
    return problems


READINESS_LAYERS = ("**Base readiness**", "**Pricing readiness**", "**Registration preflight**")
NO_UNIVERSAL_READINESS = "No single readiness result is claimed for an Item across pricing contexts"


def readiness_problems(decision: str) -> list[str]:
    problems = [f"missing layer {layer}" for layer in READINESS_LAYERS if layer not in decision]
    if NO_UNIVERSAL_READINESS not in decision:
        problems.append("no statement against a universal Item readiness")
    if re.search(r"evaluated per product-side Item", decision):
        problems.append("claims one readiness per Item")
    return problems


# Ruling A: the pointer may point to a REVIEW_REQUIRED revision, so it is never "accepted".
ACCEPTED_POINTER = re.compile(
    r"accepted[- ]revision pointer|accepted-revision|current accepted revision\b", re.I
)


def pointer_name_problems(decision: str) -> list[str]:
    return [m.group(0) for m in ACCEPTED_POINTER.finditer(decision)]


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


def test_no_table_holds_a_platform_fee_without_its_pricing_context() -> None:
    from app.db.metadata import metadata

    assert pricing_context_problems(metadata) == []


def test_the_pricing_context_detector_fires() -> None:
    synthetic = MetaData()
    Table(
        "pricing_snapshots",
        synthetic,
        Column("id", Integer, primary_key=True),
        Column("platform_fee", Integer),
    )
    Table(
        "contextual_snapshots",
        synthetic,
        Column("id", Integer, primary_key=True),
        Column("platform_fee", Integer),
        Column("marketplace_key", Integer),
        Column("pricing_policy_version", Integer),
    )
    assert pricing_context_problems(synthetic) == ["pricing_snapshots"]


def test_the_pricing_snapshot_contract_carries_its_context() -> None:
    adr = ADR_0013.read_text("utf-8")
    assert snapshot_contract_problems(adr) == []
    flat = "```text\nPricingSnapshot\n  item key\n  platform_fee\n```"
    assert snapshot_contract_problems(flat) == ["platform_fee without pricing_context"]


def test_absent_options_and_tiers_never_become_source_entities() -> None:
    decision = decision_section(ADR_0013.read_text("utf-8"))
    assert fabrication_problems(decision) == []
    first_draft = (
        "read its fields as **one implicit `SourceSKU` with one quantity-1 `QuantityOffer`**"
    )
    assert len(fabrication_problems(first_draft)) == 4


def test_readiness_is_layered_and_never_universal_across_pricing_contexts() -> None:
    decision = decision_section(ADR_0013.read_text("utf-8"))
    assert readiness_problems(decision) == []
    first_draft = (
        "Readiness is a server-side evaluation. At M4 it is evaluated per product-side Item."
    )
    assert "claims one readiness per Item" in readiness_problems(first_draft)


def test_the_current_source_revision_is_never_called_accepted() -> None:
    decision = decision_section(ADR_0013.read_text("utf-8"))
    assert pointer_name_problems(decision) == []
    first_draft = "### 3. The current accepted revision is a pointer; the accepted-revision pointer"
    assert pointer_name_problems(first_draft) == [
        "current accepted revision",
        "accepted-revision pointer",
    ]
