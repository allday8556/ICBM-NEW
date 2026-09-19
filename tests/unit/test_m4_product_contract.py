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


# PR #82 review 5247426764 blocker 2: a change to a group's CONFIRMED set and its membership
# revision are one unit of work, and only the product store writes membership.
MEMBERSHIP_OWNER = "app/products/store.py"
MEMBERSHIP_MODELS = "app/products/models.py"
MEMBERSHIP_CLASSES = frozenset({"GroupMember", "GroupMembershipRevision"})
MEMBERSHIP_TABLES = re.compile(r"\bgroup_members\b|\bgroup_membership_revisions\b")
APPEND_REVISION = "_append_membership_revision"
CANDIDATE_STATUS = "MemberStatus.CANDIDATE.value"


def membership_writer_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    """Production code, other than the store, its models and the migrations, that names the
    membership models or tables, and so could change membership without its revision."""
    offenders = []
    for where, source in sources:
        if where in (MEMBERSHIP_OWNER, MEMBERSHIP_MODELS) or "/migrations/" in where:
            continue
        for node in ast.walk(ast.parse(source)):
            named = (
                node.name
                if isinstance(node, ast.alias)
                else node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else None
            )
            text = node.value if isinstance(node, ast.Constant) else None
            if named in MEMBERSHIP_CLASSES or (
                isinstance(text, str) and MEMBERSHIP_TABLES.search(text)
            ):
                offenders.append(f"{where}:{getattr(node, 'lineno', 0)}")
    return offenders


def membership_changers(source: str) -> dict[str, bool]:
    """Every method that could change a CONFIRMED set, and whether it appends the revision.

    A method changes the set when it builds a ``GroupMember`` whose status is not literally a
    candidate, or assigns a ``status`` attribute. Building a candidate changes nothing canonical.
    """
    found: dict[str, bool] = {}
    for cls in (n for n in ast.parse(source).body if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            if fn.name == APPEND_REVISION:
                continue
            changes = False
            appends = False
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    callee = node.func
                    if isinstance(callee, ast.Name) and callee.id == "GroupMember":
                        status = next((k.value for k in node.keywords if k.arg == "status"), None)
                        if status is None or ast.unparse(status) != CANDIDATE_STATUS:
                            changes = True
                    if isinstance(callee, ast.Attribute) and callee.attr == APPEND_REVISION:
                        appends = True
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Attribute) and t.attr == "status" for t in node.targets
                ):
                    changes = True
            if changes:
                found[f"{cls.name}.{fn.name}"] = appends
    return found


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


def test_only_the_product_store_writes_membership() -> None:
    assert membership_writer_problems(_code()) == []


def test_the_membership_writer_detector_fires() -> None:
    sources = [
        ("app/other/mod.py", "from app.products.models import GroupMember\n"),
        ("app/other/raw.py", "SQL = 'UPDATE group_members SET status = 1'\n"),
        ("app/db/migrations/versions/0099_x.py", "T = 'group_members'\n"),
        (MEMBERSHIP_OWNER, "from app.products.models import GroupMember\n"),
    ]
    assert membership_writer_problems(sources) == ["app/other/mod.py:1", "app/other/raw.py:1"]


def test_every_confirmed_set_change_in_the_store_appends_its_revision() -> None:
    changers = membership_changers((REPO_ROOT / MEMBERSHIP_OWNER).read_text("utf-8"))
    # Exactly the two methods that can change a CONFIRMED set, and both append. They live on the
    # unit of work, which the store's single writes and PR-C's materializer both use.
    assert changers == {
        "ProductFoundationUnit.confirm_new_member": True,
        "ProductFoundationUnit.change_member_status": True,
    }


def test_the_unrecorded_change_detector_fires() -> None:
    source = (
        "class Store:\n"
        "    def add_candidate(self):\n"
        "        GroupMember(status=MemberStatus.CANDIDATE.value)\n"
        "    def sneak_confirm(self):\n"
        "        GroupMember(status=MemberStatus.CONFIRMED.value)\n"
        "    def sneak_status(self, row):\n"
        "        row.status = 'REJECTED'\n"
        "    def recorded(self, row, session):\n"
        "        row.status = 'CONFIRMED'\n"
        "        self._append_membership_revision(session)\n"
    )
    assert membership_changers(source) == {
        "Store.sneak_confirm": False,
        "Store.sneak_status": False,
        "Store.recorded": True,
    }


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


# ---------------------------------------------------------------- PR-D pricing and readiness
#
# Issue #80 PR-D kickoff 5737440897 §L. Each rule runs on the real repository and on a synthetic
# violation, so a rule that could never fire is caught as surely as one that fails.

PRICING_MODULES = (
    "app/products/pricing.py",
    "app/products/pricing_service.py",
    "app/products/pricing_store.py",
    "app/products/readiness.py",
)
READINESS_TRUTH = re.compile(r"registerable|readiness|(^|_)ready($|_)", re.I)
# No marketplace is named in the Product DB (ADR-0013 §7): a fee named after one would be a
# hardcoded fee table, and no fee table is accepted in the repository.
MARKETPLACE_NAMES = re.compile(
    r"smart_?store|coupang|11st|eleven_?st|gmarket|auction|lotteon|interpark|wemakeprice|tmon",
    re.I,
)
MANUFACTURED = re.compile(r"minimum_sale|quantity|purchase_cost|amount_krw")


def readiness_truth_problems(metadata: MetaData) -> list[str]:
    """A table or column that would store readiness or registrability as a truth (ADR-0013 §8)."""
    problems = [f"table {name}" for name in metadata.tables if READINESS_TRUTH.search(name)]
    problems += [
        f"{table.name}.{column.name}"
        for table in metadata.tables.values()
        for column in table.columns
        if READINESS_TRUTH.search(column.name)
    ]
    return sorted(problems)


def marketplace_named_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    offenders = []
    for where, source in sources:
        if not where.startswith("app/products/"):
            continue
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and MARKETPLACE_NAMES.search(node.value)
            ):
                offenders.append(f"{where}:{node.lineno}")
    return offenders


def manufactured_price_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    """A multiplication or division involving a source amount, a minimum or a quantity: no
    ``minimum_sale_price × quantity`` and no unit price is ever manufactured (ADR-0013 §7)."""
    offenders = []
    for where, source in sources:
        if where not in PRICING_MODULES:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.BinOp) and isinstance(
                node.op, ast.Mult | ast.Div | ast.FloorDiv
            ):
                operands = f"{ast.unparse(node.left)} {ast.unparse(node.right)}"
                if MANUFACTURED.search(operands):
                    offenders.append(f"{where}:{node.lineno}")
    return offenders


def float_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    """A float anywhere money, a rate or a margin is computed or compared."""
    offenders = []
    for where, source in sources:
        if where not in PRICING_MODULES:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"{where}:{node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                offenders.append(f"{where}:{node.lineno}")
    return offenders


def test_no_readiness_or_registrability_is_stored() -> None:
    from app.db.metadata import metadata

    assert readiness_truth_problems(metadata) == []


def test_the_readiness_truth_detector_fires() -> None:
    synthetic = MetaData()
    Table(
        "pricing_snapshots",
        synthetic,
        Column("id", Integer, primary_key=True),
        Column("registerable", Integer),
        Column("is_ready", Integer),
    )
    Table("item_readiness", synthetic, Column("id", Integer, primary_key=True))
    Table("already_there", synthetic, Column("id", Integer, primary_key=True))
    assert readiness_truth_problems(synthetic) == [
        "pricing_snapshots.is_ready",
        "pricing_snapshots.registerable",
        "table item_readiness",
    ]


def test_no_marketplace_is_named_in_the_product_db() -> None:
    assert marketplace_named_problems(_code()) == []


def test_the_marketplace_name_detector_fires() -> None:
    sources = [
        ("app/products/pricing.py", "FEES = {'smartstore': '0.0563'}\n"),
        ("app/products/other.py", "KEY = 'Coupang'\n"),
        ("app/products/fine.py", "KEY = 'market_a'\n"),
        ("integrations/marketplaces/x.py", "KEY = 'smartstore'\n"),
    ]
    assert marketplace_named_problems(sources) == [
        "app/products/pricing.py:1",
        "app/products/other.py:1",
    ]


def test_no_price_is_manufactured_from_a_quantity_or_divided_into_a_unit() -> None:
    assert manufactured_price_problems(_code()) == []


def test_the_manufactured_price_detector_fires() -> None:
    sources = [
        ("app/products/pricing.py", "final = inputs.minimum_sale_price_krw * item_quantity\n"),
        ("app/products/pricing_service.py", "unit = offer.amount_krw / tier.quantity\n"),
        ("app/products/readiness.py", "fee = rate * final_sale_price\n"),
    ]
    assert manufactured_price_problems(sources) == [
        "app/products/pricing.py:1",
        "app/products/pricing_service.py:1",
    ]


def test_no_float_enters_pricing() -> None:
    assert float_problems(_code()) == []


def test_the_float_detector_fires() -> None:
    sources = [
        ("app/products/pricing.py", "below = 1 - cost / final < 0.1\n"),
        ("app/products/readiness.py", "m = float(profit) / final\n"),
        ("app/products/pricing_store.py", "n = 10\n"),
    ]
    assert float_problems(sources) == ["app/products/pricing.py:1", "app/products/readiness.py:1"]


# ---------------------------------------------------------------- PR-E images
#
# Issue #80 PR-E kickoff 5738166312 §K. Selection is an operator decision owned by the image
# service, and PRODUCT code never writes COLLECT's source truth.

SELECTION_OWNERS = frozenset(
    {"app/products/images.py", "app/products/image_store.py", "app/products/image_models.py"}
)
SELECTION_NAMES = frozenset(
    {
        "record_selection",
        "record_operator_selection",
        "ImageSelectionRevision",
        "ImageSelectionSourceDecision",
        "ImageSelectionOutput",
        "CurrentImageSelectionMove",
    }
)
SELECTION_TABLES = re.compile(
    r"\bimage_selection_revisions\b|\bimage_selection_source_decisions\b"
    r"|\bimage_selection_outputs\b|\bcurrent_image_selection_moves\b"
)
SOURCE_TRUTH_WRITERS = frozenset({"SourceAssetStore", "SourceAssetRecorder"})
SOURCE_TRUTH_MODELS = frozenset({"SourceAsset", "ProductFactsImageRef", "ProductFactsRevision"})


def _named(node: ast.AST) -> str | None:
    if isinstance(node, ast.alias):
        return node.name
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def image_selection_writer_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    """Production code, other than the image owner and the migrations, that could create or move
    an image selection: no system path may select an image on an operator's behalf."""
    offenders = []
    for where, source in sources:
        if where in SELECTION_OWNERS or "/migrations/" in where:
            continue
        for node in ast.walk(ast.parse(source)):
            text = node.value if isinstance(node, ast.Constant) else None
            if _named(node) in SELECTION_NAMES or (
                isinstance(text, str) and SELECTION_TABLES.search(text)
            ):
                offenders.append(f"{where}:{getattr(node, 'lineno', 0)}")
    return offenders


def source_truth_write_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    """PRODUCT code that could write a source asset, a source image reference or a revision:
    a source-asset writer, a model constructor, or an update/delete/insert of those models."""
    offenders = []
    for where, source in sources:
        if not where.startswith("app/products/"):
            continue
        for node in ast.walk(ast.parse(source)):
            if _named(node) in SOURCE_TRUTH_WRITERS:
                offenders.append(f"{where}:{getattr(node, 'lineno', 0)}")
            if not isinstance(node, ast.Call):
                continue
            callee = _named(node.func)
            if callee in SOURCE_TRUTH_MODELS:
                offenders.append(f"{where}:{node.lineno}")
            if (
                callee in {"update", "delete", "insert"}
                and node.args
                and _named(node.args[0]) in SOURCE_TRUTH_MODELS
            ):
                offenders.append(f"{where}:{node.lineno}")
    return offenders


def test_only_the_image_owner_creates_or_moves_a_selection() -> None:
    assert image_selection_writer_problems(_code()) == []


def test_the_image_selection_writer_detector_fires() -> None:
    sources = [
        ("app/products/materialization.py", "unit.record_operator_selection(item)\n"),
        ("app/products/other.py", "from app.products.image_models import ImageSelectionOutput\n"),
        ("app/other/raw.py", "SQL = 'INSERT INTO current_image_selection_moves VALUES (1)'\n"),
        ("app/products/images.py", "images.record_selection(item)\n"),
        ("app/db/migrations/versions/0099_x.py", "T = 'image_selection_outputs'\n"),
    ]
    assert image_selection_writer_problems(sources) == [
        "app/products/materialization.py:1",
        "app/products/other.py:1",
        "app/other/raw.py:1",
    ]


def test_product_code_never_writes_source_truth() -> None:
    assert source_truth_write_problems(_code()) == []


def test_the_source_truth_writer_detector_fires() -> None:
    sources = [
        ("app/products/images.py", "store = SourceAssetStore(path, db, decoder, clock)\n"),
        ("app/products/image_store.py", "session.add(SourceAsset(sha256=s))\n"),
        ("app/products/other.py", "session.execute(update(SourceAsset).values(width=1))\n"),
        ("app/products/fine.py", "row = session.get(SourceAsset, sha)\n"),
        ("app/collect/assets.py", "session.add(SourceAsset(sha256=s))\n"),
    ]
    assert source_truth_write_problems(sources) == [
        "app/products/images.py:1",
        "app/products/image_store.py:1",
        "app/products/other.py:1",
    ]
