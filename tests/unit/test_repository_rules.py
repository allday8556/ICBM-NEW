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
REVIEW_ADR = DOCS / "adr" / "0016-gate2-human-review-path-and-review-item-owner.md"
ADAPTIVE_ADR = DOCS / "adr" / "0017-adaptive-collector-profile-extraction-and-shadow-validation.md"
ADAPTIVE_PROPOSAL = DOCS / "review" / "ADAPTIVE-COLLECTOR-PROPOSAL-BY-CLAUDE.md"
ARCHITECTURE_MD = DOCS / "ARCHITECTURE.md"
# The owners whose truth a ReviewItem indexes; none of them may read the review owner (G2-02).
REVIEWED_OWNERS = (
    "app/collect/",
    "app/products/",
    "app/register/",
    "app/connect/",
    "integrations/",
)
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


# Issue #52 §0 and PR #55 review 5204359614: the milestone status is stated in CLAUDE.md §11,
# ROADMAP.md §12, the README status and the runtime ``app.MILESTONE`` (health, screen meta, UI
# footer), and every accepted milestone has an acceptance record. They must agree, so the active
# milestone cannot silently drift again.
_CLAUDE_MILESTONE = re.compile(r"^(M\d+(?:\.\d+)?) — .*?\b(ACCEPTED|CURRENT)\b")
_ROADMAP_MILESTONE = re.compile(r"^(?:→\s*)?(M\d+(?:\.\d+)?) .*?\s(ACCEPTED|CURRENT)\b")
_ROADMAP_CHAIN = re.compile(r"^(?:→\s*)?(M\d+(?:\.\d+)?)\s", re.M)
_README_ACCEPTED = re.compile(r"^- \*\*(M\d+(?:\.\d+)?)\*\* — .*\*\*(ACCEPTED)\*\*")
_README_CURRENT = re.compile(r"^- \*\*Current milestone:\*\* (M\d+(?:\.\d+)?) — ")


def _milestone_statuses(text: str, *patterns: re.Pattern[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        for pattern in patterns:
            match = pattern.match(line.strip())
            if match:
                milestone = match.group(1)
                assert milestone not in found, f"{milestone} is listed twice"
                found[milestone] = match.group(2) if pattern.groups > 1 else "CURRENT"
    return found


def _roadmap_sequence() -> str:
    return _section(_read(ROADMAP_MD), r"Development sequence")


def test_active_milestone_agrees_across_the_canonical_status_documents() -> None:
    claude = _milestone_statuses(
        _section(_read(CLAUDE_MD), r"Current milestone"), _CLAUDE_MILESTONE
    )
    roadmap = _milestone_statuses(_roadmap_sequence(), _ROADMAP_MILESTONE)
    readme = _milestone_statuses(
        _section(_read(README_MD), r"^Status$"), _README_ACCEPTED, _README_CURRENT
    )
    assert claude == roadmap == readme
    current = [milestone for milestone, status in roadmap.items() if status == "CURRENT"]
    assert len(current) == 1, current
    from app import MILESTONE

    assert current == [MILESTONE], "runtime app.MILESTONE is not the canonical CURRENT milestone"
    # Every milestone before the current one is accepted; none after it carries a status.
    chain = _ROADMAP_CHAIN.findall(_roadmap_sequence())
    position = chain.index(current[0])
    assert [roadmap.get(m) for m in chain[:position]] == ["ACCEPTED"] * position
    assert not [m for m in chain[position + 1 :] if m in roadmap]


def test_accepted_milestones_have_an_accepted_acceptance_record() -> None:
    statuses = _milestone_statuses(_roadmap_sequence(), _ROADMAP_MILESTONE)
    for milestone, status in statuses.items():
        record = DOCS / "acceptance" / f"{milestone}.md"
        header = _read(record).split("\n## ", 1)[0] if record.is_file() else ""
        assert ("Status: **ACCEPTED**" in header) == (status == "ACCEPTED"), milestone


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


def test_the_review_item_contract_is_recorded_and_pinned() -> None:
    """ADR-0016 (Gate 2 G2-0): the ReviewItem owner contract, before any schema."""
    from app.review.service import ReviewKind

    adr = _read(REVIEW_ADR)
    assert re.search(r"^Status: \*\*ACCEPTED\*\*", adr, re.M)
    assert "5804605624" in adr
    for canonical in (ARCHITECTURE_MD, ROADMAP_MD):
        assert REVIEW_ADR.name in _read(canonical), canonical.name
    block = adr.split("\n## Invariants", 1)[1].split("```text", 1)[1].split("```", 1)[0]
    invariants = dict(re.findall(r"^(G2-\d\d)\s+(.*\S)\s*$", block, re.M))
    assert list(invariants) == [f"G2-{n:02d}" for n in range(1, 20)]
    # The kinds are closed, and the contract names exactly the ones the code holds.
    assert invariants["G2-03"].endswith(", ".join(kind.value for kind in ReviewKind))
    # The decisions the kickoff asked the contract to pick, pinned by their wording.
    assert "supersedes the old item" in invariants["G2-07"]
    assert "leaves the item OPEN while the owner still derives" in invariants["G2-10"]
    assert "never reported as zero" in invariants["G2-14"]
    assert "COMPLIANCE never implements ComplianceGate" in invariants["G2-13"]
    # Recovery never waits for an event (review 5805095154): startup and periodic full passes,
    # coverage that fails closed, and the crash/restart proof every producer slice must carry.
    assert (
        "full reconciliation at process startup and a bounded periodic full reconciliation"
        in invariants["G2-09"]
    )
    assert "never only by the next event for that scope" in invariants["G2-09"]
    assert "authoritative only after a successful full reconciliation" in invariants["G2-14"]
    assert "no known indexing failure unrecovered" in invariants["G2-14"]
    assert "exactly once by the startup full reconciliation after a restart" in invariants["G2-19"]
    assert "periodic full reconciliation in a running process" in invariants["G2-19"]
    recovery = _section(adr, r"^4\. Lifecycle$")
    for required in (
        "**at application process startup**",
        "**periodically while the process runs**",
        "restarts with **no new owner write**",
        "recreates the missing `OPEN` item **exactly once**",
    ):
        assert required in recovery, required
    assert "never a fake `0`" in _section(adr, r"^7\. Counts")


def test_the_adaptive_collector_contract_is_recorded_and_pinned() -> None:
    """ADR-0017 (Issue #110): the Adaptive Collector contract, before any implementation."""
    from app.collect.facts import EvidenceKind, FieldLevel, FieldStatus

    adr = _read(ADAPTIVE_ADR)
    assert re.search(r"^Status: \*\*ACCEPTED\*\*", adr, re.M)
    for source in ("5302952567", "5812200650", "5812422770"):
        assert source in adr, source
    for canonical in (ARCHITECTURE_MD, ROADMAP_MD, DOCS / "GLOSSARY.md"):
        assert ADAPTIVE_ADR.name in _read(canonical) or "ADR-0017" in _read(canonical), canonical
    # The amended contracts point at their amendment; nothing amends them silently.
    for amended in ("0010-supplier-generic-collect", "0013-m4-canonical-product-contract"):
        (path,) = (DOCS / "adr").glob(f"{amended}*.md")
        assert "Amendment note (ADR-0017" in _read(path), path.name
    proposal = _read(ADAPTIVE_PROPOSAL)
    assert re.search(r"^Status: \*\*ACCEPTED\*\*", proposal, re.M)
    assert ADAPTIVE_ADR.name in proposal
    block = adr.split("\n## Invariants", 1)[1].split("```text", 1)[1].split("```", 1)[0]
    invariants = dict(re.findall(r"^(AC-\d\d)\s+(.*\S)\s*$", block, re.M))
    assert list(invariants) == [f"AC-{n:02d}" for n in range(1, 25)]
    # The truth vocabulary the design must not widen is exactly what the code holds.
    assert {s.value for s in FieldStatus} == {"CONFIRMED", "ABSENT", "REVIEW_REQUIRED"}
    assert {level.value for level in FieldLevel} == {"CORE", "COVERAGE"}
    assert len(EvidenceKind) == 8
    assert "non-CORE fields are COVERAGE" in invariants["AC-21"]
    # The rulings the audits fixed, pinned by their wording.
    assert "No existing ProductFactsRevision is backfilled" in invariants["AC-06"]
    assert "never by itself EXTRACTOR_CHANGED" in invariants["AC-07"]
    assert "(hook_point, target) bindings" in invariants["AC-10"]
    assert "never by the EPR or PTR it validates" in invariants["AC-11"]
    assert "IMAGES_FIELD" in invariants["AC-12"]
    assert "writes only a lifecycle transition" in invariants["AC-13"]
    assert "never nested" in invariants["AC-16"]
    assert "UNMATCHABLE is never a success" in invariants["AC-17"]
    assert "a crash included, counts INCOMPLETE and is never excluded" in invariants["AC-18"]
    assert "with no hold exception" in invariants["AC-20"]
    assert "frozen at a run's first product-read reservation" in invariants["AC-23"]
    assert "revision_id is nullable and absent for a NO_REVISION run" in invariants["AC-24"]
    # ADR-0010 §7's historical level label is aligned with COVERAGE, not left as a second name.
    (collect_adr,) = (DOCS / "adr").glob("0010-supplier-generic-collect*.md")
    levels = _section(_read(collect_adr), r"^7\. Facts: two levels")
    assert "Amendment note (ADR-0017 §4)" in levels and "`COVERAGE`" in levels
    # The six cross-audit items of Issue #110 5812200650 are each closed by a named section.
    closed = _section(adr, r"^14\. The cross-audit items, closed$")
    assert len(re.findall(r"^\| [1-6] \|", closed, re.M)) == 6
    assert closed.count("**yes**") == 2


def test_no_reviewed_owner_reads_the_review_owner() -> None:
    """ADR-0016 G2-02: the review owner reads owners, never the reverse."""
    offenders = []
    for module, tree in _production_modules().items():
        if not module.startswith(REVIEWED_OWNERS):
            continue
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            offenders += [f"{module}: {n}" for n in names if n.split(".")[:2] == ["app", "review"]]
    assert offenders == []


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


# ---------------------------------------------------------------- the connection owner

# Issue #52 comments 5687814715 and 5688150031: ICBM-NEW decides its own data root in one place,
# and CONNECT alone owns the logins, the connection state and the sessions. No other code may
# redefine where that owner lives, or stand up a second one.
DATA_ROOT_RESOLVER = "app/config.py"
_CODE_ROOTS = (REPO_ROOT / "app", REPO_ROOT / "integrations", REPO_ROOT / "scripts")
# The root's inputs (comment 5688854287): read by the resolver and nothing else.
_DATA_ROOT_INPUTS = re.compile(
    r"USERPROFILE|HOME_ENV|DATA_DIR_ENV"
    r"|(?:\.get|getenv)\(\s*[\"']ICBM_DATA_DIR|\[\s*[\"']ICBM_DATA_DIR[\"']\s*\]"
)
# Retired roots and home lookups that may never own ICBM-NEW's data again, anywhere in code:
# the AppData/XDG roots (host-dependent under MSIX), the repo-local var/ default, the old lock.
_RETIRED_ROOTS = re.compile(
    r"LOCALAPPDATA|XDG_DATA_HOME|DEFAULT_DATA_DIR|icbm-owner\.lock|\.home\(\)|expanduser\("
)
_RESOLVER_FUNCTIONS = {"default_data_dir", "configured_data_dir"}
M3_HARNESS = REPO_ROOT / "scripts" / "m3harness"
# Where the M3 harness may make each of these calls: (module, enclosing function).
M3_OWNER_CALLS = {
    # The REAL owner is the canonical one; the only configuration built here is the DRY stand-in.
    "AppConfig": {("cli.py", "_dry_owner_config")},
    "ConnectionOwner": {("cli.py", "rehearse")},
    "build_container": {("cli.py", "_owner_container")},
    # The campaign-scoped store holds the capture key only.
    "KeyringSecretStore": {("cli.py", "capture_key_store")},
    # Never: a second session store, a second CONNECT service, or a login prompt.
    "SupplierSessionStore": set(),
    "ConnectService": set(),
    "getpass": set(),
}


def _code_files() -> dict[str, str]:
    return {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text("utf-8")
        for root in _CODE_ROOTS
        for path in root.rglob("*.py")
    }


def test_only_the_config_module_resolves_the_data_root() -> None:
    files = _code_files()
    readers = {path for path, text in files.items() if _DATA_ROOT_INPUTS.search(text)}
    assert readers == {DATA_ROOT_RESOLVER}
    retired = {path for path, text in files.items() if _RETIRED_ROOTS.search(text)}
    assert retired == set(), "a retired data root or home lookup is back"
    definers = {
        path
        for path, text in files.items()
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.FunctionDef) and node.name in _RESOLVER_FUNCTIONS
    }
    assert definers == {DATA_ROOT_RESOLVER}


def test_icbm_and_the_m3_harness_take_the_owner_from_the_same_resolver() -> None:
    icbm = ast.parse((REPO_ROOT / "app" / "cli.py").read_text("utf-8"))
    assert _calls(icbm, "from_env"), "icbm reads its configuration through AppConfig.from_env"
    harness = ast.parse((M3_HARNESS / "cli.py").read_text("utf-8"))
    operator = next(
        f for f in ast.walk(harness) if isinstance(f, ast.FunctionDef) and f.name == "operator"
    )
    from_env = _calls(operator, "from_env")
    assert from_env and all(not c.args and not c.keywords for c in from_env), (
        "the REAL owner is AppConfig.from_env() with no override"
    )


def test_the_m3_harness_never_defines_a_connection_owner_of_its_own() -> None:
    for path in sorted(M3_HARNESS.glob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        assert "getpass" not in _imported_modules(tree), f"{path.name} prompts for a login"
        for call in _calls(tree):
            callee = _callee(call)
            function = _enclosing_function(tree, call)
            where = (path.name, getattr(function, "name", "<module>"))
            if callee in M3_OWNER_CALLS:
                assert where in M3_OWNER_CALLS[callee], f"{path.name}:{call.lineno} {callee}"
            receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
            writes_login = (
                callee == "save"
                and isinstance(receiver, ast.Call)
                and _callee(receiver) == "SupplierCredentialStore"
            )
            assert not writes_login, f"{path.name}:{call.lineno} writes a supplier login"


def test_the_docs_name_the_resolver_and_the_canonical_root() -> None:
    for path in (README_MD, DOCS / "ARCHITECTURE.md"):
        text = _read(path)
        assert "app/config.py" in text and r"%USERPROFILE%\ICBM-NEW\data" in text, path.name
        assert "runtime/owner.lock" in text, path.name
        for retired in (r"%LOCALAPPDATA%\ICBM-NEW", "XDG_DATA_HOME", r"var\icbm.db", "var/icbm.db"):
            assert retired not in text, (path.name, retired)


# PR #64 review 5217542767 §3: the local scan reads cookie material, never a session.
SCAN_BOUNDARY = "session_cookies_for_scan"
SCAN_BOUNDARY_CALLERS = {"app/connect/service.py", "scripts/m3harness/cli.py"}


def test_the_local_scan_boundary_is_not_a_session_transport() -> None:
    files = _code_files()
    service = files["app/connect/service.py"]
    assert "def stored_session" not in service, "no generic stored-session accessor"
    assert {path for path, text in files.items() if SCAN_BOUNDARY in text} == SCAN_BOUNDARY_CALLERS
    boundary = next(
        node
        for node in ast.walk(ast.parse(service))
        if isinstance(node, ast.FunctionDef) and node.name == SCAN_BOUNDARY
    )
    forbidden = {"verify", "_verify", "_prove", "_authenticate", "collection_session", "fetch"}
    assert not [call for call in _calls(boundary) if _callee(call) in forbidden], "reads only"
    # Comment 5690832285 §2: no sibling path may hand out payload material instead.
    lenient = {"repr", "str", "format", "hex"}
    assert not [call for call in _calls(boundary) if _callee(call) in lenient], "no coercion"
    assert not [
        call
        for call in _calls(boundary, "decode")
        if call.args or any(word.arg == "errors" for word in call.keywords)
    ], "no replacement decoding"
    handlers = [node for node in ast.walk(boundary) if isinstance(node, ast.ExceptHandler)]
    assert handlers, "the decode failure is handled"
    assert not [
        node for handler in handlers for node in ast.walk(handler) if isinstance(node, ast.Return)
    ], "no fallback return"
    assert [node for node in ast.walk(boundary) if isinstance(node, ast.Raise)], "it fails closed"
    returned = {
        _callee(node.value)
        for node in ast.walk(boundary)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Call)
    }
    assert "load" not in returned, "the session payload itself never leaves the owner"


# ---------------------------------------------------------------- supplier CONNECT boundary

# Issue #7 comments 5653608622 §6/§9 and 5653615136: supplier-specific code is site knowledge
# only, raw clients live in the common transport, and payloads come from the allowlist builder.
SITE_KNOWLEDGE_IMPORTS = {
    "integrations.suppliers.base",
    # ADR-0010 §3: the COLLECT port's data types (profile, document view), no transport.
    "integrations.suppliers.collection",
    "__future__",
    "re",
    "dataclasses",
    "enum",
    # A collection parser reads text it was handed. These open nothing: html.parser is a pure
    # tokenizer and urllib.parse is string arithmetic — urllib.request, the one that opens a
    # connection, stays a raw client below and is never allowed here.
    "html.parser",
    "urllib.parse",
    "collections.abc",
    "typing",
    # ADR-0010 §7–§8: the COLLECT domain's fact vocabulary — frozen values, evidence and statuses.
    # It is data, not machinery: a parser needs it to say what it read, and it carries no
    # database, job, asset store, transport or egress handle of any kind.
    "app.collect.facts",
}
# Issue #52 ruling 5702780630 P2: a supplier parser turns immutable documents into facts. It never
# executes, retries, hashes, stores, persists or schedules anything — those stay in COLLECT core.
PARSER_MAY_NOT_OWN = {
    "app.collect.assets",
    # Stage-B2 (ruling 5706133893): the run, its durable result and the job stay in COLLECT core.
    "app.collect.collection",
    "app.collect.runs",
    "app.jobs.registry",
    "app.collect.revisions",
    "app.collect.sourceassets",
    "app.collect.readback",
    "app.core.egress",
    "app.db.database",
    "app.jobs.service",
    "integrations.suppliers.transport.collection",
    "integrations.suppliers.transport.gateway",
    "hashlib",
    "sqlite3",
}
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
    # ADR-0010 §3: the common COLLECT gateway, the only code that performs a collection request.
    "integrations/suppliers/transport/collection.py": "the common collection gateway",
    # ENDPOINT_MATRIX §13: the one registry-gated SmartStore endpoint caller (M2 PR-A).
    "integrations/marketplaces/smartstore/caller.py": "the registry-gated SmartStore caller",
    # Opens nothing: socket.gaierror is the evidence that separates a DNS failure (ERRORS §15.1).
    "integrations/marketplaces/smartstore/transmission.py": "DNS-phase evidence type (socket)",
    "app/core/egress.py": "resolves the granted hosts' addresses for the guard (socket)",
    "app/core/ownership.py": "hostname for the diagnostic owner metadata (socket)",
}
_COMMON_SUPPLIER_MODULES = {
    "integrations/suppliers/__init__.py",
    "integrations/suppliers/base.py",
    "integrations/suppliers/collection.py",
    "integrations/suppliers/extraction.py",
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
    assert "integrations/suppliers/kmretail/collect/images.py" in packages
    for path, tree in packages.items():
        # A package may import its own modules: site knowledge is allowed more than one file.
        own = "integrations.suppliers." + path.split("/")[2]
        outside = {
            module
            for module in _imported_modules(tree)
            if module != own and not module.startswith(own + ".")
        }
        assert outside <= SITE_KNOWLEDGE_IMPORTS, path
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not attributes & {"_raw", "client", "page", "context", "request", "grant"}, path


def test_only_the_collect_transport_judges_a_fetch_target_and_it_always_names_why() -> None:
    # Issue #52 ruling 5716978033 §3–§4: one judge of fetchability, and a refusal from a closed
    # vocabulary. A target refusal is built only in the COLLECT transport, always with a
    # ``FetchTargetRefusal`` member, never a free-form string; and nothing outside the transport
    # keeps a URL-shape check of its own that could disagree with it.
    judge = "integrations/suppliers/transport/collection.py"
    builders = {}
    for path, tree in _production_modules().items():
        calls = _calls(tree, "CollectionTargetRefused") + _calls(tree, "_refused")
        if calls:
            builders[path] = calls
    assert set(builders) == {judge}
    for call in builders[judge]:
        if _callee(call) == "CollectionTargetRefused":
            reason = call.args[0]
            assert isinstance(reason, ast.Name) and reason.id == "reason", ast.dump(call)
            continue
        reason = call.args[0]
        assert (
            isinstance(reason, ast.Attribute)
            and isinstance(reason.value, ast.Name)
            and reason.value.id == "FetchTargetRefusal"
        ), f"{judge}:{call.lineno}"
    shape_checks = {
        path
        for path, tree in _production_modules().items()
        if path.startswith(("app/collect/", "integrations/suppliers/"))
        and not path.startswith("integrations/suppliers/transport/")
        and path != "app/collect/urls.py"  # the persisted-URL sanitizer, not a fetch decision
        and any(
            isinstance(node, ast.Attribute) and node.attr in {"port", "username", "password"}
            for node in ast.walk(tree)
        )
    }
    assert shape_checks == set(), "a fetch-target decision lives in the transport only"


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
    # The supplier transport for supplier profile hosts (M1) and the SmartStore caller for the
    # SmartStore provider host (M2 PR-A). No other code can open a grant.
    openers = {path for path, tree in _production_modules().items() if _calls(tree, "grant")}
    assert openers == {
        "integrations/suppliers/transport/gateway.py",
        # ADR-0010 §3: the COLLECT gateway, for exactly its collection profile's hosts.
        "integrations/suppliers/transport/collection.py",
        "integrations/marketplaces/smartstore/caller.py",
    }


def test_every_collection_definition_pins_its_extraction_identity() -> None:
    # ADR-0010 §12: a supplier collect package has an acyclic, complete and current pin.
    from integrations.suppliers.extraction import manifest_problems

    suppliers = REPO_ROOT / "integrations" / "suppliers"
    packages = [
        path
        for path in suppliers.iterdir()
        if path.is_dir() and path.name not in {"transport", "__pycache__"}
    ]
    assert suppliers / "kmretail" in packages
    problems = {package.name: manifest_problems(REPO_ROOT, package) for package in packages}
    assert {name: found for name, found in problems.items() if found} == {}


def test_image_role_rules_are_bound_to_the_extraction_identity() -> None:
    # Issue #52 comment 5696242775 §4: there is one set of role rules, and a semantic change to
    # what they mean advances the same identity the future parser will be pinned to.
    from integrations.suppliers.extraction import read_manifest
    from integrations.suppliers.kmretail import IMAGE_ROLES
    from integrations.suppliers.kmretail.collect.images import ROLE_RULES, ROLE_RULES_REVISION

    manifest = read_manifest(
        REPO_ROOT / "integrations" / "suppliers" / "kmretail" / "extraction_identity.py"
    )
    assert IMAGE_ROLES.identity == ROLE_RULES_REVISION == manifest.revision
    assert len({rule.rule_id for rule in ROLE_RULES}) == len(ROLE_RULES), "rule ids are distinct"


def test_a_supplier_parser_owns_nothing_but_reading() -> None:
    # The KM collect package may read a document and say what it found. Requesting, retrying,
    # hashing, storing, persisting and scheduling belong to generic COLLECT core, and a module
    # that cannot import them cannot quietly take them over.
    package = {
        path: tree
        for path, tree in _production_modules().items()
        if path.startswith("integrations/suppliers/kmretail/")
    }
    assert "integrations/suppliers/kmretail/collect/facts.py" in package
    assert "integrations/suppliers/kmretail/collect/identity.py" in package
    for path, tree in package.items():
        imported = _imported_modules(tree)
        assert not imported & PARSER_MAY_NOT_OWN, path
        assert not imported & RAW_CLIENTS, path
        called = {_callee(call) for call in _calls(tree)}
        # The verbs of ownership: storing an asset, installing egress, scheduling work,
        # hashing bytes. Building a list is not one of them.
        assert not called & {"put", "install", "enqueue", "sha256", "acquire"}, path
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not attributes & {"EGRESS", "grant", "db", "jobs", "assets", "revisions"}, path


def test_a_collection_reads_exactly_one_product_page() -> None:
    # Issue #52 ruling 5706133893: one operator URL, one product document. No listing, no
    # pagination, no related product. The budget is the structural guarantee, so every budget a
    # production collection builds allows exactly one product read.
    budgets = [
        call
        for _, tree in _production_modules().items()
        for call in _calls(tree)
        if _callee(call) == "RunBudget"
    ]
    assert budgets, "the collection budget must exist"
    for call in budgets:
        reads = {
            keyword.arg: keyword.value
            for keyword in call.keywords
            if keyword.arg == "max_product_reads"
        }
        value = reads.get("max_product_reads")
        assert isinstance(value, ast.Constant) and value.value == 1, ast.dump(call)


def test_the_collection_job_belongs_to_collect_core() -> None:
    # The durable job is generic: a supplier contributes site knowledge, never a job type.
    from app.collect.collection import COLLECT_PRODUCT_JOB

    assert COLLECT_PRODUCT_JOB.startswith("collect.")
    offenders = [
        path
        for path, tree in _production_modules().items()
        if path.startswith("integrations/")
        and any(_callee(call) == "JobDefinition" for call in _calls(tree))
    ]
    assert offenders == [], "a supplier never defines a job"


def test_one_extraction_identity_covers_the_whole_collect_package() -> None:
    # Issue #52 ruling 5702780630: image roles, the identity rule and the facts parser are read
    # from one document and accepted together, so they share one revision.
    from integrations.suppliers.extraction import read_manifest
    from integrations.suppliers.kmretail import IMAGE_ROLES
    from integrations.suppliers.kmretail.collect.revision import EXTRACTION_REVISION

    package = REPO_ROOT / "integrations" / "suppliers" / "kmretail"
    manifest = read_manifest(package / "extraction_identity.py")
    assert EXTRACTION_REVISION == manifest.revision == IMAGE_ROLES.identity
    assert manifest.revision != "kmretail-images-1", "the contract expanded beyond image roles"
    modules = {
        path.relative_to(REPO_ROOT).as_posix() for path in (package / "collect").rglob("*.py")
    }
    assert modules == set(manifest.inputs), "every collect module is part of the identity"


def test_supplier_logs_and_audit_payloads_come_from_the_allowlist() -> None:
    scoped = {
        path: tree
        for path, tree in _production_modules().items()
        if path.startswith(
            ("app/connect/", "integrations/suppliers/", "integrations/marketplaces/")
        )
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


# ---------------------------------------------------------------- COLLECT source-truth boundary

# ADR-0010 §13 (Issue #52 §10): COLLECT and ProductFactsRevision work with every AI capability
# unavailable, and the M3 path makes no marketplace call. The source-truth path therefore imports
# no AI-provider, OCR or vision library, no ``ai`` package and no marketplace code.
SOURCE_TRUTH_ROOTS = ("app/collect/", "integrations/suppliers/")
SOURCE_TRUTH_FORBIDDEN = (
    "anthropic",
    "openai",
    "google.generativeai",
    "google.genai",
    "vertexai",
    "cohere",
    "mistralai",
    "groq",
    "ollama",
    "litellm",
    "langchain",
    "llama_index",
    "transformers",
    "torch",
    "tensorflow",
    "onnxruntime",
    "pytesseract",
    "tesserocr",
    "easyocr",
    "paddleocr",
    "cv2",
    "azure.ai",
    "azure.cognitiveservices",
    "app.ai",
    "integrations.ai",
    "integrations.marketplaces",
    "app.connect.marketplace",
    "app.connect.smartstore",
)


def test_collect_source_truth_path_imports_no_ai_ocr_or_marketplace_code() -> None:
    modules = {p: t for p, t in _production_modules().items() if p.startswith(SOURCE_TRUTH_ROOTS)}
    assert {"app/collect/service.py", "integrations/suppliers/kmretail/__init__.py"} <= set(modules)
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            forbidden = [f for f in SOURCE_TRUTH_FORBIDDEN if name == f or name.startswith(f"{f}.")]
            assert not forbidden, f"{path}: {name}"


# Issue #52 ruling 5711123764 §1: the REAL acceptance harness orchestrates and never collects.
CAMPAIGN_MAY_NOT_IMPORT = (
    "integrations.suppliers.kmretail.collect",
    "app.collect.assets",
    "app.collect.sourceassets",
    "app.collect.revisions",
    "app.collect.imagedecode",
)
# What a campaign may not reach on the composed application, and what it may not call.
CAMPAIGN_MAY_NOT_TOUCH = {"revisions", "source_asset_recorder", "source_assets"}
# (The identity rule itself, `resolve`, is reachable only through the parser package, which the
# import rule above already refuses; `Path.resolve` is not it.)
CAMPAIGN_MAY_NOT_CALL = {"classify", "classify_images", "parse_fields", "record"}


def test_the_acceptance_harness_parses_hashes_and_appends_nothing() -> None:
    # A campaign reaches the product only through the production ProductCollectionService: it
    # does not parse a supplier page, classify an image, store an asset or append a revision.
    files = [
        *sorted((REPO_ROOT / "scripts" / "m3accept").rglob("*.py")),
        REPO_ROOT / "scripts" / "m3_accept.py",
    ]
    assert any(f.name == "campaign.py" for f in files)
    for path in files:
        tree = ast.parse(path.read_text("utf-8"))
        for name in _imported_modules(tree):
            assert not any(
                name == f or name.startswith(f"{f}.") for f in CAMPAIGN_MAY_NOT_IMPORT
            ), f"{path.name}: {name}"
        reached = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"container", "built"}
        }
        assert not reached & CAMPAIGN_MAY_NOT_TOUCH, f"{path.name}: {reached}"
        called = {_callee(call) for call in _calls(tree)}
        assert not called & CAMPAIGN_MAY_NOT_CALL, f"{path.name}: {called & CAMPAIGN_MAY_NOT_CALL}"


def test_the_campaign_hard_zero_list_is_the_source_truth_list() -> None:
    from scripts.m3accept.prep import HARD_ZERO_MODULES

    assert HARD_ZERO_MODULES == SOURCE_TRUTH_FORBIDDEN


def test_connect_gateway_port_is_not_widened_for_collect() -> None:
    # ADR-0010 §3 (Issue #52 §2): COLLECT gets its own port. The CONNECT gateway keeps exactly the
    # protected-read proof and the login, and ``fetch`` takes no URL, path or target, so it can
    # never become an arbitrary-URL escape hatch. The Protocol and its implementation agree.
    expected = {
        "fetch": (["self", "definition"], ["kind", "session"]),
        "login": (["self", "definition", "credentials"], []),
    }
    modules = _production_modules()
    for path, class_name in (
        ("integrations/suppliers/base.py", "SupplierGateway"),
        ("integrations/suppliers/transport/gateway.py", "PolicedSupplierGateway"),
    ):
        cls = next(
            n
            for n in ast.walk(modules[path])
            if isinstance(n, ast.ClassDef) and n.name == class_name
        )
        public = {
            n.name: n
            for n in cls.body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and not n.name.startswith("_")
        }
        assert set(public) == set(expected), f"{class_name}: {sorted(public)}"
        for name, (positional, keyword_only) in expected.items():
            args = public[name].args
            where = f"{class_name}.{name}"
            assert [a.arg for a in args.posonlyargs + args.args] == positional, where
            assert [a.arg for a in args.kwonlyargs] == keyword_only, where
            assert args.vararg is None and args.kwarg is None, where


# ---------------------------------------------------------------- SmartStore endpoint boundary

# ENDPOINT_MATRIX §13 #3/#11/#12: the registry is the only source of the SmartStore host, base
# URL and paths, and the caller is the only code that composes a URL from them.
SMARTSTORE_REGISTRY = "integrations/marketplaces/smartstore/registry.py"
SMARTSTORE_CALLER = "integrations/marketplaces/smartstore/caller.py"
_SMARTSTORE_WIRE = re.compile(r"commerce\.naver\.com|/external\b|^/v\d+/|/oauth2/|/seller/", re.I)


def _code_strings(tree: ast.Module) -> list[ast.Constant]:
    """String literals that are code, not documentation: standalone string statements
    (docstrings) are skipped; f-string fragments are included."""
    statements = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in statements
    ]


def test_smartstore_wire_literals_live_only_in_the_endpoint_registry() -> None:
    holders = {
        path
        for path, tree in _production_modules().items()
        if any(_SMARTSTORE_WIRE.search(node.value) for node in _code_strings(tree))
    }
    assert holders == {SMARTSTORE_REGISTRY}


def test_only_the_caller_composes_a_smartstore_url() -> None:
    wire_names = {"BASE_URL", "PROVIDER_HOST"}
    users = set()
    for path, tree in _production_modules().items():
        for node in ast.walk(tree):
            named = (
                (isinstance(node, ast.Name) and node.id in wire_names)
                or (isinstance(node, ast.Attribute) and node.attr in wire_names)
                or (isinstance(node, ast.alias) and node.name in wire_names)
            )
            if named:
                users.add(path)
    assert users == {SMARTSTORE_REGISTRY, SMARTSTORE_CALLER}


def test_no_production_client_follows_redirects() -> None:
    # ENDPOINT_MATRIX §11, ERRORS §17.3: redirect following is off everywhere, explicitly.
    settings = [
        (path, call.lineno, _keyword(call, "follow_redirects"))
        for path, tree in _production_modules().items()
        for call in _calls(tree)
        if _keyword(call, "follow_redirects") is not None
    ]
    assert {path for path, _, _ in settings} >= {SMARTSTORE_CALLER}
    for path, line, value in settings:
        assert isinstance(value, ast.Constant) and value.value is False, f"{path}:{line}"


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


def test_s17_18_only_pr_a_supplies_the_mapping_revision_and_the_application_identity() -> None:
    # M2 instructions §5.1/§5.2/§6.7: A0 consumes the endpoint-mapping revision and the
    # application identity through seams, and PR-A supplies their one authoritative
    # implementation each: the endpoint registry's revision and the committed SmartStore
    # credential bundle. No other production module may implement either — no hardcoded
    # "v1"-style revision and no typed-in identity. The seams are class methods; a module-level
    # function of the same name (the Alembic schema revision in app/db/migrate.py) is not one.
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
    assert implementers == {
        "integrations/marketplaces/smartstore/registry.py:RegistryMappingRevision.current_revision",
        "app/connect/smartstore/service.py:SmartStoreConnectService.current_identity",
    }


def test_s17_22_marketplace_evidence_never_reads_the_wall_clock() -> None:
    # PERMISSIONS_SCOPES §8.1: current freshness uses the injected Clock — the service obtains
    # ``now`` and passes it to the pure evaluator — so expiry is deterministic and testable. No
    # capability or A0 module reads wall-clock time itself.
    wall_clock = {
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("datetime", "today"),
        ("date", "today"),
        ("time", "time"),
        ("time", "monotonic"),
    }
    modules = {
        path: tree
        for path, tree in _production_modules().items()
        if path.startswith("app/connect/marketplace/")
    }
    assert {
        "app/connect/marketplace/attestation.py",
        "app/connect/marketplace/attestation_service.py",
    } <= set(modules)
    readers, injected = set(), 0
    for path, tree in modules.items():
        for call in _calls(tree):
            func = call.func
            if not isinstance(func, ast.Attribute):
                continue
            owner = func.value
            name = (
                owner.id
                if isinstance(owner, ast.Name)
                else owner.attr
                if isinstance(owner, ast.Attribute)
                else None
            )
            if (name, func.attr) in wall_clock:
                readers.add(f"{path}:{call.lineno}")
            if (name, func.attr) == ("_clock", "now"):
                injected += 1
    assert readers == set()
    assert injected >= 2  # the A0 context and the freshness recording read the injected clock


def test_schema_holds_source_truth_and_the_m4_product_foundation() -> None:
    # ADR-0010 §1: M3 persists COLLECT source truth. ADR-0013 (Issue #80 PR-B) adds the M4
    # foundation, whose canonical Product is the ProductGroup; no other product root exists.
    # CONNECT code never handles ProductFacts.
    from app.db.metadata import metadata

    assert set(metadata.tables) == {
        "jobs",
        "job_attempts",
        "audit_events",
        "supplier_connections",
        "marketplace_capabilities",
        "marketplace_workflow_overlays",
        "marketplace_permission_attestations",
        "marketplace_connections",
        "product_facts_revisions",
        "product_facts_fields",
        "product_facts_evidence",
        "source_assets",
        "product_facts_image_refs",
        # Stage-B2: the durable identity and result of one submitted collection. It is not source
        # truth and holds no product fact; it says which run produced which revision.
        "collection_runs",
        # M4 PR-B (ADR-0013): source identity, the current source revision history, the
        # canonical Product (ProductGroup) with its membership, compositions, Items and bindings.
        "source_products",
        "current_source_revision_moves",
        "product_groups",
        "group_members",
        "group_membership_revisions",
        "group_change_events",
        "listing_compositions",
        "product_items",
        "source_bindings",
        # M4 PR-D (ADR-0013 §7): pricing snapshots per Item and explicit context, and the history
        # of which one is current. No readiness table: readiness is derived, never stored.
        "pricing_snapshots",
        "current_pricing_snapshot_moves",
        # M4 PR-E (ADR-0013 §9): derived artifacts and their lineage, operator image selections and
        # exact-binary QA. PRODUCT-owned: COLLECT's source assets and references stay untouched.
        "derived_image_artifacts",
        "derived_image_derivations",
        "derived_image_derivation_inputs",
        "derived_image_derivation_roots",
        "image_selection_revisions",
        "image_selection_source_decisions",
        "image_selection_outputs",
        "current_image_selection_moves",
        "image_qa_results",
        # M4 PR-Q (ruling 5738760913): immutable, revision-scoped product-level quantity offers.
        # There is no SourceSKU table: a product-level offer has none, and none is fabricated.
        "quantity_offers",
        # M5 PR-B (ACCOUNT_IDENTITY §2, review 5255746944): the canonical seller and marketplace
        # account that scope registration state, established only from a committed M2 binding.
        "seller_entities",
        "marketplace_accounts",
        # M5 PR-B (ADR-0014): the registration foundation. Drafts and immutable Snapshots,
        # Batches with no stored summary, Intents, append-only Attempts, verified Registrations
        # and duplicate overrides. No readiness or registrability table: preflight is derived.
        "registration_drafts",
        "registration_draft_items",
        "registration_snapshots",
        "registration_item_snapshots",
        "registration_batches",
        "registration_intents",
        "registration_attempts",
        "marketplace_registrations",
        "marketplace_registration_items",
        "duplicate_overrides",
        # M5 PR-E (ADR-0014 26, architect decision 5749504280): REGISTER's own send brake for one
        # marketplace x canonical account x endpoint group, with its durable resume boundary. It
        # is not capability truth and holds no provider identity.
        "registration_execution_scopes",
        # M5 PR-F (ADR-0014 27, architect decision 5751540323): the operator-authored preparation
        # of one provider-listing unit, append-only, and the provenance of the Snapshot it froze.
        # It stores inputs only: no readiness, no status and no reason code.
        "registration_preparations",
        "registration_preparation_revisions",
        "registration_preparation_items",
        "registration_snapshot_preparations",
        # Gate 1 G1-A (ADR-0015 §2, authorization 5785935712): the durable registration target
        # policy of one marketplace x canonical account, its append-only revisions and its one
        # current revision. Policy inputs only: no readiness, price or provider truth.
        "registration_target_policies",
        "registration_target_policy_revisions",
        "registration_target_policy_current",
        # Gate 1 G1-B (ADR-0015 §3, authorization 5788082735): the durable operator-reviewed
        # category metadata of one marketplace x taxonomy x category, its append-only revisions
        # with explicit review provenance, and its one current revision.
        "registration_category_metadata",
        "registration_category_metadata_revisions",
        "registration_category_metadata_current",
        # Gate 2 G2-A (ADR-0016): the durable ReviewItem owner, an index of human work over
        # owner-derived conditions, and its append-only history. References only: no owner value,
        # readiness, verdict or provider content.
        "review_items",
        "review_item_events",
        # Gate 2 G2-B (ADR-0016 §4, §7): each review producer's coverage watermark and its
        # known indexing failure. Whether coverage is current is derived, never stored.
        "review_coverage",
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
