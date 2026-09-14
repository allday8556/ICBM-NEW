"""Static guards for rules that must hold regardless of runtime behaviour.

The canonical-document checks assert *active* references — the current UI authority, the
accepted milestone chain, recorded decisions — rather than banning historical strings, because
revision-history and review documents legitimately keep older prototype names.
"""

import argparse
import ast
import hashlib
import re
from pathlib import Path

import pytest

from app import cli
from app.jobs.models import AttemptOutcome, JobState

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
UI_DIR = REPO_ROOT / "ui" / "web"
V29 = REPO_ROOT / "ui" / "prototypes" / "icbm_redesign_test_v29_final.html"
V29_SHA256 = "896ad87011b8615b8a6a9cd3e790ca04f52e908e4ff7b6a26ea4bf5372dfeb82"

UI_SOURCE_OF_TRUTH = DOCS / "UI_SOURCE_OF_TRUTH.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ROADMAP_MD = REPO_ROOT / "ROADMAP.md"
PROTOTYPE_README = REPO_ROOT / "ui" / "prototypes" / "README.md"
M0_ACCEPTANCE = DOCS / "acceptance" / "M0.md"
JOB_STATE_ADR = DOCS / "adr" / "0005-durable-job-state-and-attempt-history.md"
OWNERSHIP_ADR = DOCS / "adr" / "0006-single-data-directory-process-ownership.md"
README_MD = REPO_ROOT / "README.md"
PROTOTYPE_FILE = re.compile(r"icbm_redesign_test_\w+\.html")
PRODUCTION_ROOTS = [REPO_ROOT / "app", REPO_ROOT / "integrations"]

# Production modules that open the SQLite database themselves (ADR-0006 mutation-target
# invariant). Everything else reaches the database through the Container. A new direct opener
# fails the rules below until it is gated and listed here. scripts/ and tests/ are not production:
# the M0 acceptance script is deliberately independent evidence.
DATABASE_OPENERS = {
    "app/db/database.py": "engine factory",  # defines Database / create_sqlite_engine
    "app/container.py": "require_ownership",  # the application Database
    "app/db/migrations/env.py": "require_ownership",  # schema migrations
    "app/db/migrate.py": "read-only",  # `icbm db current` (mode=ro)
}
_OPENING_CALLS = {"Database", "create_sqlite_engine", "create_engine"}

SCREENS = [
    "dashboard",
    "collect",
    "db",
    "register",
    "orders",
    "inquiry",
    "soldout",
    "ai-insight",
    "analytics",
    "settings",
]


def _ui_sources() -> list[Path]:
    return [p for p in UI_DIR.rglob("*") if p.suffix in {".html", ".js", ".css"}]


def _read(path: Path) -> str:
    return path.read_text("utf-8")


def _section(text: str, heading: str) -> str:
    """Body of the first markdown section whose title matches ``heading``, up to the next
    heading of the same or a higher level. Lines inside code fences are never headings."""
    lines = text.splitlines()
    fenced = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else re.match(r"^(#+)\s+(.*)$", line)
        if match and re.search(heading, match.group(2)):
            level = len(match.group(1))
            body: list[str] = []
            inner_fence = False
            for following in lines[index + 1 :]:
                if following.startswith("```"):
                    inner_fence = not inner_fence
                elif not inner_fence:
                    next_heading = re.match(r"^(#+)\s", following)
                    if next_heading and len(next_heading.group(1)) <= level:
                        break
                body.append(following)
            return "\n".join(body)
    raise AssertionError(f"no section titled {heading!r}")


def _assert_in_order(text: str, items: list[str]) -> None:
    position = 0
    for item in items:
        found = text.find(item, position)
        assert found >= 0, f"{item!r} is missing or out of order"
        position = found + len(item)


def _canonical_prototype() -> tuple[str, str, int]:
    record = _section(_read(UI_SOURCE_OF_TRUTH), r"^Canonical visual source$")
    name = re.search(r"`ui/prototypes/(icbm_redesign_test_\w+\.html)`", record)
    sha = re.search(r"SHA-256:\s*`([0-9a-f]{64})`", record)
    size = re.search(r"Size:\s*`(\d+)`", record)
    assert name and sha and size, "UI_SOURCE_OF_TRUTH lost its canonical record"
    return name.group(1), sha.group(1), int(size.group(1))


# ---------------------------------------------------------------- UI source of truth


def test_v29_source_of_truth_is_byte_identical_to_the_approved_file() -> None:
    data = V29.read_bytes()
    assert len(data) == 323751
    assert hashlib.sha256(data).hexdigest() == V29_SHA256


def test_ui_source_of_truth_record_describes_the_prototype_file() -> None:
    name, sha, size = _canonical_prototype()
    assert name == V29.name
    data = (REPO_ROOT / "ui" / "prototypes" / name).read_bytes()
    assert (len(data), hashlib.sha256(data).hexdigest()) == (size, sha)


def test_prototype_readme_mirrors_the_canonical_record() -> None:
    name, sha, size = _canonical_prototype()
    readme = _read(PROTOTYPE_README)
    canonical = _section(readme, r"^Canonical prototype$")
    assert set(PROTOTYPE_FILE.findall(canonical)) == {name}
    assert sha in canonical
    assert f"`{size}`" in canonical
    assert "docs/UI_SOURCE_OF_TRUTH.md" in readme


def test_claude_md_takes_the_ui_source_from_the_record() -> None:
    text = _read(CLAUDE_MD)
    assert PROTOTYPE_FILE.findall(text) == [], "CLAUDE.md must not hard-code a prototype file"
    for heading in (r"UI source rule", r"Current milestone"):
        assert "docs/UI_SOURCE_OF_TRUTH.md" in _section(text, heading), heading


def test_roadmap_takes_the_ui_source_from_the_record() -> None:
    section = _section(_read(ROADMAP_MD), r"UI Source of Truth")
    assert "docs/UI_SOURCE_OF_TRUTH.md" in section
    assert PROTOTYPE_FILE.findall(section) == []


# ---------------------------------------------------------------- milestones and OPERATE


def test_roadmap_milestone_chain_is_the_accepted_sequence() -> None:
    _assert_in_order(
        _section(_read(ROADMAP_MD), r"Development sequence"),
        [
            "M0 Foundation",
            "M1 KM통상 CONNECT",
            "M2 SmartStore CONNECT",
            "M3 one-product COLLECT",
            "ProductFactsRevision",
            "M4 canonical Product DB",
            "M5 SmartStore REGISTER",
            "M6 OPERATE",
            "M6.5 fulfillment",
            "FIRST VERTICAL",
        ],
    )


def test_roadmap_places_fulfillment_inside_operate() -> None:
    roadmap = _read(ROADMAP_MD)
    operate = _section(roadmap, r"Phase 5 — OPERATE")
    assert re.search(r"^#+\s+.*Fulfillment \(inside OPERATE\)", operate, re.M)
    _assert_in_order(
        _section(roadmap, r"Fulfillment \(inside OPERATE\)"),
        [
            "Order",
            "Product/SKU",
            "SupplierOrder",
            "tracking",
            "marketplace shipment update",
            "delivery read-back",
        ],
    )
    top_level = re.findall(r"^#\s+(.*)$", roadmap, re.M)
    assert not [t for t in top_level if re.search(r"fulfil", t, re.I)], "fulfillment is not a phase"


# ---------------------------------------------------------------- recorded decisions


def test_m0_acceptance_is_recorded_as_accepted_with_rulings() -> None:
    text = _read(M0_ACCEPTANCE)
    header = text.split("\n## ", 1)[0]
    assert "Status: **ACCEPTED**" in header
    assert "Architect acceptance date: 2026-09-13" in header
    assert "5190491898" in header
    rulings = _section(text, r"Architect rulings")
    for question in ("Q1", "Q2", "Q3", "Q4"):
        assert f"**{question}**" in rulings, question


def test_job_state_adr_matches_the_code() -> None:
    adr = _read(JOB_STATE_ADR)
    assert "Status: **ACCEPTED**" in adr
    states = re.search(r"^(QUEUED(?: \| [A-Z_]+)+)$", adr, re.M)
    assert states, "ADR must list the job states"
    assert states.group(1).split(" | ") == [state.value for state in JobState]
    outcomes = re.search(r"outcome \(([A-Z_ |]+)\)", adr)
    assert outcomes, "ADR must list the attempt outcomes"
    assert outcomes.group(1).split(" | ") == [outcome.value for outcome in AttemptOutcome]


def _leaf_commands(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> set[tuple[str, ...]]:
    subparsers = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subparsers:
        return {prefix}
    found: set[tuple[str, ...]] = set()
    for action in subparsers:
        for name, child in action.choices.items():
            found |= _leaf_commands(child, (*prefix, name))
    return found


def test_every_cli_command_is_classified_for_data_dir_ownership() -> None:
    owning, read_only = set(cli.OWNING_COMMANDS), set(cli.READ_ONLY_COMMANDS)
    assert _leaf_commands(cli.build_parser()) == owning | read_only
    assert not owning & read_only


def test_ownership_adr_lists_exactly_the_read_only_commands() -> None:
    adr = _read(OWNERSHIP_ADR)
    assert "Status: **ACCEPTED**" in adr
    rows = [line for line in adr.splitlines() if line.startswith("| `icbm ")]
    listed = {
        tuple(match.group(1).split())
        for row in rows
        if "read-only" in row.split("|")[2] and (match := re.search(r"`icbm ([^`]+)`", row))
    }
    assert listed == set(cli.READ_ONLY_COMMANDS)


def _production_modules() -> dict[str, ast.Module]:
    return {
        path.relative_to(REPO_ROOT).as_posix(): ast.parse(path.read_text("utf-8"))
        for root in PRODUCTION_ROOTS
        if root.exists()
        for path in root.rglob("*.py")
    }


def _calls(node: ast.AST, name: str | None = None) -> list[ast.Call]:
    calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
    if name is None:
        return calls
    return [c for c in calls if isinstance(c.func, ast.Name | ast.Attribute) and _callee(c) == name]


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _opening_calls(tree: ast.Module) -> list[ast.Call]:
    def opens(call: ast.Call) -> bool:
        func = call.func
        if _callee(call) in _OPENING_CALLS:
            return True
        if isinstance(func, ast.Attribute) and func.attr == "connect":
            return isinstance(func.value, ast.Name) and func.value.id == "sqlite3"
        return isinstance(func, ast.Name) and func.id == "connect"

    return [call for call in _calls(tree) if opens(call)]


def _enclosing_function(tree: ast.Module, node: ast.AST) -> ast.AST | None:
    line = getattr(node, "lineno", 0)
    functions = [
        f
        for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef)
        and f.lineno <= line <= (f.end_lineno or f.lineno)
    ]
    return max(functions, key=lambda f: f.lineno, default=None)


def test_only_listed_production_modules_open_the_database() -> None:
    openers = {path for path, tree in _production_modules().items() if _opening_calls(tree)}
    assert openers == set(DATABASE_OPENERS)


def test_production_database_openers_are_gated() -> None:
    # production mutation target T + supplied/active lease L => L covers T before any side effect.
    modules = _production_modules()
    for path, gate in DATABASE_OPENERS.items():
        tree = modules[path]
        for call in _opening_calls(tree):
            where = f"{path}:{call.lineno}"
            function = _enclosing_function(tree, call)
            if gate == "engine factory":
                continue
            assert function is not None, f"{where} opens the database outside a function"
            if gate == "require_ownership":
                gates = [c.lineno for c in _calls(function, "require_ownership")]
                assert gates and min(gates) < call.lineno, f"{where} opens before require_ownership"
            else:
                texts = [
                    n.value
                    for n in ast.walk(function)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                ]
                assert any("mode=ro" in text for text in texts), f"{where} is not read-only"


def test_lease_coverage_is_decided_only_by_require_ownership() -> None:
    deciders = {path for path, tree in _production_modules().items() if _calls(tree, "covers")}
    assert deciders <= {"app/core/ownership.py"}


# ---------------------------------------------------------------- supplier CONNECT boundary

# Issue #7 comments 5653608622 §6/§9 and 5653615136: supplier-specific code is site knowledge
# only, raw clients live in the common transport, and payloads come from the allowlist builder.
SITE_KNOWLEDGE_IMPORTS = {"integrations.suppliers.base", "__future__", "re", "dataclasses", "enum"}
RAW_CLIENTS = {
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "http.client",
    "websockets",
    "playwright",
    "selenium",
    "socket",
}
# Production modules allowed a raw client, and why.
RAW_CLIENT_OWNERS = {
    "integrations/suppliers/transport/gateway.py": "the common policy-enforcing supplier transport",
    "app/core/egress.py": "resolves the granted hosts' addresses for the guard (socket)",
    "app/core/ownership.py": "hostname for the diagnostic owner metadata (socket)",
}
_COMMON_SUPPLIER_MODULES = {
    "integrations/suppliers/__init__.py",
    "integrations/suppliers/base.py",
    "integrations/suppliers/registry.py",
}


def _imported_modules(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_safe_payload(value: ast.expr | None) -> bool:
    return value is None or (isinstance(value, ast.Call) and _callee(value) == "safe_payload")


def test_supplier_packages_hold_site_knowledge_only() -> None:
    packages = {
        path: tree
        for path, tree in _production_modules().items()
        if path.startswith("integrations/suppliers/")
        and not path.startswith("integrations/suppliers/transport/")
        and path not in _COMMON_SUPPLIER_MODULES
    }
    assert "integrations/suppliers/kmretail/__init__.py" in packages
    for path, tree in packages.items():
        assert _imported_modules(tree) <= SITE_KNOWLEDGE_IMPORTS, path
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not attributes & {"_raw", "client", "page", "context", "request", "grant"}, path


def test_raw_network_and_browser_clients_live_only_in_the_common_transport() -> None:
    importers = {
        path
        for path, tree in _production_modules().items()
        if any(
            m == raw or m.startswith(f"{raw}.")
            for m in _imported_modules(tree)
            for raw in RAW_CLIENTS
        )
    }
    assert importers == set(RAW_CLIENT_OWNERS)


def test_only_the_common_transport_opens_egress_grants() -> None:
    openers = {path for path, tree in _production_modules().items() if _calls(tree, "grant")}
    assert openers == {"integrations/suppliers/transport/gateway.py"}


def test_supplier_logs_and_audit_payloads_come_from_the_allowlist() -> None:
    scoped = {
        path: tree
        for path, tree in _production_modules().items()
        if path.startswith(("app/connect/", "integrations/suppliers/"))
    }
    checked = 0
    for path, tree in scoped.items():
        for call in _calls(tree):
            where = f"{path}:{call.lineno}"
            func = call.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "logger"
            ):
                checked += 1
                # A constant message and an allowlisted ``extra`` — nothing else reaches a log.
                assert len(call.args) == 1 and isinstance(call.args[0], ast.Constant), where
                assert _is_safe_payload(_keyword(call, "extra")), where
            if _callee(call) == "AuditEntry":
                checked += 1
                for field in ("details", "before", "after"):
                    assert _is_safe_payload(_keyword(call, field)), f"{where} {field}"
    assert checked >= 5


def test_marketplace_capability_code_cannot_reach_a_provider() -> None:
    # M2 PR-B is provider-call zero: the capability owner imports no client, transport, egress
    # grant, integration or supplier session — only domain, persistence and audit building blocks.
    allowed = (
        "__future__",
        "collections.abc",
        "contextlib",
        "dataclasses",
        "datetime",
        "enum",
        "logging",
        "typing",
        "pydantic",
        "sqlalchemy",
        "app.connect.marketplace",
        "app.core.errors",
        "app.core.clock",
        "app.core.safe_payload",
        # A0 (PR-C): the keyed fingerprint and its key in the OS secret store — no network.
        "base64",
        "hashlib",
        "hmac",
        "os",
        "app.core.secrets",
        "app.audit",
        "app.db.base",
        "app.db.types",
        "app.db.database",
    )
    modules = {
        path: tree
        for path, tree in _production_modules().items()
        if path.startswith("app/connect/marketplace/")
    }
    assert "app/connect/marketplace/service.py" in modules
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            assert any(name == a or name.startswith(f"{a}.") for a in allowed), f"{path}: {name}"


def test_s17_19_only_the_operator_entry_point_records_contract_freshness() -> None:
    # CAPABILITY_MAPPING F8: contract freshness is contract-governance truth, not provider runtime
    # evidence. PR-B's local operator entry point is its only production recorder; an adapter
    # (PR-A) may consume the recorded value but must never set, infer, seed or fabricate CURRENT.
    modules = _production_modules()
    assert {p for p, t in modules.items() if _calls(t, "record_contract_freshness")} == {
        "app/api/routes/connect.py"
    }
    assert {p for p, t in modules.items() if _calls(t, "record_freshness")} == {
        "app/connect/marketplace/service.py"
    }


def test_s17_18_no_production_code_supplies_a_mapping_revision_or_application_identity() -> None:
    # M2 instructions §5.1/§6.7: A0 consumes the endpoint-mapping revision and the application
    # identity through seams, and PR-A supplies their one authoritative implementation. Until then
    # no production module may implement either — no hardcoded "v1"-style revision and no typed-in
    # identity; only test fixtures implement them. PR-A updates this rule with its registry.
    # The seams are class methods; a module-level function of the same name (the Alembic schema
    # revision in app/db/migrate.py) is not one.
    declared, implementers = set(), set()
    for path, tree in _production_modules().items():
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for node in cls.body:
                if not (
                    isinstance(node, ast.FunctionDef)
                    and node.name in {"current_revision", "current_identity"}
                ):
                    continue
                body = [
                    n
                    for n in node.body
                    if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
                ]
                (implementers if body else declared).add(f"{path}:{cls.name}.{node.name}")
    assert declared == {
        "app/connect/marketplace/revision.py:EndpointMappingRevisionProvider.current_revision",
        "app/connect/marketplace/sources.py:ApplicationIdentitySource.current_identity",
    }
    assert implementers == set()


def test_connect_adds_no_product_facts_or_product_schema() -> None:
    from app.db.metadata import metadata

    assert set(metadata.tables) == {
        "jobs",
        "job_attempts",
        "audit_events",
        "supplier_connections",
        "marketplace_capabilities",
        "marketplace_workflow_overlays",
        "marketplace_permission_attestations",
    }
    offenders = [
        path
        for path in _production_modules()
        if path.startswith(("app/connect/", "integrations/suppliers/"))
        and re.search(r"ProductFacts", (REPO_ROOT / path).read_text("utf-8"))
    ]
    assert offenders == []


def test_readme_does_not_present_m0_as_the_current_milestone() -> None:
    readme = _read(README_MD)
    assert not re.search(r"^#+\s*Current milestone:\s*M0", readme, re.M)
    status = _section(readme, r"^Status$")
    assert "**ACCEPTED**" in status
    assert "M1" in status


# ---------------------------------------------------------------- UI and runtime code


def test_ui_references_no_external_origin() -> None:
    offenders = [
        f"{p.relative_to(REPO_ROOT)}: {m.group(0)}"
        for p in _ui_sources()
        for m in re.finditer(r"(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", p.read_text("utf-8"), re.I)
    ]
    assert offenders == []


def test_ui_has_no_inline_styles_that_the_csp_would_block() -> None:
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in _ui_sources()
        if p.suffix != ".css"
        and re.search(r"style\s*=\s*[\"']|setAttribute\(\s*['\"]style", p.read_text("utf-8"))
    ]
    assert offenders == []


@pytest.mark.parametrize("screen", SCREENS)
def test_every_top_level_screen_has_a_page_module_bound_to_its_contract(screen: str) -> None:
    module = UI_DIR / "js" / "pages" / f"{screen}.js"
    assert module.is_file(), module
    assert f"/api/v1/screens/{screen}" in module.read_text("utf-8")


def test_no_runtime_code_references_the_legacy_project() -> None:
    # Runtime code only: the acceptance tooling names the legacy project to prove its absence.
    roots = [REPO_ROOT / "app", REPO_ROOT / "integrations", UI_DIR]
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for root in roots
        if root.exists()
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".js", ".html", ".css"}
        and re.search(r"ICBM-PROJECT|icbm_project", p.read_text("utf-8"), re.I)
    ]
    assert offenders == []
