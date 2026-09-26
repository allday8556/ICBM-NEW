"""Static guards for rules that must hold regardless of runtime behaviour.

The canonical-document checks assert *active* references — the current UI authority, the
accepted milestone chain, recorded decisions — rather than banning historical strings, because
revision-history and review documents legitimately keep older prototype names.
"""

import argparse
import ast
import hashlib
import importlib
import inspect
import re
import tomllib
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
LIVE_ADR = DOCS / "adr" / "0018-gate3-pre-live-safety-and-bounded-live-authorization.md"
M5_ACCEPTANCE = DOCS / "acceptance" / "M5.md"
GLOSSARY_MD = DOCS / "GLOSSARY.md"
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
    # Gate 3 area 2 (ADR-0018 §7, §8): reads back, read-only, the schema the shipped migrations
    # build in a private temporary directory — the expected schema, never a data directory.
    "app/db/schema_contract.py": "read-only",
    # Gate 3 area 2 (ADR-0018 §7): the drill reads the active database read-only and writes only
    # the backup into a separate fresh restore root, never a data directory.
    "app/live/drill.py": "read-only or fresh restore root",
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
    assert list(invariants) == [f"AC-{n:02d}" for n in range(1, 30)]
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
    assert "SAMPLE_TRUNCATED sample ends INCOMPLETE, never PASS" in invariants["AC-25"]
    assert (
        "only SHADOW_MISSING_AFTER_RECOVERY permits a recorded supersession" in invariants["AC-26"]
    )
    assert "no window is ever abandoned" in invariants["AC-26"]
    assert "append-only event stream per collection_run_id" in invariants["AC-27"]
    assert "the denominator counts each run once" in invariants["AC-27"]
    assert "a closeout is never revised, versioned or mutated" in invariants["AC-28"]
    # P3 (Issue #110 5824551569): the exact-bundle, no-nonce rule closes carry-forward 5818794101.
    assert "a content-identical EPR is the same bundle" in invariants["AC-29"]
    assert "only a content-different EPR starts fresh evidence" in invariants["AC-29"]
    clears = _section(adr, r"^11\.3 ")
    assert "Exact bundles, no nonce" in clears and "5818794101" in clears
    # ADR-0010 §7's historical level label is aligned with COVERAGE, not left as a second name.
    (collect_adr,) = (DOCS / "adr").glob("0010-supplier-generic-collect*.md")
    levels = _section(_read(collect_adr), r"^7\. Facts: two levels")
    assert "Amendment note (ADR-0017 §4)" in levels and "`COVERAGE`" in levels
    # The six cross-audit items of Issue #110 5812200650 are each closed by a named section.
    closed = _section(adr, r"^14\. The cross-audit items, closed$")
    assert len(re.findall(r"^\| [1-6] \|", closed, re.M)) == 6
    assert closed.count("**yes**") == 2


def test_the_live_authorization_contract_is_recorded_and_pinned() -> None:
    """ADR-0018 (Gate 3 G3-0): the pre-LIVE safety contract, before any schema or runtime."""
    from app.system.execution_mode import M0_POLICY, ExecutionModeService

    adr = _read(LIVE_ADR)
    assert re.search(r"^Status: \*\*ACCEPTED\*\*", adr, re.M)
    assert "5821078540" in adr
    # The roadmap names the contract file and its kickoff; the other canonical docs cite it.
    roadmap = _read(ROADMAP_MD)
    assert f"contract `docs/adr/{LIVE_ADR.name}`" in roadmap and "5821078540" in roadmap
    for canonical in (ARCHITECTURE_MD, M5_ACCEPTANCE, GLOSSARY_MD):
        assert "ADR-0018" in _read(canonical), canonical.name
    block = adr.split("\n## Invariants", 1)[1].split("```text", 1)[1].split("```", 1)[0]
    invariants = dict(re.findall(r"^(G3-\d\d)\s+(.*\S)\s*$", block, re.M))
    assert list(invariants) == [f"G3-{n:02d}" for n in range(1, 30)]
    # D1: deny by default, exact scope, terminal states, no blind replay, never UI authority.
    assert "refused before any transmission" in invariants["G3-02"]
    assert "grant of its stage" in invariants["G3-02"]
    assert "UI text and checkboxes are never authority" in invariants["G3-03"]
    assert "never refunded, an UNKNOWN included" in invariants["G3-04"]
    assert "EXPIRED, REVOKED and EXHAUSTED are terminal" in invariants["G3-05"]
    assert "never authorizes a blind CREATE or upload replay" in invariants["G3-07"]
    # D4: the brake is fail closed, survives restart, and never rewrites an UNKNOWN.
    assert "absent or unreadable means ENGAGED" in invariants["G3-08"]
    assert "never rewrites an UNKNOWN" in invariants["G3-09"]
    assert "never resurrects" in invariants["G3-10"]
    assert "brake stays CREATE-only, unchanged and not weakened" in invariants["G3-11"]
    # D2 and D3: no ComplianceGate, eligibility is never a PASS, and the evidence blocker holds.
    assert "implements no ComplianceGate" in invariants["G3-12"]
    assert "never a COMPLIANCE PASS" in invariants["G3-13"]
    assert "CREATE and SEARCH stay NOT_ADOPTED" in invariants["G3-14"]
    assert "zero-result search are never proof of remote absence" in invariants["G3-15"]
    # D5-D7: proven prerequisites, not declarations.
    assert "a declaration is not a drill" in invariants["G3-16"]
    # Review 5821787401: the drill proves the REGISTER chain too, and never manufactures it.
    for element in (
        "RegistrationSnapshot",
        "RegistrationIntent with its idempotency key and state",
        "execution-scope brake state",
        "by identity and state",
        "recorded as absent, never created",
    ):
        assert element in invariants["G3-16"], element
    drill = _section(adr, r"^7\. Backup and restore")
    for element in (
        "**the REGISTER chain**",
        "**idempotency key**",
        "the REGISTER **execution-scope brake** state (ADR-0014 §26) for the CREATE endpoint group",
        "**no ADR-0014 §26\n  scope row is part of an ASSET proof**",
        "**The ASSET restore proof**",
        "**The CREATE restore proof**",
        "**A pre-freeze proof is never accepted",
        "the proof is **stale**",
        "**the durable upload-attempt and replay state over the whole replay-conflict scope**",
        "**whatever\n  grant, preparation revision, candidate fingerprint or local profile it was "
        "started under**",
        "a proof that\n  inspects only the current candidate's or profile's attempts "
        "proves nothing",
        "upload-attempt state of the whole replay-conflict scope for ASSET",
    ):
        assert element in drill, element
    assert "never discarded" in invariants["G3-17"]
    assert "no server-owned blocker is hidden" in invariants["G3-18"]
    assert "never permission to write" in invariants["G3-19"]
    # Review 5822405880: two mutation stages, each exactly bound, proven and gated at send time.
    for element in (
        "ASSET_MUTATION_READY before an upload",
        "CREATE_MUTATION_READY before a CREATE",
        "mandatory send-time layers",
        "eligibility, restore proof, retention and visual acceptance",
        "never depends on a PREPARED Intent",
    ):
        assert element in invariants["G3-19"], element
    for element in (
        "preparation revision, candidate fingerprint, selected artifact set and asset profile",
        "the Snapshot, Intent and idempotency key",
        "no unit-less or wildcard grant exists",
        "one stage never widens into the other",
    ):
        assert element in invariants["G3-21"], element
    assert "never re-uploaded blindly" in invariants["G3-22"]
    assert "evidence is kept while it is unresolved" in invariants["G3-22"]
    assert "never persisted, hashed or logged" in invariants["G3-23"]
    # Review 5823321537: a durable ASSET upload-attempt owner is a prerequisite of any upload.
    for key, element in (
        ("G3-24", "recorded the attempt as started in the same atomic unit that consumes"),
        (
            "G3-24",
            "terminalized exactly once as APPLIED_PROVEN, NOT_APPLIED_PROVEN or UPLOAD_UNKNOWN",
        ),
        ("G3-25", "not terminal after a crash or restart is UPLOAD_UNKNOWN"),
        ("G3-25", "never proof that no unresolved upload exists"),
        ("G3-25", "only APPLIED_PROVEN yields a known provider asset identity"),
        ("G3-26", "ASSET_MUTATION_READY requires that durable owner"),
        ("G3-26", "no started or unresolved UPLOAD_UNKNOWN attempt in the replay-conflict scope"),
        ("G3-26", "never depends on an ADR-0014 §26 scope row"),
        ("G3-27", "needs a new CREATE grant and a fresh restore proof"),
        ("G3-16", "never an ADR-0014 §26 row"),
    ):
        assert element in invariants[key], (key, element)
    owner = _section(adr, r"^3\.4 The durable ASSET upload-attempt owner")
    for element in (
        "**Therefore `ASSET_MUTATION_READY` is necessarily `BLOCKED` at this main.**",
        "If that commit fails, **nothing is transmitted**.",
        "**No record is not proof.**",
        "A restart never erases this fence.",
    ):
        assert element in owner, element
    # Reviews 5823765435, 5824235764 and 5825163444: the replay fence is keyed by the conservative
    # wire boundary only; every local or locally chosen value is provenance and never keys it.
    for key, element in (
        ("G3-28", "attempt provenance and the ASSET replay-conflict key are separate"),
        (
            "G3-28",
            "the key is exactly the marketplace, the canonical account, the normalized wire "
            "endpoint identity (HTTP method, provider host and path) and the exact outbound "
            "content digest;",
        ),
        (
            "G3-28",
            "the multipart file name, MIME or type metadata, local artifact kind, derivation_id, "
            "candidate fingerprint, preparation revision, grant, Draft revision, listing, "
            "category, policy state, local profile label, local endpoint-mapping revision, "
            "provider-document version label and any ICBM adoption or contract label are "
            "provenance only and never enter or narrow it, even when serialized on the wire",
        ),
        ("G3-28", "ambiguity takes the wider scope"),
        (
            "G3-28",
            "an undeterminable wire endpoint identity or content digest keeps the ASSET stage "
            "BLOCKED",
        ),
        (
            "G3-29",
            "across a new grant, preparation revision, candidate fingerprint, derivation, local "
            "artifact kind, file name, MIME or type metadata, local profile or contract/adoption "
            "label change, restart or batch",
        ),
        ("G3-29", "inspect the whole scope, never only the current candidate's attempts"),
        ("G3-29", "only NOT_APPLIED_PROVEN clears it for a retry"),
        (
            "G3-29",
            "an APPLIED_PROVEN in that scope keeps a fresh upload with the same key blocked, "
            "whatever file name, MIME or type metadata, candidate, derivation, local artifact "
            "kind, profile or contract label asks, until a separately adopted reuse/rebind path "
            "exists",
        ),
        ("G3-16", "upload-attempt state over the whole replay-conflict scope"),
    ):
        assert element in invariants[key], (key, element)
    provenance, fence = owner.split(
        "- **Replay-conflict key — the conservative wire boundary.**", 1
    )
    provenance = provenance.split("- **Provenance.**", 1)[1]
    key_fields, fence_rule = fence.split("- **Replay fence, over the whole", 1)
    # The key is exactly the four wire-boundary fields; nothing local is a key field.
    key_list = re.findall(r"^  - (.*)$", key_fields.split("\n\n", 1)[0], re.M)
    assert key_list == [
        "the marketplace;",
        "the canonical account;",
        "the normalized wire endpoint identity: HTTP method, provider host and path;",
        "the exact outbound content digest of the uploaded binary.",
    ]
    flat = " ".join(key_fields.split())
    for element in (
        "one `POST /v1/product-images/upload` with one `imageFiles` multipart part",
        "The path includes a version segment only when that segment is actually in the path.",
        "**Nothing else keys a replay scope. Provenance only, never a key field, and never "
        "narrowing the scope**: the multipart file name, MIME or type metadata, the local "
        "source-or-derived artifact kind, the `derivation_id`, the candidate fingerprint, "
        "preparation revision, grant, Draft revision, listing text, category, policy state, any "
        "local profile label, a local endpoint-mapping revision, a provider-document version "
        "label and any ICBM adoption or contract label.",
        "**Even when such a value is serialized on the wire, it never makes a new replay key**",
        "the same bytes sent under another file name or MIME type are the same scope",
        "a changed ICBM contract or adoption label with an unchanged method, host and path never "
        "opens a new one",
        "A local identity used to derive a serialized file name or type is no exception.",
        "with the same outbound bytes are **one** replay-conflict scope",
        "**Ambiguity is resolved by the wider scope, never by inventing another key.**",
        "never as a key that narrows this fence",
        "**If the wire endpoint identity or the outbound content digest cannot be determined, the "
        "ASSET stage stays `BLOCKED`.**",
    ):
        assert element in flat, element
    flat_provenance = " ".join(provenance.split())
    for element in (
        "local artifact tuple (the local source-or-derived artifact kind, SHA-256 and "
        "`derivation_id`)",
        "its local adoption or contract label",
        "the multipart file name and MIME or type metadata actually sent",
        "it never decides which attempts block another**",
    ):
        assert element in flat_provenance, element
    flat_fence = " ".join(fence_rule.split())
    for element in (
        "attempt **anywhere in a replay-conflict key's scope** blocks every new upload",
        "another derivation or local artifact kind of the same bytes, another file name or MIME "
        "or type metadata, a local profile or contract/adoption label change,",
        "a new candidate fingerprint",
        "Changing local provenance never erases an unresolved remote-mutation ambiguity.",
        "A `NOT_APPLIED_PROVEN` attempt may clear that ambiguity for a retry",
        "fresh ASSET restore proof",
        "**The same key governs `APPLIED_PROVEN`.**",
        "**keeps a fresh upload with that key blocked**",
        "**never re-sent merely because the file name, MIME or type metadata, derivation, local "
        "artifact kind, candidate, preparation, grant, profile or local contract label changed**",
        "until one is adopted a fresh upload in that scope stays blocked",
    ):
        assert element in flat_fence, element
    # The liveness cost of the conservative key is recorded, never used to narrow it.
    consequences = " ".join(adr.split("\n## Consequences", 1)[1].split("\n## ", 1)[0].split())
    assert "**The ASSET replay key is deliberately over-conservative**" in consequences
    assert "it is **never** a reason to narrow the replay key" in consequences
    assert "the exact artifact, candidate and profile" not in adr
    assert "provider-visible upload-request identity" not in adr
    assert "kind, SHA-256 and derivation identity — or an equivalent" not in adr
    grant = _section(adr, r"^3\.2 What a grant binds")
    assert "**A grant's exact unit is authorization provenance, never a replay boundary.**" in grant
    for element in (
        "bound to a target state digest and stale once that state changes",
        "taken after the freeze",
        "which it may never record as absent",
        "a pre-freeze proof never gates a CREATE",
    ):
        assert element in invariants["G3-16"], element
    stages = _section(adr, r"^10\. Mutation-stage readiness")
    assert "**it is a mandatory layer of the send-time" in stages
    assert "**The ASSET stage never depends on a `PREPARED` Intent**" in stages
    assert "**`ASSET_MUTATION_READY` is `BLOCKED` at this main**" in stages
    assert "proven from that owner and never from row absence" in stages
    assert "**no started or unresolved `UPLOAD_UNKNOWN` attempt in the replay-conflict scope**" in (
        stages
    )
    assert "whatever grant, preparation revision, candidate fingerprint or local profile" in stages
    assert "**The ASSET readiness queries the whole replay-conflict scope**" in stages
    assert "a readiness that does is not\n  `ASSET_MUTATION_READY`" in stages
    safety = _section(adr, r"^4\.3 The whole safety stack")
    assert "the stage's mutation readiness is `READY`" in safety
    assert "**for the CREATE stage only**" in safety
    assert "**The ASSET stage has no §26\n   scope owner" in safety
    assert "`ASSET_MUTATION_READY` before an upload" in safety
    assert "`CREATE_MUTATION_READY` before a CREATE" in safety
    # G3-0 changes no runtime: the M0 policy still refuses LIVE, and M5 is still PENDING.
    assert M0_POLICY == "M0_DRY_RUN_ONLY"
    assert "live_writes_permitted=False" in inspect.getsource(ExecutionModeService.state)
    assert "Status: **PENDING**" in _read(M5_ACCEPTANCE).split("\n---", 1)[0]
    assert "authorizes nothing to run" in adr.split("\n---", 1)[0]


# ---------------------------------------------------------------- Gate 3 area 1 (ADR-0018 §12)

LIVE_OWNER = "app/live/store.py"
LIVE_ROWS = frozenset(
    {
        "LiveGrant",
        "ProtectedWriteBrake",
        "AssetUploadAttempt",
        "RestoreDrill",
        "RetentionProof",
        "VisualAcceptance",
    }
)


def test_a_visual_acceptance_is_recorded_only_through_the_verified_command() -> None:
    """ADR-0018 §9 (Gate 3 area 3, authorization 5843380581): ``VISUAL_ACCEPTANCE_RECORDED`` is
    never asserted. The one writer records only what ``app.live.visual`` verified, and the only
    caller of that recorder is the ``icbm live record-visual-acceptance`` command: no route, page
    or other module can record or assert a visual acceptance."""
    modules = _production_modules()
    writers = {
        path
        for path, tree in modules.items()
        for call in _calls(tree)
        if _callee(call) == "record_visual_acceptance"
    }
    assert writers == {"app/live/visual.py"}
    recorders = {
        path
        for path, tree in modules.items()
        for call in _calls(tree)
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "record"
        and isinstance(call.func.value, ast.Attribute)
        and call.func.value.attr == "visual_acceptance"
    }
    assert recorders == {"app/cli.py"}
    importers = {
        path for path, tree in modules.items() if "app.live.visual" in _imported_modules(tree)
    }
    assert importers <= {"app/container.py", "app/live/proofs.py"}, importers
    for path, tree in modules.items():
        if path.startswith("app/api/"):
            assert not [m for m in _imported_modules(tree) if m.startswith("app.live")], path
    for page in (REPO_ROOT / "ui/web").rglob("*.js"):
        text = page.read_text("utf-8")
        assert "visual-acceptance" not in text and "record-visual" not in text, page


def test_only_the_live_owner_writes_the_live_tables() -> None:
    """ADR-0018 §3, §3.4, §4: one writer for grants, the brake and ASSET attempts."""
    users = {
        path
        for path, tree in _production_modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.alias)
        and (node.id if isinstance(node, ast.Name) else node.name) in LIVE_ROWS
    }
    # The models module defines the rows; only the owner names them to build, change or read one.
    assert users == {LIVE_OWNER}


def test_the_live_owners_reach_no_provider() -> None:
    """Gate 3 area 1 is provider-zero: the owners import no client, transport or adapter."""
    allowed = (
        "__future__",
        "collections.abc",
        "contextlib",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "ipaddress",
        "json",
        "logging",
        "pathlib",
        "re",
        "shutil",
        "sqlite3",
        "typing",
        "uuid",
        "sqlalchemy",
        "app.audit",
        "app.connect.accounts",
        "app.core.clock",
        "app.core.errors",
        "app.core.execution",
        "app.db.base",
        "app.db.database",
        # Gate 3 area 2: the expected schema the shipped migrations build (drill and retention).
        "app.db.schema_contract",
        "app.db.types",
        "app.live",
        "app.products.image_model",
        "app.products.model",
        "app.register.model",
        "app.register.preparation",
        "app.register.sanitize",
        "app.register.store",
    )
    modules = {p: t for p, t in _production_modules().items() if p.startswith("app/live/")}
    assert {"app/live/stack.py", "app/live/assets.py", LIVE_OWNER} <= set(modules)
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            assert any(name == a or name.startswith(f"{a}.") for a in allowed), f"{path}: {name}"


def test_the_container_wires_the_deny_by_default_stack_and_no_sender() -> None:
    """At this main the stack reads the M0 execution-mode owner and no proof exists; the CREATE
    owner is wired to that stack, and the ASSET path to a sender that sends nothing."""
    tree = ast.parse((REPO_ROOT / "app/container.py").read_text("utf-8"))
    (stack,) = _calls(tree, "SafetyStack")
    mode, proofs = _keyword(stack, "mode"), _keyword(stack, "proofs")
    assert isinstance(mode, ast.Name) and mode.id == "execution_mode"
    # Area 2: the restore and retention proofs are durable owners. Area 3: visual acceptance is the
    # reviewed record of exactly the running code (its digest, taken once at composition) at the
    # current head. Eligibility (§5) has no owner yet, so the durable proof source answers False.
    assert isinstance(proofs, ast.Name) and proofs.id == "stage_proofs"
    (durable,) = _calls(tree, "DurableStageProofs")
    visual = _keyword(durable, "visual")
    assert isinstance(visual, ast.Name) and visual.id == "visual_acceptance"
    (recorder,) = _calls(tree, "VisualAcceptanceService")
    for keyword in ("code_sha", "code_identity"):
        bound = _keyword(recorder, keyword)
        assert isinstance(bound, ast.Lambda) and isinstance(bound.body, ast.Name), keyword
    (digest,) = _calls(tree, "running_code_digest")
    assert ast.unparse(digest) == "running_code_digest(config.ui_dir)"
    (sha,) = _calls(tree, "running_checkout_sha")
    assert ast.unparse(sha) == "running_checkout_sha()"
    source = importlib.import_module("app.live.proofs").DurableStageProofs
    never = inspect.getsource(source.canary_non_regulated)
    assert never.rstrip().endswith("return False")
    recorded = inspect.getsource(source.visual_acceptance_recorded)
    assert recorded.rstrip().endswith("return self._visual.recorded()")
    owner = inspect.getsource(importlib.import_module("app.live.visual").VisualAcceptanceService)
    assert "unit.visual_accepted(sha, self._code(), head)" in owner
    assert 'if not sha or sha != report["code_sha"]:' in owner
    (execution,) = _calls(tree, "RegistrationExecutionService")
    authority = _keyword(execution, "authority")
    assert isinstance(authority, ast.Name) and authority.id == "safety_stack"
    (uploads,) = _calls(tree, "AssetUploadService")
    sender = _keyword(uploads, "sender")
    assert isinstance(sender, ast.Call) and _callee(sender) == "UnwiredAssetSender"
    # No production module can build a permitting mode, a proven proof or an admitting authority.
    for path, module in _production_modules().items():
        defined = {n.name for n in ast.walk(module) if isinstance(n, ast.ClassDef)}
        assert not defined & {"PermittedMode", "ProvenProofs", "AdmittingAuthority"}, path
        senders = [
            n.name
            for n in ast.walk(module)
            if isinstance(n, ast.ClassDef)
            and not any(isinstance(b, ast.Name) and b.id == "Protocol" for b in n.bases)
            and any(
                isinstance(f, ast.FunctionDef)
                and f.name == "send"
                and "content" in {a.arg for a in f.args.kwonlyargs}
                for f in n.body
            )
        ]
        assert senders in ([], ["UnwiredAssetSender"]), (path, senders)
    unwired = inspect.getsource(importlib.import_module("app.live.assets").UnwiredAssetSender)
    assert "return False" in unwired and "raise TransmissionPrecluded" in unwired


def test_no_application_module_constructs_the_image_upload_adapter() -> None:
    """The adopted upload caller is reachable only through a wired ASSET sender, and none is."""
    users = {
        path for path, tree in _production_modules().items() if _calls(tree, "ImageUploadAdapter")
    }
    assert users == set()


def test_the_asset_replay_key_is_the_wire_boundary_and_the_fence_is_in_the_schema() -> None:
    """G3-28, G3-29: four key fields; the partial unique index fences every non-proven state."""
    from app.live.model import FENCING_UPLOAD_STATES, UploadAttemptState, replay_key
    from app.live.models import AssetUploadAttempt

    assert list(inspect.signature(replay_key).parameters) == [
        "marketplace_key",
        "marketplace_account_id",
        "endpoint",
        "content_sha256",
    ]
    assert (
        set(UploadAttemptState) - {UploadAttemptState.NOT_APPLIED_PROVEN} == FENCING_UPLOAD_STATES
    )
    (fence,) = [i for i in AssetUploadAttempt.__table__.indexes if i.name.endswith("_replay_fence")]
    assert fence.unique and [c.name for c in fence.columns] == ["replay_key"]
    where = str(fence.dialect_options["sqlite"]["where"])
    for state in FENCING_UPLOAD_STATES:
        assert f"'{state.value}'" in where
    assert "'NOT_APPLIED_PROVEN'" not in where
    migration = (REPO_ROOT / "app/db/migrations/versions/0026_g3_live_authority.py").read_text(
        "utf-8"
    )
    assert "state IN ('APPLIED_PROVEN', 'STARTED', 'UPLOAD_UNKNOWN')" in migration


# The owner rows the REGISTER preflight, the send gate and the safety stack read, and the one
# production module that creates each. Gate 3 area 1 carry-forward: the send-time fence is the
# audited owner-write count, so a new writer of this truth is a review point — it must be an
# audited owner write before it is added here, or the fence stops covering it.
PREFLIGHT_TRUTH_WRITERS = {
    **dict.fromkeys(
        (
            "RegistrationDraft", "RegistrationDraftItem", "RegistrationSnapshot",
            "RegistrationItemSnapshot", "RegistrationBatch", "RegistrationIntent",
            "RegistrationAttempt", "MarketplaceRegistration", "MarketplaceRegistrationItem",
            "DuplicateOverride", "RegistrationExecutionScope", "RegistrationPreparation",
            "RegistrationPreparationRevision", "RegistrationPreparationItem",
            "RegistrationSnapshotPreparation",
        ),
        "app/register/store.py",
    ),
    **dict.fromkeys(
        ("RegistrationTargetPolicy", "RegistrationTargetPolicyRevision",
         "RegistrationTargetPolicyCurrent"),
        "app/register/target_policy.py",
    ),
    **dict.fromkeys(
        ("RegistrationCategoryMetadata", "RegistrationCategoryMetadataRevision",
         "RegistrationCategoryMetadataCurrent"),
        "app/register/category_metadata.py",
    ),
    **dict.fromkeys(
        ("ProductGroup", "ProductItem", "GroupMember", "GroupMembershipRevision",
         "ListingComposition", "SourceBinding", "SourceProduct", "CurrentSourceRevisionMove",
         "QuantityOffer"),
        "app/products/store.py",
    ),
    **dict.fromkeys(
        ("PricingSnapshot", "CurrentPricingSnapshotMove"), "app/products/pricing_store.py"
    ),
    **dict.fromkeys(
        ("DerivedImageArtifact", "DerivedImageDerivation", "DerivedImageDerivationInput",
         "DerivedImageDerivationRoot", "ImageSelectionRevision", "ImageSelectionOutput",
         "ImageSelectionSourceDecision", "CurrentImageSelectionMove", "ImageQaResult"),
        "app/products/image_store.py",
    ),
    **dict.fromkeys(("ProductFactsRevision", "ProductFactsField", "ProductFactsEvidence",
                     "ProductFactsImageRef"), "app/collect/revisions.py"),
    "SourceAsset": "app/collect/assets.py",
    **dict.fromkeys(("MarketplaceAccount", "SellerEntity"), "app/connect/accounts.py"),
    **dict.fromkeys(("MarketplaceCapability", "MarketplaceWorkflowOverlay"),
                    "app/connect/marketplace/service.py"),
    "MarketplaceConnection": "app/connect/smartstore/service.py",
    **dict.fromkeys(("LiveGrant", "ProtectedWriteBrake", "AssetUploadAttempt", "RestoreDrill",
                     "RetentionProof", "VisualAcceptance"), "app/live/store.py"),
}  # fmt: skip


def test_the_truth_the_register_preflight_reads_has_one_reviewed_writer_each() -> None:
    """Area 1 carry-forward: no new writer of preflight-read truth appears without review."""
    constructed: dict[str, set[str]] = {}
    for path, tree in _production_modules().items():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in PREFLIGHT_TRUTH_WRITERS
            ):
                constructed.setdefault(node.func.id, set()).add(path)
    assert constructed == {name: {path} for name, path in PREFLIGHT_TRUTH_WRITERS.items()}


def test_every_reviewed_writer_of_preflight_truth_appends_to_the_audit_log() -> None:
    """Each pinned writer module (or the owner that drives it) appends owner audit events."""
    drivers = {
        "app/products/store.py": ("app/products/materializer.py", "app/products/service.py"),
        "app/products/pricing_store.py": ("app/products/pricing.py",),
        "app/products/image_store.py": ("app/products/images.py",),
        "app/collect/revisions.py": ("app/collect/revisions.py", "app/collect/collection.py"),
        "app/collect/assets.py": ("app/collect/assets.py", "app/collect/collection.py"),
    }
    modules = _production_modules()
    for writer in sorted(set(PREFLIGHT_TRUTH_WRITERS.values())):
        owners = drivers.get(writer, (writer,))
        audited = any(
            _calls(modules[owner], "append") or _calls(modules[owner], "_event")
            for owner in owners
            if owner in modules
        )
        assert audited, writer


def test_the_drill_writes_only_into_the_fresh_root_it_validated() -> None:
    """ADR-0018 §7: the one writable connection is opened by the drill run, after the root was
    validated as fresh and outside the active data root."""
    tree = ast.parse((REPO_ROOT / "app/live/drill.py").read_text("utf-8"))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    callers = {name for name, fn in functions.items() if _calls(fn, "_backup_into_fresh_root")}
    assert callers == {"_run"}
    for stage in ("drill_asset", "drill_create"):
        body = functions[stage]
        fresh = [c.lineno for c in _calls(body, "_fresh_root")]
        run = [c.lineno for c in _calls(body, "_run")]
        assert fresh and run and min(fresh) < min(run), stage


def test_a_create_drill_never_records_the_register_chain_as_absent() -> None:
    """ADR-0018 §7: a CREATE proof never records the Snapshot or the Intent as absent."""
    tree = ast.parse((REPO_ROOT / "app/live/drill.py").read_text("utf-8"))
    chain = {"snapshot", "item_snapshots", "snapshot_provenance", "batch", "intent"}
    seen = set()
    for call in _calls(tree, "Element"):
        name = call.args[0] if call.args else None
        if isinstance(name, ast.Constant) and name.value in chain:
            seen.add(name.value)
            for keyword in ("required", "must_be_absent"):
                value = _keyword(call, keyword)
                expected = keyword == "required"
                assert value is None or (
                    isinstance(value, ast.Constant) and value.value is expected
                )
    assert seen == chain


def test_the_evidence_readers_only_read() -> None:
    """The drill and the retention proof name owner tables only to read them: no write SQL, no row
    added, and the active database opened read-only (the restore root aside)."""
    # A statement that writes starts with its verb; a word inside a check (BEFORE DELETE) does not.
    write = re.compile(
        r"^\s*(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|REPLACE\s+INTO|DROP\s+\w|ALTER\s+\w"
        r"|CREATE\s+\w)",
        re.IGNORECASE,
    )
    for path in ("app/live/drill.py", "app/live/retention.py"):
        tree = ast.parse((REPO_ROOT / path).read_text("utf-8"))
        texts = [node.value for node in _code_strings(tree)]
        assert not [text for text in texts if write.search(text)], path
        assert not _calls(tree, "add") and not _calls(tree, "delete"), path
        assert not _calls(tree, "commit"), path


def test_no_production_code_deletes_protected_evidence() -> None:
    """ADR-0018 §8: no automatic deletion of canary-scope evidence is authorized."""
    from app.live.retention import PROTECTED_TABLES

    offenders = []
    for path, tree in _production_modules().items():
        if "/migrations/" in path:
            continue
        for node in _code_strings(tree):
            text = node.value.upper()
            for table in PROTECTED_TABLES:
                if f"DELETE FROM {table.upper()}" in text:
                    offenders.append(f"{path}:{node.lineno}:{table}")
        for call in _calls(tree, "delete"):
            offenders.extend(
                f"{path}:{call.lineno}"
                for arg in call.args
                if isinstance(arg, ast.Name) and arg.id in PREFLIGHT_TRUTH_WRITERS
            )
    assert offenders == []


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
            elif gate == "read-only or fresh restore root" and getattr(function, "name", "") == (
                "_backup_into_fresh_root"
            ):
                continue
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


# ADR-0017 P1 (Issue #110 5821999699): no dynamic-import or code-evaluation escape in the source
# truth path, so an import rule can never be walked around at run time.
DYNAMIC_ESCAPES = frozenset({"__import__", "exec", "eval", "compile", "__builtins__"})


def test_production_code_has_no_dynamic_import_escape() -> None:
    # P1 held this for the source-truth path; P2 wires persistence into ``app/`` (Issue #110
    # 5822923514 carry-forward 1), so it now holds for every production module.
    modules = _production_modules()
    assert {"app/collect/adaptive/engine.py", "app/collect/adaptive_store/store.py"} <= set(modules)
    assert {"app/main.py", "integrations/suppliers/registry.py"} <= set(modules)
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            assert name.split(".")[0] != "importlib", f"{path}: {name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in DYNAMIC_ESCAPES:
                pytest.fail(f"{path}: {node.id}")
            if isinstance(node, ast.Attribute) and node.attr in {"__import__", "import_module"}:
                pytest.fail(f"{path}: .{node.attr}")


# ADR-0017 P1: the Adaptive core is pure and offline. It may import only these stdlib modules,
# pydantic, the COLLECT value models and itself — no network, browser, DB, gateway, session,
# CONNECT, provider, AI or OCR code — and nothing in production wires it in yet.
ADAPTIVE_ROOT = "app/collect/adaptive/"
ADAPTIVE_STDLIB = frozenset(
    {
        "collections",
        "collections.abc",
        "dataclasses",
        "enum",
        "functools",
        "hashlib",
        "html.parser",
        "json",
        "math",
        "re",
        "struct",
        "types",
        "typing",
        "urllib.parse",
    }
)
ADAPTIVE_MAY_IMPORT = frozenset({"pydantic", "app.collect.facts"})


def test_the_adaptive_core_imports_only_pure_modules() -> None:
    modules = {p: t for p, t in _production_modules().items() if p.startswith(ADAPTIVE_ROOT)}
    assert f"{ADAPTIVE_ROOT}validation.py" in modules
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            allowed = (
                name in ADAPTIVE_STDLIB
                or name in ADAPTIVE_MAY_IMPORT
                or name == "app.collect.adaptive"
                or name.startswith("app.collect.adaptive.")
            )
            assert allowed, f"{path}: {name}"


# ADR-0017 P2 (Issue #110 5822024807): the persistence owner may reach the database, the clock,
# the error vocabulary, the pure core and the supplier registry (its gate) — and nothing that
# acts: no network, browser, gateway, session, COLLECT runtime, review or audit owner.
ADAPTIVE_STORE_ROOT = "app/collect/adaptive_store/"
ADAPTIVE_STORE_MAY_IMPORT = frozenset(
    {
        "collections.abc",
        "dataclasses",
        "datetime",
        "json",
        "typing",
        "uuid",
        "pydantic",
        "sqlalchemy",
        "sqlalchemy.orm",
        "app.core.clock",
        "app.core.errors",
        "app.db.base",
        "app.db.database",
        "app.db.types",
        "integrations.suppliers.base",
        "integrations.suppliers.collection",
        "integrations.suppliers.registry",
    }
)


ADAPTIVE_PACKAGES = (
    "app.collect.adaptive",
    "app.collect.adaptive_store",
    "app.collect.adaptive_shadow",
    "app.collect.adaptive_capture",
)


def _is_adaptive(name: str) -> bool:
    return any(name == package or name.startswith(f"{package}.") for package in ADAPTIVE_PACKAGES)


def test_the_adaptive_store_imports_only_what_persistence_needs() -> None:
    modules = {p: t for p, t in _production_modules().items() if p.startswith(ADAPTIVE_STORE_ROOT)}
    assert f"{ADAPTIVE_STORE_ROOT}store.py" in modules
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            assert name in ADAPTIVE_STORE_MAY_IMPORT or _is_adaptive(name), f"{path}: {name}"


def test_the_disposable_phase_b_prototype_never_reached_the_repository() -> None:
    prototypes = REPO_ROOT / "prototypes"
    assert not prototypes.exists(), (
        f"{prototypes} exists. The Adaptive Collector Phase B prototype (PR #114, closed unmerged) "
        "is evidence only and is never promoted or copied into the repository (ADR-0017 §13); "
        "production Adaptive code is written fresh under app/collect/adaptive*/. Remove the "
        "directory (a leftover local checkout, or a copy) rather than weakening this rule."
    )


# ADR-0017 P3 (Issue #110 5824551569): the shadow owner may read the canonical run and fact
# models and the canonical side of the shadow seam, and write only its own tables. It may not
# import a canonical writer (the revision store, source-asset recorder, run store, collection,
# review, product, audit or job owners), a transport, a session or anything that reaches a network
# (S1, S4, S6).
ADAPTIVE_SHADOW_ROOT = "app/collect/adaptive_shadow/"
ADAPTIVE_SHADOW_MAY_IMPORT = frozenset(
    {
        "collections",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "json",
        "logging",
        "typing",
        "urllib.parse",
        "uuid",
        "sqlalchemy",
        "sqlalchemy.orm",
        "app.core.clock",
        "app.core.errors",
        "app.db.base",
        "app.db.database",
        "app.db.types",
        "app.collect.facts",
        "app.collect.urls",
        "app.collect.models",
        "app.collect.shadow",
        "integrations.suppliers.collection",
    }
)
SHADOW_OWN_MODELS = frozenset(
    {"ShadowSwitchEntry", "ShadowRecord", "EvidenceWindow", "EvidenceWindowEvent", "LedgerEvent"}
)


def test_the_shadow_owner_imports_no_canonical_writer_and_no_network() -> None:
    modules = {p: t for p, t in _production_modules().items() if p.startswith(ADAPTIVE_SHADOW_ROOT)}
    assert {f"{ADAPTIVE_SHADOW_ROOT}{m}.py" for m in ("runner", "store", "switch")} <= set(modules)
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            assert name in ADAPTIVE_SHADOW_MAY_IMPORT or _is_adaptive(name), f"{path}: {name}"


def test_the_shadow_owner_constructs_only_its_own_rows() -> None:
    # S4: the canonical models are read through queries, never built or added.
    for path, tree in _production_modules().items():
        if not path.startswith(ADAPTIVE_SHADOW_ROOT):
            continue
        canonical = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module in ("app.collect.models", "app.jobs.models")
            for alias in node.names
        }
        built = {_callee(call) for call in ast.walk(tree) if isinstance(call, ast.Call)}
        assert not (canonical & built), f"{path}: {sorted(canonical & built)}"


def test_only_the_shadow_switch_moves_an_epr_into_or_out_of_shadow() -> None:
    callers = {
        path
        for path, tree in _production_modules().items()
        if any(
            isinstance(node, ast.Attribute) and node.attr == "_switch_transition"
            for node in ast.walk(tree)
        )
    }
    assert callers == {f"{ADAPTIVE_SHADOW_ROOT}switch.py"}
    store = ast.parse((REPO_ROOT / "app/collect/adaptive_store/store.py").read_text("utf-8"))
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "_switch_transition"
        for node in ast.walk(store)
    )


def test_only_the_recovery_branch_settles_a_run_by_recovery() -> None:
    # Review 5312254605 B2: settled_by_recovery decides the one non-blocking missing-shadow cause,
    # so exactly one call site — the collection's revision-recovery branch — may set it true.
    callers = [
        (path, node.lineno)
        for path, tree in _production_modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "recovered"
    ]
    assert [path for path, _ in callers] == ["app/collect/collection.py"], callers
    tree = ast.parse((REPO_ROOT / "app/collect/collection.py").read_text("utf-8"))
    (run_job,) = (
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_job"
    )
    recovery = next(
        n
        for n in ast.walk(run_job)
        if isinstance(n, ast.If)
        and any(isinstance(c, ast.Attribute) and c.attr == "for_run" for c in ast.walk(n.test))
    )
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "recovered" for n in ast.walk(recovery)
    ), "the marker is set inside the branch that found an already-appended revision"


def test_the_canonical_collection_knows_only_the_shadow_seam() -> None:
    # The canonical owners depend on the protocols of app.collect.shadow, never on an Adaptive
    # package; only the container composes the two.
    for path in ("app/collect/collection.py", "app/collect/runs.py", "app/collect/shadow.py"):
        tree = ast.parse((REPO_ROOT / path).read_text("utf-8"))
        assert not any(_is_adaptive(name) for name in _imported_modules(tree)), path


# Who may import the Adaptive packages at all: the packages themselves, the schema aggregate for
# their models, and the container that composes them (P3). No COLLECT owner, route, job or script
# imports them.
ADAPTIVE_IMPORTERS = {
    "app/db/metadata.py": {
        "app.collect.adaptive_store",
        "app.collect.adaptive_shadow",
        "app.collect.adaptive_capture",
    },
    "app/container.py": {
        "app.collect.adaptive.hooks",
        "app.collect.adaptive_capture.accounting",
        "app.collect.adaptive_capture.commands",
        "app.collect.adaptive_capture.runner",
        "app.collect.adaptive_capture.store",
        "app.collect.adaptive_shadow.runner",
        "app.collect.adaptive_shadow.store",
        "app.collect.adaptive_shadow.switch",
        "app.collect.adaptive_store.gate",
        "app.collect.adaptive_store.store",
    },
}
# Phase C C0 (Issue #110 5826469852 item 2, 6): the harness under scripts/phasec is the only other
# importer, and the only non-test caller of the Phase C operator actions.
PHASE_C_HARNESS = "scripts/phasec/"
ADAPTIVE_CAPTURE_ROOT = "app/collect/adaptive_capture/"


def test_only_the_container_wires_the_adaptive_collector() -> None:
    roots = [*PRODUCTION_ROOTS, REPO_ROOT / "scripts"]
    for root in roots:
        for file in root.rglob("*.py"):
            relative = file.relative_to(REPO_ROOT).as_posix()
            if relative.startswith(
                (ADAPTIVE_ROOT, ADAPTIVE_STORE_ROOT, ADAPTIVE_SHADOW_ROOT, ADAPTIVE_CAPTURE_ROOT)
            ):
                continue
            if relative.startswith(PHASE_C_HARNESS):
                continue  # the harness: pinned by the Phase C rules below
            allowed = ADAPTIVE_IMPORTERS.get(relative, set())
            for name in _imported_modules(ast.parse(file.read_text("utf-8"))):
                if _is_adaptive(name):
                    assert name in allowed, f"{relative}: {name}"
    # The pure core never reaches its persistence or shadow owner, and the persistence owner
    # never reaches the shadow owner.
    for path, tree in _production_modules().items():
        names = _imported_modules(tree)
        if path.startswith(ADAPTIVE_ROOT):
            assert not any(n.startswith(ADAPTIVE_PACKAGES[1:]) for n in names), path
        if path.startswith(ADAPTIVE_STORE_ROOT):
            assert not any(n.startswith("app.collect.adaptive_shadow") for n in names), path


# ------------------------------------------------------------ Phase C C0 (Issue #110 5826469852)

# The container attributes behind the Phase C operator actions (capture requests and sample
# finalization, the shadow switch, windows, resolutions). Outside the Adaptive packages, only the
# container builds them, the app lifespan calls the shadow owner's startup pass, and the harness
# uses them; no route, service, job or other script ever reaches them.
PHASE_C_OWNERS = frozenset(
    {"shadow_switch", "shadow_evidence", "capture_store", "phase_c_commands", "phase_c_reads"}
)
PHASE_C_OWNER_MODULES = frozenset(
    {
        "app.collect.adaptive_shadow.switch",
        "app.collect.adaptive_shadow.store",
        "app.collect.adaptive_capture.store",
        "app.collect.adaptive_capture.commands",
        "app.collect.adaptive_capture.accounting",
    }
)


def _phase_c_modules() -> dict[str, ast.Module]:
    roots = [*PRODUCTION_ROOTS, REPO_ROOT / "scripts"]
    return {
        path.relative_to(REPO_ROOT).as_posix(): ast.parse(path.read_text("utf-8"))
        for root in roots
        for path in root.rglob("*.py")
    }


def test_only_the_harness_reaches_the_phase_c_operator_actions() -> None:
    adaptive = (ADAPTIVE_ROOT, ADAPTIVE_STORE_ROOT, ADAPTIVE_SHADOW_ROOT, ADAPTIVE_CAPTURE_ROOT)
    users: dict[str, set[str]] = {}
    for path, tree in _phase_c_modules().items():
        if path.startswith(adaptive):
            continue
        reached = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in PHASE_C_OWNERS
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module in PHASE_C_OWNER_MODULES
        }
        if reached:
            users[path] = reached
    assert {p for p in users if not p.startswith(PHASE_C_HARNESS)} == {
        "app/container.py",
        "app/main.py",
    }, users
    assert users["app/main.py"] == {"shadow_evidence"}
    main = ast.parse((REPO_ROOT / "app/main.py").read_text("utf-8"))
    calls = {
        node.attr
        for node in ast.walk(main)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "shadow_evidence"
    }
    assert calls == {"on_startup"}, "the app itself only runs the startup pass"
    assert {p for p in users if p.startswith(PHASE_C_HARNESS)} == {
        f"{PHASE_C_HARNESS}harness.py"
    }, users


def _within_lease(tree: ast.Module, callee: str) -> bool:
    """Every call to ``callee`` sits lexically inside ``with <lease>:`` over an
    ``acquire_data_dir(...)`` result that was bound before it."""
    leases: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _callee(node.value) == "acquire_data_dir"
        ):
            leases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Name) and item.context_expr.id in leases
            for item in node.items
        ):
            guarded |= {id(inner) for inner in ast.walk(node)}
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and _callee(c) == callee]
    return bool(calls) and all(id(call) in guarded for call in calls)


def test_the_harness_composes_the_owners_only_under_the_data_root_lease() -> None:
    # Supplement 5313045448: acquire the ADR-0006 lease, then compose, then act, all under it.
    tree = ast.parse((REPO_ROOT / PHASE_C_HARNESS / "harness.py").read_text("utf-8"))
    assert _within_lease(tree, "compose"), "the owners are composed only under the lease"
    handlers = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "HANDLERS"
    ]
    assert handlers and all(
        id(node)
        in {id(inner) for w in ast.walk(tree) if isinstance(w, ast.With) for inner in ast.walk(w)}
        for node in handlers
    ), "every handler runs under the lease"


def test_the_harness_never_opens_sqlite_reaches_a_network_or_submits_a_collection() -> None:
    # The live data root is reached only through the composed owners. The one SQLite file the
    # harness opens is its own campaign ledger (review 5313663701 B4): only ``ledger.py`` imports
    # ``sqlite3``, and it imports nothing of the application, so it cannot name a data root.
    ledger = f"{PHASE_C_HARNESS}ledger.py"
    ledger_imports = _imported_modules(ast.parse((REPO_ROOT / ledger).read_text("utf-8")))
    assert "sqlite3" in ledger_imports
    assert not any(n == "app" or n.startswith("app.") for n in ledger_imports), ledger_imports
    forbidden_modules = (
        "sqlite3",
        "sqlalchemy",
        "httpx",
        "requests",
        "socket",
        "urllib.request",
        "playwright",
        "integrations.suppliers.transport",
        "app.db.database",
    )
    for path, tree in _phase_c_modules().items():
        if not path.startswith((PHASE_C_HARNESS, "scripts/phase_c.py")):
            continue
        names = _imported_modules(tree) - ({"sqlite3"} if path == ledger else set())
        assert not any(n == f or n.startswith(f"{f}.") for n in names for f in forbidden_modules), (
            path,
            names,
        )
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        # C1 PREP-0: nor does it collect, read a supplier's CONNECT or policy target, or log in.
        assert not attributes & {
            "submit",
            "run_next",
            "collect",
            "read_document",
            "read_image",
            "read_discovered_policy",
            "collection_session",
            "ensure_connected",
            "verify",
            "fetch",
            "login",
        }, path


def test_the_capture_owner_imports_no_canonical_writer_and_no_network() -> None:
    modules = {
        p: t for p, t in _production_modules().items() if p.startswith(ADAPTIVE_CAPTURE_ROOT)
    }
    assert {f"{ADAPTIVE_CAPTURE_ROOT}{m}.py" for m in ("runner", "store", "controls")} <= set(
        modules
    )
    allowed = ADAPTIVE_SHADOW_MAY_IMPORT | {"types", "hashlib"}
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            assert name in allowed or _is_adaptive(name), f"{path}: {name}"


def test_the_collection_knows_only_the_capture_seam() -> None:
    tree = ast.parse((REPO_ROOT / "app/collect/collection.py").read_text("utf-8"))
    assert "app.collect.shadow" in _imported_modules(tree)
    assert not any(_is_adaptive(name) for name in _imported_modules(tree))


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
        # Adaptive Collector P2 (ADR-0017 §3, §7, Issue #110 5822923514): immutable EPR/PTR
        # revisions with their pins and DRAFT lint, the append-only EPR lifecycle, local-only
        # samples and validation runs. No VALIDATED or ACTIVE table: VALIDATED is derived.
        "adaptive_profile_revisions",
        "adaptive_profile_pins",
        "adaptive_profile_lint",
        "adaptive_profile_transitions",
        "adaptive_validation_samples",
        "adaptive_validation_runs",
        "adaptive_validation_run_samples",
        # Adaptive Collector P3 (ADR-0017 §10, §11; Issue #110 5824551569): the non-canonical
        # shadow owner. The switch history, raw shadow records, the evidence ledger and the
        # evidence windows; no ACTIVE table and no shadow ReviewItem.
        "adaptive_shadow_switch_entries",
        "adaptive_shadow_records",
        "adaptive_evidence_windows",
        "adaptive_evidence_window_events",
        "adaptive_shadow_ledger_events",
        # Gate 3 area 1 (ADR-0018 §3, §3.4, §4): the LIVE grant, the protected-write brake and the
        # durable ASSET upload-attempt owner.
        "live_grants",
        "protected_write_brakes",
        "asset_upload_attempts",
        # Adaptive Collector Phase C C0 (Issue #110 5826469852): capture requests and the sanitized
        # capture candidates of requested runs; never a page body.
        "adaptive_capture_requests",
        "adaptive_capture_candidates",
        "adaptive_phase_c_commands",
        "adaptive_phase_c_command_results",
        "adaptive_phase_c_read_budgets",
        "adaptive_phase_c_reads",
        "adaptive_phase_c_read_refusals",
        # Gate 3 area 2 (ADR-0018 §7, §8): the restore-drill and evidence-retention proofs.
        "restore_drills",
        "retention_proofs",
        "visual_acceptances",
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


# ------------------------------------------------ Phase C C1 PREP-0 (Issue #110 5841947773)


def test_the_phase_c_harness_is_type_checked_in_ci() -> None:
    """``scripts.phasec`` is among the strictly type-checked packages; removing it fails CI."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))["tool"]["mypy"]
    assert "scripts.phasec" in config["packages"]
    assert config["strict"] is True and config.get("warn_unused_ignores") is True


def test_only_the_collection_and_connect_send_points_reach_the_send_guard() -> None:
    """Every accounted send point is a known one: the collection's request budget and its
    guard scope, and CONNECT's fetch and login. Nothing else reserves, and only the collection
    sets a guard."""
    importers: dict[str, set[str]] = {}
    for path, tree in _production_modules().items():
        if "app.core.send_guard" in _imported_modules(tree):
            called = {
                c.func.id
                for c in ast.walk(tree)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            }
            importers[path] = called & {"reserve_send", "guarding"}
    assert importers == {
        "app/collect/collection.py": {"reserve_send", "guarding"},
        "app/connect/service.py": {"reserve_send"},
        "app/collect/shadow.py": set(),
    }, importers
