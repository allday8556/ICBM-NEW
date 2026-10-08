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

from app.capabilities.jobs.models import AttemptOutcome, JobState
from app.interface import cli

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_DIR = REPO_ROOT / "ui" / "web"
V29 = REPO_ROOT / "design" / "prototypes" / "icbm_redesign_test_v29_final.html"
V29_SHA256 = "896ad87011b8615b8a6a9cd3e790ca04f52e908e4ff7b6a26ea4bf5372dfeb82"

UI_SOURCE_OF_TRUTH = REPO_ROOT / "documents" / "contracts" / "ui" / "UI_SOURCE_OF_TRUTH.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
RULES_DIR = REPO_ROOT / "documents" / "rules"
CURRENT_MILESTONE_MD = REPO_ROOT / "documents" / "roadmap" / "CURRENT-MILESTONE.md"
ROADMAP_MD = REPO_ROOT / "documents" / "roadmap" / "ROADMAP.md"
PROTOTYPE_README = REPO_ROOT / "design" / "prototypes" / "README.md"
M0_ACCEPTANCE = REPO_ROOT / "documents" / "acceptance" / "milestones" / "M0.md"
JOB_STATE_ADR = (
    REPO_ROOT / "documents" / "decisions" / "adr" / "0005-durable-job-state-and-attempt-history.md"
)
OWNERSHIP_ADR = (
    REPO_ROOT
    / "documents"
    / "decisions"
    / "adr"
    / "0006-single-data-directory-process-ownership.md"
)
REVIEW_ADR = (
    REPO_ROOT
    / "documents"
    / "decisions"
    / "adr"
    / "0016-gate2-human-review-path-and-review-item-owner.md"
)
ADAPTIVE_ADR = (
    REPO_ROOT
    / "documents"
    / "decisions"
    / "adr"
    / "0017-adaptive-collector-profile-extraction-and-shadow-validation.md"
)
ADAPTIVE_PROPOSAL = (
    REPO_ROOT / "documents" / "archive" / "reviews" / "ADAPTIVE-COLLECTOR-PROPOSAL-BY-CLAUDE.md"
)
LIVE_ADR = (
    REPO_ROOT
    / "documents"
    / "decisions"
    / "adr"
    / "0018-gate3-pre-live-safety-and-bounded-live-authorization.md"
)
TRANSPORT_ADR = (
    REPO_ROOT / "documents" / "decisions" / "adr" / "0019-extension-primary-collection-transport.md"
)
STANDING_ADR = (
    REPO_ROOT / "documents" / "decisions" / "adr" / "0020-roadmap-standing-authorization.md"
)
M5_ACCEPTANCE = REPO_ROOT / "documents" / "acceptance" / "milestones" / "M5.md"
GLOSSARY_MD = REPO_ROOT / "documents" / "architecture" / "GLOSSARY.md"
ARCHITECTURE_MD = REPO_ROOT / "documents" / "architecture" / "ARCHITECTURE.md"
# The owners whose truth a ReviewItem indexes; none of them may read the review owner (G2-02).
REVIEWED_OWNERS = (
    "app/stages/collect/",
    "app/stages/products/",
    "app/stages/register/",
    "app/stages/connect/",
    "integrations/",
)
README_MD = REPO_ROOT / "README.md"
PROTOTYPE_FILE = re.compile(r"icbm_redesign_test_\w+\.html")
PRODUCTION_ROOTS = [REPO_ROOT / "app", REPO_ROOT / "integrations"]

# Production modules that open the SQLite database themselves (ADR-0006 mutation-target
# invariant). Everything else reaches the database through the Container. A new direct opener
# fails the rules below until it is gated and listed here. automation/ and tests/ are not
# production:
# the M0 acceptance script is deliberately independent evidence.
DATABASE_OPENERS = {
    "app/platform/db/database.py": "engine factory",  # defines Database / create_sqlite_engine
    "app/container.py": "require_ownership",  # the application Database
    "app/platform/db/migrations/env.py": "require_ownership",  # schema migrations
    "app/platform/db/migrate.py": "read-only",  # `icbm db current` (mode=ro)
    # Gate 3 area 2 (ADR-0018 §7, §8): reads back, read-only, the schema the shipped migrations
    # build in a private temporary directory — the expected schema, never a data directory.
    "app/platform/db/schema_contract.py": "read-only",
    # Gate 3 area 2 (ADR-0018 §7): the drill reads the active database read-only and writes only
    # the backup into a separate fresh restore root, never a data directory.
    "app/capabilities/live_safety/drill.py": "read-only or fresh restore root",
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
    name = re.search(r"`design/prototypes/(icbm_redesign_test_\w+\.html)`", record)
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
    data = (REPO_ROOT / "design" / "prototypes" / name).read_bytes()
    assert (len(data), hashlib.sha256(data).hexdigest()) == (size, sha)


def test_prototype_readme_mirrors_the_canonical_record() -> None:
    name, sha, size = _canonical_prototype()
    readme = _read(PROTOTYPE_README)
    canonical = _section(readme, r"^Canonical prototype$")
    assert set(PROTOTYPE_FILE.findall(canonical)) == {name}
    assert sha in canonical
    assert f"`{size}`" in canonical
    assert "documents/contracts/ui/UI_SOURCE_OF_TRUTH.md" in readme


def test_the_extension_collector_prototype_is_the_recorded_file() -> None:
    # The side panel's own approved prototype (ADR-0019 §12.1), recorded beside v29 and mirrored by
    # the prototypes README; the repository copy is byte-identical to what the user approved.
    record = _section(_read(UI_SOURCE_OF_TRUTH), r"^Extension Collector visual source$")
    name = re.search(r"`design/prototypes/(icbm_extension_collector\.html)`", record)
    sha = re.search(r"SHA-256:\s*`([0-9a-f]{64})`", record)
    size = re.search(r"Size:\s*`(\d+)`", record)
    assert name and sha and size, "UI_SOURCE_OF_TRUTH lost the Extension Collector record"
    data = (REPO_ROOT / "design" / "prototypes" / name.group(1)).read_bytes()
    assert (len(data), hashlib.sha256(data).hexdigest()) == (int(size.group(1)), sha.group(1))
    mirror = _section(_read(PROTOTYPE_README), r"^Extension Collector prototype$")
    assert name.group(1) in mirror and sha.group(1) in mirror and f"`{size.group(1)}`" in mirror


def test_claude_md_takes_the_ui_source_from_the_record() -> None:
    for path in (CLAUDE_MD, CURRENT_MILESTONE_MD, *sorted(RULES_DIR.glob("*.md"))):
        assert PROTOTYPE_FILE.findall(_read(path)) == [], f"{path.name} hard-codes a prototype file"
    for path, heading in (
        (RULES_DIR / "03-ui-source.md", r"UI source rule"),
        (CURRENT_MILESTONE_MD, r"Current milestone"),
    ):
        text = _section(_read(path), heading)
        assert "documents/contracts/ui/UI_SOURCE_OF_TRUTH.md" in text, heading


# Issue #151 (ADR-0021 §3, §4): the rule bodies CLAUDE.md now imports are its former sections,
# and later reviewed changes stay explicitly pinned. Each digest began as the SHA-256 of the
# pre-migration section (CLAUDE.md
# at main e72a5cad) after the normalization below, which masks locators only: code spans that
# name a path or a file, markdown link targets and bare path tokens. Every rule word must be
# unchanged unless a reviewed rule change updates its digest in the same PR. ADR-0022 and the
# 2026-10-01 validation calibration updated the authority, risk, closeout, contract, PR and LIVE
# scope wording. The digests below are of that reviewed text. §11 (milestone status)
# is pinned by the milestone agreement test instead. documents/rules/README.md is deliberately
# absent: it is the new index (the former intro and §13 restated with moved locators, plus the
# section map), not a preserved body.
_FORMER_CLAUDE_SECTIONS = {
    "01-roles-and-exchange.md": "92a5d9b99bc3db6c4cf6bb7e8d39f74f1c80bb651b73f66010ca875bc66e57aa",
    "02-no-legacy.md": "d31203c3015febe156ef1142992434fc67fbed479107c2eefa5fb31ac1f57ee3",
    "03-ui-source.md": "3a10a40e13552f52cb96e946cd8cdf16a8c500c20b116989649e3d44a69a2de1",
    "04-runtime-stack.md": "23b06ad5483f0dd2e1a86a604c487498de6f22c9778630f07bdb7f886c72a3e3",
    "05-architectural-rules.md": "64110977f367dbe264720f7f1489d313be04b8705a959ae4a0508fabb1df5d7a",
    "06-immutable-domain-rules.md": (
        "7dc72add46c63e5af49bbe515621555b7ffd18a3325650643bc2f0207de663d7"
    ),
    "07-execution-safety.md": "57aebf9eb2dd22a987a29fe86a2f8a901982f58fd265d2c241200e08898c27e4",
    "08-git-conventions.md": "369e8241e18fe0013273f441a5c137dfb73402a23676cf113053087cb3c002c9",
    "09-definition-of-done.md": "58dadd3df8dce8dfae1594b34ddeefb41efcb1a3c05ce656d29bb2b9989b1def",
    "10-working-style.md": "8c685dad839064be75eb77ea075d63fb01757b31a764f96715e6638637473b9d",
    "12-first-vertical.md": "87bf12ed4b3d68da182516ea16046b6a7e7f8db4ad75480f4ae78a38c8d672bf",
}


def _rule_words(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    # a code span is masked only when it is a locator (it names a path or a file); every other span,
    # such as `DRY_RUN | LIVE` or `max(...)`, is a contract term and stays part of the digest
    text = re.sub(
        r"`[^`\n]*(?:/|\.(?:md|py|json|toml|ini|yml|html|js|csv|ps1)\b)[^`\n]*`", "`L`", text
    )
    text = re.sub(r"\]\([^)\s]*\)", "](L)", text)
    text = re.sub(r"(?<![\w`])[\w.-]+(?:/[\w.*<>-]+)+/?", "L", text)
    text = re.sub(r"\n-{3,}\n", "\n", text)
    return " ".join(text.split())


def test_rule_bodies_are_the_former_claude_md_sections() -> None:
    for name, digest in _FORMER_CLAUDE_SECTIONS.items():
        words = _rule_words(_read(RULES_DIR / name))
        assert hashlib.sha256(words.encode()).hexdigest() == digest, name


def test_the_rule_words_detector_masks_only_locators() -> None:
    before = "Read `docs/adr/0001.md` and [x](docs/x.md) under docs/acceptance/ first."
    after = (
        "Read `documents/decisions/adr/0001.md` and [x](../x.md) under documents/acceptance/ first."
    )
    assert _rule_words(before) == _rule_words(after)
    assert _rule_words("never resend CREATE") != _rule_words("always resend CREATE")
    assert _rule_words("mode `DRY_RUN`") != _rule_words("mode `LIVE`")


def test_testpaths_collect_every_test_module() -> None:
    """Issue #151: pyproject testpaths lists the test directories in their pre-restructure order; it
    must still reach every test module, so none is silently dropped from the suite."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    testpaths = [REPO_ROOT / p for p in config["tool"]["pytest"]["ini_options"]["testpaths"]]
    assert all(p.is_dir() for p in testpaths), testpaths
    modules = [
        p
        for p in (REPO_ROOT / "tests").rglob("*.py")
        if p.name.startswith("test_") or p.name.endswith("_test.py")
    ]
    assert modules
    outside = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in modules
        if not any(p.is_relative_to(root) for root in testpaths)
    ]
    assert outside == []


def test_the_s17_ownership_registry_matches_s17() -> None:
    """Appendix A of CAPABILITY_MAPPING.md (the §17 ownership registry, merged by the Issue #151
    reconciliation) names exactly the §17 target IDs, each once and each with an owner — the
    exact-set rule its §4 follow-up asked for."""
    text = _read(REPO_ROOT / "documents/contracts/platforms/smartstore/CAPABILITY_MAPPING.md")
    s17 = _section(text, r"^17\. Implementation enforcement targets")
    targets = [int(n) for n in re.findall(r"^(\d+)\. ", s17, re.M)]
    appendix = text.split("## Appendix A. §17 implementation ownership registry", 1)[1]
    rows = re.findall(r"^\| (\d+) \| [^|]+ \| ([^|]+) \|", appendix, re.M)
    ids = [int(n) for n, _ in rows]
    assert targets == list(range(1, len(targets) + 1)), targets
    assert sorted(ids) == targets and len(ids) == len(set(ids)), ids
    assert all("PR-" in owner for _, owner in rows), rows


def test_the_smartstore_readme_names_exactly_the_adopted_endpoints() -> None:
    """Issue #151 H6: the SmartStore README states the adopted endpoint set that ENDPOINT_MATRIX.md
    §4 owns, and nothing more."""
    smartstore = REPO_ROOT / "documents" / "contracts" / "platforms" / "smartstore"
    matrix = _section(_read(smartstore / "ENDPOINT_MATRIX.md"), r"^4\. Master registry")
    adopted = set(re.findall(r"^\| `(SMARTSTORE_[A-Z0-9_]+)` \| `ADOPTED` \|", matrix, re.M))
    # M2's two, PR-D's two read-backs, IMAGE UPLOAD, the CREATE and the SEARCH adoption slices,
    # the DELETE slice (ADR-0018 §3.5), the leaf-category read and the two notice reads
    # (notice coverage S0), the address-book read (Settings delivery policy), and the two M6-C
    # order reads
    assert len(adopted) == 14, adopted
    boundary = _section(_read(smartstore / "README.md"), r"^2\. Current M2 execution boundary")
    named = boundary.split("Every other SmartStore endpoint")[0]
    assert set(re.findall(r"`(SMARTSTORE_[A-Z0-9_]+)`", named)) == adopted


def test_architecture_cites_the_phase_c_record_status() -> None:
    """Issue #151 H9: the Phase C status in ARCHITECTURE is the acceptance record's own."""
    status = "NOT ACCEPTED — C1 INCOMPLETE / STOPPED"
    record = REPO_ROOT / "documents" / "acceptance" / "adaptive" / "ADAPTIVE-PHASE-C.md"
    assert f"Status: **{status}" in _read(record)
    assert f"ADAPTIVE-PHASE-C.md` ({status};" in _read(ARCHITECTURE_MD)


def test_the_agent_host_protocol_states_its_merged_status() -> None:
    """Issue #151 H10: the protocol says it is canonical, not a candidate. V3 is the ADR-0022
    operating-authority correction of V2."""
    protocol = _read(RULES_DIR / "agent-host" / "AGENT_HOST_AUDIT_PROTOCOL.md")
    status = protocol.split("\n", 3)[2]
    assert status.startswith("Status: **V3 — canonical.**"), status
    assert "canonical candidate" not in protocol.split("## 0.", 1)[0]


AGENT_HOST_DIR = REPO_ROOT / "automation" / "agent-host"
ADR_0022 = REPO_ROOT / "documents" / "decisions" / "adr" / "0022-agent-operating-authority.md"
# The closed list of what stops a loop for the user (ADR-0022 §2). Anything else is technical.
HUMAN_DECISION_CATEGORIES = [
    "NEW_PRODUCT_FEATURE",
    "PRODUCT_DIRECTION_UNDECIDED",
    "BEYOND_USER_REQUIREMENT",
    "LIVE",
    "SUPPLIER_ORDER",
    "RESIDUAL_RISK_APPROVAL",
    "COST",
    "EXTERNAL_DATA_TRANSFER",
    "DESTRUCTIVE",
    "OWNER_HOLD",
]


def _host_script(name: str) -> str:
    return (AGENT_HOST_DIR / name).read_text("utf-8-sig")


def test_the_operating_authority_is_one_rule_in_adr_protocol_and_rule_file() -> None:
    """ADR-0022: the user decides the product and real external actions; the agent runs the loop;
    no human classification gates a packet; and no mechanical check is relaxed."""
    adr = _read(ADR_0022)
    assert adr.splitlines()[2].startswith("Status: **ACCEPTED**")
    for invariant in range(1, 11):
        assert f"OA-{invariant:02d}  " in adr, invariant
    protocol = _read(RULES_DIR / "agent-host" / "AGENT_HOST_AUDIT_PROTOCOL.md")
    rule = _read(RULES_DIR / "14-operating-authority.md")
    for text in (adr, protocol, rule):
        assert "auto_merge" in text and "auto_next" in text
        assert "HUMAN_DECISION_REQUIRED" in text or "Stop for the user only for" in text
    # the default mode
    assert "auto_merge = true\nauto_next  = true" in protocol
    assert "auto_merge = true\nauto_next  = true" in adr
    # no human classification, and history is kept
    assert "**A marked source never holds a packet.**" in protocol
    assert "**No classification record is read.**" in protocol
    assert "never deleted or edited" in adr and "never edited or deleted" in protocol
    # the two hold classes, by category
    assert "HUMAN_DECISION_REQUIRED   the closed list of §0.2." in protocol
    assert "TECHNICAL_HOLD            everything else." in protocol
    assert "never from how often something failed" in protocol
    # nothing in the strong path is relaxed
    kept = _section(protocol, r"0\.2 Operating authority")
    for check in (
        "the exact-HEAD audit and the generated Audit Packet",
        "the packet digest and the two-part audit identity",
        "the same-identity DUAL PASS",
        "`evidence_seen` coverage and the PASS-only cache",
        "FULL CI after READY",
        "the pre-merge packet regeneration and the current-base check",
        "MERGE_GUARD, the merge with `expected_head_sha`, POST_MERGE_VERIFY",
        "every core invariant and protected action owned by rules §6 and §7",
    ):
        assert check in kept, check
    # the authority write guard stays
    assert "no automated actor creates or edits a\nmarker-first body" in protocol
    assert "No automated actor writes a marker-first body" in adr
    # the rule file never asks the user for bookkeeping and keeps §7
    assert "No `[OWNER-AMENDMENT]` and no classification comment is requested." in rule
    assert "§7 owns the exact protected-action list" in rule


def test_validation_strength_is_risk_scoped_without_weakening_live_safety() -> None:
    """The three validation tiers agree across the rule, Host protocol, ADR and CI workflow.

    Final-LIVE evidence is generated on final main rather than churned after provider-zero merges.
    """
    rule = _read(RULES_DIR / "14-operating-authority.md")
    protocol = _read(RULES_DIR / "agent-host" / "AGENT_HOST_AUDIT_PROTOCOL.md")
    adr = _read(ADR_0022)
    ci = _read(REPO_ROOT / ".github" / "workflows" / "ci.yml")
    done = _read(RULES_DIR / "09-definition-of-done.md")
    for text in (rule, protocol, adr):
        assert all(tier in text for tier in ("BASIC", "PROVIDER_ZERO", "HIGH_RISK"))
    roles = _read(RULES_DIR / "01-roles-and-exchange.md")
    assert "single operational assignment" in roles
    assert "stated once in rule §14.1" in adr
    assert "does not restate a\nsecond role contract" in protocol
    assert "still V3, not V4" in protocol
    assert "small internal PR" in done and "feature or milestone closeout" in done
    assert "final main immediately before the\nbounded LIVE action" in rule
    assert "BASIC or PROVIDER_ZERO merge does not rerun or re-record it" in protocol
    standing_authority = _read(
        REPO_ROOT / "documents" / "decisions" / "adr" / "0020-roadmap-standing-authorization.md"
    )
    for text in (rule, protocol, adr, standing_authority):
        assert "Transitional V3" in text or "transitional V3" in text
        assert "current-main" in text or "current main" in text
    assert "NEXT_HOLD=MAIN_NOT_DUAL_PASS_AUDITED" in rule
    assert "NEXT_HOLD=MAIN_NOT_DUAL_PASS_AUDITED" in protocol
    assert "without `-ForceFull`" in rule and "without `-ForceFull`" in protocol
    assert "supervising agent" in rule and "supervising agent" in protocol
    assert "not a merge prerequisite" in rule
    assert "does not retroactively\nmake that PR HIGH_RISK" in protocol
    for mode in ("basic", "provider_zero", "full", "wip"):
        assert mode in ci
    assert "if: needs.scope.outputs.final_live == 'true'" in ci
    assert 'provider_zero) required="SCOPE QUALITY TESTS MIGRATIONS M5"' in ci
    # The required "Tests (<os>)" checks exist in every merge-candidate scope: a matrix job skipped
    # at job level reports one unexpanded name, so the tier gates its steps, never the job.
    tests_job = ci.split("\n  tests:\n", 1)[1].split("\n  migrations:\n", 1)[0]
    assert "if: needs.scope.outputs.mode != 'wip'" in tests_job
    assert (
        "RUN_SUITE: ${{ needs.scope.outputs.mode == 'provider_zero'"
        " || needs.scope.outputs.mode == 'full' }}" in tests_job
    )
    assert tests_job.count("if: env.RUN_SUITE == 'true'") == 4
    assert 'basic) required="SCOPE QUALITY BASIC"' in ci
    # These core owners can never be downgraded to BASIC by a Markdown or generic-code match.
    for protected in (
        "automation/agent-host/*.ps1",
        "app/container.py",
        "app/platform/core/*",
        "app/platform/system/execution_mode.py",
        "app/capabilities/live_safety/*",
        "app/stages/register/*",
        "app/stages/connect/accounts.py",
        "app/stages/connect/account_models.py",
        "app/stages/connect/service.py",
        "app/stages/connect/sessions.py",
        "app/stages/connect/smartstore/models.py",
        "app/stages/connect/smartstore/service.py",
        "app/platform/db/migrations/versions/0006_m2_marketplace_connections.py",
        "app/platform/db/migrations/versions/0016_m5_registration_foundation.py",
        "app/platform/db/migrations/versions/0017_m5_registration_execution_scope.py",
        "app/platform/db/migrations/versions/0026_g3_live_authority.py",
        "app/platform/db/migrations/versions/0029_g3_restore_retention.py",
        "app/platform/db/migrations/versions/0030_g3_visual_acceptance.py",
        "app/platform/db/migrations/versions/0031_m5_registration_reconcile.py",
        "app/platform/db/migrations/versions/0033_m5_canary_eligibility.py",
        "app/platform/db/migrations/versions/0036_g3_residual_risk_acceptance.py",
        "integrations/marketplaces/*",
        "documents/rules/06-immutable-domain-rules.md",
        "documents/rules/07-execution-safety.md",
        "documents/rules/14-operating-authority.md",
        "documents/rules/agent-host/AGENT_HOST_AUDIT_PROTOCOL.md",
        "documents/decisions/adr/0020-*",
        "documents/decisions/adr/0022-*",
    ):
        assert protected in ci
    assert "app/platform/db/migrations/*" not in ci
    provider_zero_exceptions = ci.split('case "$f" in', 1)[1].split(";;", 1)[0]
    assert "integrations/marketplaces/smartstore/readback.py" in provider_zero_exceptions
    assert "integrations/marketplaces/smartstore/create.py" not in provider_zero_exceptions
    assert "no real supplier order" in _host_script("run-lookahead-main-v1.ps1")
    assert "SUPPLIER_ORDER: any real supplier order" in _host_script("run-repair-v1.1.ps1")
    assert "any real supplier order" in _host_script("run-audit-v1.1.ps1")


def test_the_calibrated_rules_remove_process_overhead_without_removing_safety() -> None:
    """The second calibration removes template and approval churn while preserving hard gates."""
    readme = _read(RULES_DIR / "README.md")
    stack = _read(RULES_DIR / "04-runtime-stack.md")
    architecture = _read(RULES_DIR / "05-architectural-rules.md")
    immutable = _read(RULES_DIR / "06-immutable-domain-rules.md")
    execution = _read(RULES_DIR / "07-execution-safety.md")
    git_rules = _read(RULES_DIR / "08-git-conventions.md")
    first_vertical = _read(RULES_DIR / "12-first-vertical.md")
    standing_authority = _read(
        REPO_ROOT / "documents" / "decisions" / "adr" / "0020-roadmap-standing-authorization.md"
    )

    assert "adds no\nsecond stop or approval rule" in readme
    assert "Safety ownership is also singular" in readme
    assert "Compatible\ndependency updates" in stack and "without creating an ADR" in stack
    assert "Site-specific request fields" in architecture
    assert "milestone status, not as a second immutable rule" in immutable
    assert "never blindly resent" in immutable
    assert "per bounded scope, not necessarily per item" in execution
    assert "does not ask again for each item" in execution
    assert (
        "A BASIC\ndocumentation, infrastructure or simple internal PR may stop there" in git_rules
    )
    assert "do not create an ADR\nonly to satisfy a template" in git_rules
    assert "not a requirement\nfor each slice" in first_vertical
    assert "do not execute LIVE merely to keep this proof current" in first_vertical
    assert "routine read-only\n> provider call is not protected by name alone" in standing_authority
    assert "The applicable gate" in standing_authority
    assert "historical\nscope notes, not an active rule" in standing_authority
    assert "routine read-only calls may proceed under rule §7.1" in standing_authority
    assert "only HIGH_RISK requires exact-HEAD DUAL PASS" in standing_authority
    authority = _read(RULES_DIR / "14-operating-authority.md")
    assert "routine read-only provider call" in authority
    assert "Uncertainty alone does not create HIGH_RISK" in authority
    assert "### 14.2.1 Repository-wide application" in authority
    assert "Agent Host is one consumer of\nthe rule, not its boundary" in authority
    assert "non-mutating read-back, health check or lookup is not HIGH_RISK by default" in authority
    assert "material cost" in authority and "changes provider state" in authority
    assert "actual visual, restore and retention proof and durable record" in authority
    assert "never removes focused tests or CI" in authority
    assert "single active owner" in execution
    protocol = _read(RULES_DIR / "agent-host" / "AGENT_HOST_AUDIT_PROTOCOL.md")
    assert "rule §14.4" in protocol and "rule §7.2" in protocol
    assert "material defect in the changed code on a reachable path" in authority


def test_transitional_v3_baseline_recovery_matches_the_existing_runtime() -> None:
    """Lower-tier merges stay lower-tier while current V3 fail-closes auto-next on its baseline."""
    rule = _read(RULES_DIR / "14-operating-authority.md")
    protocol = _read(RULES_DIR / "agent-host" / "AGENT_HOST_AUDIT_PROTOCOL.md")
    adr = _read(ADR_0022)
    standing = _read(
        REPO_ROOT / "documents" / "decisions" / "adr" / "0020-roadmap-standing-authorization.md"
    )
    lookahead = _host_script("run-lookahead-main-v1.ps1")
    full_audit = _host_script("run-full-audit-v1.ps1")
    orchestrator = _host_script("orchestrator-v1.3.ps1")

    assert "[string]$baseline.main -ne $mainHead" in lookahead
    assert '[string]$baseline.status -ne "DUAL_PASS"' in lookahead
    assert 'Write-Output "NEXT_HOLD=MAIN_NOT_DUAL_PASS_AUDITED"' in lookahead
    assert "main = $mainHead" in full_audit and "status = $Status" in full_audit
    assert 'Save-AuditBaseline -Status "BLOCKED"' in full_audit
    assert 'Save-AuditBaseline -Status "DUAL_PASS"' in full_audit
    assert "FULL_AUDIT_RESULT=TECHNICAL_HOLD_INSUFFICIENT" in full_audit
    assert "FULL_AUDIT_RESULT=STALE_MAIN_MOVED" in full_audit
    assert "-Params @{ MergedPr = $CurrentPr }" in orchestrator
    assert "FULL_AUDIT_RESULT=DUAL_PASS" in orchestrator
    assert "FULL_AUDIT_RESULT=BLOCKED" in orchestrator
    assert "FULL_AUDIT_RESULT=TECHNICAL_HOLD_INSUFFICIENT" in orchestrator
    for text in (rule, protocol, adr, standing):
        assert "lower-tier" in text
        assert "DUAL_PASS" in text
    assert "Host does not invoke this standalone audit from the hold" in protocol
    assert "fully unattended Host waits" in protocol
    assert "not a\n  merge prerequisite or HIGH_RISK proof" in adr
    assert "first five classes were decided" in adr
    assert "sixth was added separately by the PR #174" in adr


def test_the_host_stops_for_the_user_only_on_the_closed_category_list() -> None:
    """ADR-0022 OA-01, OA-03: the closed list lives once, in the shared authority helper, and the
    documents name exactly that list."""
    authority = _host_script("agent-host-authority-v2.ps1")
    block = authority.split("$script:HumanDecisionCategories = @(", 1)[1].split(")", 1)[0]
    assert re.findall(r'"([A-Z_]+)"', block) == HUMAN_DECISION_CATEGORIES
    protocol = _read(RULES_DIR / "agent-host" / "AGENT_HOST_AUDIT_PROTOCOL.md")
    for category in HUMAN_DECISION_CATEGORIES[:-1]:
        assert f"`{category}`" in protocol, category
    # the class word alone decides nothing, and neither does a category word inside another reason:
    # only a whole reason in a form the Host composes makes a stop the user's
    assert (
        "'^(?:NEXT_HOLD_|[A-Z][A-Z_]*_HUMAN_DECISION_REQUIRED_)?([A-Z_]+)$'" in authority
        and "$m.Groups[1].Value -cin $script:HumanDecisionCategories" in authority
    )
    assert '$r.Contains("HUMAN_DECISION_REQUIRED")' not in authority
    assert '"(^|[^A-Z0-9])$cat([^A-Z0-9]|`$)"' not in authority
    audit = _host_script("run-audit-v1.1.ps1")
    assert "-not (Get-HumanDecisionCategory $r.Summary)" in audit
    assert "HUMAN_DECISION_WITHOUT_CATEGORY" in audit
    # every script that can stop takes its class from that one helper
    for name in ("orchestrator-v1.3.ps1", "run-repair-v1.1.ps1"):
        script = _host_script(name)
        assert 'agent-host-authority-v2.ps1")' in script, name
        assert "Get-HoldClass" in script, name
    orchestrator = _host_script("orchestrator-v1.3.ps1")
    assert "$class = Get-HoldClass $Reason" in orchestrator
    # no script writes the pre-ADR-0022 hand-off any more
    for path in sorted(AGENT_HOST_DIR.glob("*.ps1")):
        if path.name in {"orchestrator-v1.2.ps1", "run-lookahead-v1.ps1"}:
            continue  # display-only / non-authoritative helpers, identity-pinned and unchanged
        text = path.read_text("utf-8-sig")
        assert '-Status "HUMAN_HOLD"' not in text, path.name
        assert 'Write-Host "HUMAN_HOLD=' not in text, path.name


def test_the_packet_generator_has_no_human_classification_gate() -> None:
    """ADR-0022 OA-04: no classification record is parsed and no marker holds a packet."""
    audit = _host_script("run-audit-v1.1.ps1")
    for gone in (
        "Read-ClassificationRecord",
        "UNCLASSIFIED_MARKED_SOURCE",
        "CLASSIFIED_SOURCE_DIGEST_CHANGED",
        "CLASSIFICATION_CONFLICT",
        "CLASSIFICATION_RECORD_",
        "HOST_MANIFEST_MAY_NOT_CLASSIFY",
        "$scopeLine",
    ):
        assert gone not in audit, gone
    assert "Get-EvidenceReferences $declarationText" in audit
    # declared evidence never silently disappears: a citation nothing holds is a technical hold
    assert 'Add-PacketHold "CITED_SOURCE_UNRESOLVED:$label"' in audit
    # a citation names its kind and resolves only to a source of that kind, never by the id alone
    assert '$citeKey = "$(($it.Locator -split ":", 2)[0]):$($it.Id)"' in audit
    assert "$citedIds" not in audit
    # the user's rule (ADR-0022 §4.1): only six kinds of finding block; the rest are notes
    assert audit.count("WHAT A BLOCKER IS (the user's rule, ADR-0022 §4.1)") == 2
    assert _host_script("run-full-audit-v1.ps1").count("WHAT A BLOCKER IS") == 2
    for kind in (
        "DATA_DAMAGE",
        "DUPLICATE_OR_WRONG_SEND",
        "SECURITY",
        "CORE_BROKEN",
        "CI_CODE_DEFECT",
        "SAFETY_GATE_BYPASS",
    ):
        assert audit.count(kind) >= 2, kind
    assert "### 4.1 What a BLOCKER is" in (
        REPO_ROOT / "documents/decisions/adr/0022-agent-operating-authority.md"
    ).read_text("utf-8")
    # the canon the slice is judged against is in the packet, and an auditor never passes without it
    assert "foreach ($cp in $evidenceRefs.Canon)" in audit and 'Kind = "CANON"' in audit
    # canon is read at the audited base, and the Host's baseline is in every packet: a declaration
    # only adds to it
    assert "foreach ($cp in (Get-BaselineCanon))" in audit
    assert '$locator = "git_blob:base:$cp"' in audit and "git_blob:HEAD:$cp" not in audit
    baseline = _host_script("agent-host-authority-v2.ps1").split("function Get-BaselineCanon", 1)[1]
    assert re.findall(r'"(documents/[^"]+)"', baseline.split("}", 1)[0]) == [
        "documents/roadmap/ROADMAP.md",
        "documents/roadmap/CURRENT-MILESTONE.md",
        "documents/rules/07-execution-safety.md",
        "documents/rules/14-operating-authority.md",
    ]
    assert audit.count("never assume what an unseen document says") == 2
    assert "(review:|review-comment:)?([1-9][0-9]{8,11})" in _host_script(
        "agent-host-authority-v2.ps1"
    )
    assert 'Origin = "referenced"' in audit
    assert 'Write-Output "HOLD_CLASS=TECHNICAL_HOLD"' in audit
    # what V3 keeps in the generator
    for kept in (
        "PACKET_IMMUTABILITY_VIOLATION",
        "STREAM_UNREADABLE",
        "STREAM_TRUNCATED",
        "EVIDENCE_NOT_SEEN",
        '"PACKET_DIGEST=$packetDigest"',
        "function Test-PassCache",
    ):
        assert kept in audit, kept
    # the write guard is untouched
    authority = _host_script("agent-host-authority-v2.ps1")
    assert "AUTHORITY_WRITE_GUARD_MARKER_FIRST_BODY" in authority


def test_the_agent_host_readme_pins_the_committed_script_bytes() -> None:
    """The Agent Host scripts are identified byte for byte: every sha256 in the README's table is
    the hash of the committed file, and every script is in the table."""
    readme = (AGENT_HOST_DIR / "README.md").read_text("utf-8")
    rows = dict(re.findall(r"^\| `([^`]+)` \| [^|]+ \| `([0-9a-f]{64})` \|\r?$", readme, re.M))
    scripts = {
        path.relative_to(AGENT_HOST_DIR).as_posix() for path in AGENT_HOST_DIR.rglob("*.ps1")
    }
    assert scripts <= set(rows), sorted(scripts - set(rows))
    wrong = {
        name: digest
        for name, digest in rows.items()
        if hashlib.sha256((AGENT_HOST_DIR / name).read_bytes()).hexdigest() != digest
    }
    assert wrong == {}


def test_the_merge_path_keeps_every_mechanical_check() -> None:
    """ADR-0022 OA-08: MERGE_GUARD and the merge are as strict as V2, plus base containment and the
    post-merge tree check."""
    orchestrator = _host_script("orchestrator-v1.3.ps1")
    guard = orchestrator.split("function Invoke-MergeGuard {", 1)[1].split(
        "function Invoke-AutoMerge {", 1
    )[0]
    for reason in (
        "GUARD_PR_ON_OWNER_HOLD",
        "GUARD_NOT_DUAL_PASS",
        "GUARD_AUDITED_HEAD_MISMATCH",
        "GUARD_PACKET_DIGEST_MISMATCH",
        "HEAD_MOVED_AFTER_AUDIT",
        "MAIN_MOVED_AFTER_AUDIT",
        "HEAD_BEHIND_BASE",
        "PACKET_DIGEST_CHANGED_AFTER_AUDIT",
        "GUARD_PACKET_INCOMPLETE",
        "GUARD_PR_IS_DRAFT",
        "GUARD_CI_FAILED",
        "GUARD_NOT_MERGEABLE_",
    ):
        assert reason in guard, reason
    _assert_in_order(
        guard,
        [
            "GUARD_NOT_DUAL_PASS",
            "HEAD_MOVED_AFTER_AUDIT",
            "MAIN_MOVED_AFTER_AUDIT",
            "HEAD_BEHIND_BASE",
            "PACKET_DIGEST_CHANGED_AFTER_AUDIT",
            "GUARD_CI_FAILED",
            'Decision = "MERGE"',
        ],
    )
    merge = orchestrator.split("function Invoke-AutoMerge {", 1)[1]
    assert '"-f", "sha=$AuditedHead"' in merge
    assert "--force" not in orchestrator and "force-with-lease" not in orchestrator
    assert "function Test-PostMergeTree" in orchestrator
    assert 'Stop-Hold `\n                -Reason "POST_MERGE_TREE_MISMATCH"' in orchestrator


def test_the_repository_map_counts_match_the_tree() -> None:
    """Issue #151: every files count in REPOSITORY_MAP.md is the number of tracked files under that
    path, so the map cannot drift from the tree unnoticed."""
    import subprocess

    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.splitlines()
    rows = re.findall(
        r"^\| `([^`]+)` \| (\d+) \|",
        _read(REPO_ROOT / "documents" / "reference" / "REPOSITORY_MAP.md"),
        re.M,
    )
    assert rows
    wrong = {
        path: (int(count), sum(1 for f in tracked if f.startswith(path)))
        for path, count in rows
        if int(count) != sum(1 for f in tracked if f.startswith(path))
    }
    assert wrong == {}


def test_claude_md_auto_loads_every_rule_body() -> None:
    """ADR-0021 §3 (Issue #151 §2): the root CLAUDE.md is the bootstrap index.

    It imports every rule body with a Claude Code ``@`` import, so the rules load with it instead of
    being merely named.
    """
    imports = re.findall(r"^@(\S+)$", _read(CLAUDE_MD), re.M)
    assert imports, "CLAUDE.md imports nothing"
    for target in imports:
        assert (REPO_ROOT / target).is_file(), target
    expected = {p.relative_to(REPO_ROOT).as_posix() for p in RULES_DIR.glob("*.md")}
    expected.add(CURRENT_MILESTONE_MD.relative_to(REPO_ROOT).as_posix())
    assert set(imports) == expected
    # Former CLAUDE.md section numbers survive in the rule bodies (PATH_MIGRATION_MAP).
    headings = "\n".join(_read(REPO_ROOT / target) for target in imports)
    for number in range(1, 14):
        assert re.search(rf"^## {number}\. ", headings, re.M), number


def test_roadmap_takes_the_ui_source_from_the_record() -> None:
    section = _section(_read(ROADMAP_MD), r"UI Source of Truth")
    assert "documents/contracts/ui/UI_SOURCE_OF_TRUTH.md" in section
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


# Issue #52 §0 and PR #55 review 5204359614: the milestone status is stated in CLAUDE.md §11
# (now documents/roadmap/CURRENT-MILESTONE.md),
# documents/roadmap/ROADMAP.md §12, the README status and the runtime ``app.MILESTONE`` (health,
# screen meta, UI
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
        _section(_read(CURRENT_MILESTONE_MD), r"Current milestone"), _CLAUDE_MILESTONE
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
        record = REPO_ROOT / "documents" / "acceptance" / "milestones" / f"{milestone}.md"
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
    from app.capabilities.review.service import ReviewKind

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
    from app.stages.collect.facts import EvidenceKind, FieldLevel, FieldStatus

    adr = _read(ADAPTIVE_ADR)
    assert re.search(r"^Status: \*\*ACCEPTED\*\*", adr, re.M)
    for source in ("5302952567", "5812200650", "5812422770"):
        assert source in adr, source
    for canonical in (
        ARCHITECTURE_MD,
        ROADMAP_MD,
        REPO_ROOT / "documents" / "architecture" / "GLOSSARY.md",
    ):
        assert ADAPTIVE_ADR.name in _read(canonical) or "ADR-0017" in _read(canonical), canonical
    # The amended contracts point at their amendment; nothing amends them silently.
    for amended in ("0010-supplier-generic-collect", "0013-m4-canonical-product-contract"):
        (path,) = (REPO_ROOT / "documents" / "decisions" / "adr").glob(f"{amended}*.md")
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
    (collect_adr,) = (REPO_ROOT / "documents" / "decisions" / "adr").glob(
        "0010-supplier-generic-collect*.md"
    )
    levels = _section(_read(collect_adr), r"^7\. Facts: two levels")
    assert "Amendment note (ADR-0017 §4)" in levels and "`COVERAGE`" in levels
    # The six cross-audit items of Issue #110 5812200650 are each closed by a named section.
    closed = _section(adr, r"^14\. The cross-audit items, closed$")
    assert len(re.findall(r"^\| [1-6] \|", closed, re.M)) == 6
    assert closed.count("**yes**") == 2


def test_the_extension_transport_contract_is_recorded_and_pinned() -> None:
    """ADR-0019 (Issue #126 5844537419, E0): the extension-primary COLLECT transport contract,
    contract-only and runtime-zero, before any extension code, endpoint or migration."""
    adr = _read(TRANSPORT_ADR)
    assert re.search(r"^Status: \*\*ACCEPTED\*\*", adr, re.M)
    for source in ("5844537419", "5844538783", "5844496942", "5845042878", "5845080203"):
        assert source in adr, source
    # The canonical documents cite it; the roadmap names the file and the decision.
    roadmap = _read(ROADMAP_MD)
    assert f"`documents/decisions/adr/{TRANSPORT_ADR.name}`" in roadmap and "5844537419" in roadmap
    for canonical in (ARCHITECTURE_MD, GLOSSARY_MD):
        assert "ADR-0019" in _read(canonical) or TRANSPORT_ADR.name in _read(canonical), canonical
    collect_section = _section(_read(ARCHITECTURE_MD), r"^COLLECT$")
    assert "`EXTENSION`" in collect_section and "`DIRECT_URL`" in collect_section
    # Issue #89 5847528940 asked the architecture to state the implementation status beside the
    # target contract. E1 (Issue #126 5906290729, owner amendment 5907095955) moved that status
    # to one click, compare only; E2 (ADR-0019 §10) moves it again: the extension run is recorded
    # by the canonical extractor, through the one pipeline after capture.
    for element in (
        "`EXTENSION`-primary is the accepted ADR-0019 **target contract**",
        "**E2 is implemented: one click, recorded**",
        "**the supplier's canonical extractor is its revision writer**",
        "Both transports write a revision through the one pipeline after capture",
        "**E3 is implemented: the list queue**",
        "every queue read is issued by the server, durably, before it happens",
        "E3's real acceptance passed under the user's grant",
        "**One pipeline after capture (ADR-0019 §2, E2).**",
        "The application gains no CORS",
        "pairing replaces none",
        "A refusal before that point creates no run",
        "never written to the database, a job payload, the filesystem or a log",
        "the capture is never processed twice",
        "never backfilled, never an identity input",
        "**No server product read** is made for an extension run",
        "the browser relays no image bytes",
    ):
        assert element in collect_section, element
    assert "E0 only: contract, runtime zero" not in collect_section
    # ADR-0010 and ADR-0017 are amended by notes at exactly the named sections, text preserved.
    (collect_adr,) = (REPO_ROOT / "documents" / "decisions" / "adr").glob(
        "0010-supplier-generic-collect*.md"
    )
    collect = _read(collect_adr)
    for heading in (
        r"^3\. Port and adapter boundary",
        r"^4\. Request policy and budgets",
        r"^5\. Source reconnaissance",
        r"^8\. Evidence model",
        r"^9\. Source images",
        r"^12\. Extraction identity",
        r"^13\. AI, OCR and marketplace isolation",
    ):
        assert "Amendment note (ADR-0019" in _section(collect, heading), heading
    adaptive = _read(ADAPTIVE_ADR)
    for heading in (
        r"^2\. What is authorized, phase by phase",
        r"^5\. Extraction identity and comparability",
        r"^7\.3 `ValidationSample`",
        r"^10\. One-fetch shadow comparison",
        r"^15\. What this ADR does not decide",
    ):
        assert "Amendment note (ADR-0019" in _section(adaptive, heading), heading
    block = adr.split("\n## Invariants", 1)[1].split("```text", 1)[1].split("```", 1)[0]
    invariants = dict(re.findall(r"^(AC-\d\d)\s+(.*\S)\s*$", block, re.M))
    assert list(invariants) == [f"AC-{n:02d}" for n in range(1, 32)]
    # Two transports, one pipeline, Collection Management kept.
    assert "exactly two acquisition transports" in invariants["AC-01"]
    assert "Collection Management is kept" in invariants["AC-01"]
    assert "never the server-side browser execution" in invariants["AC-02"]
    assert "refused, never defaulted" in invariants["AC-03"]
    # Transport only; the writer and ACTIVE are unchanged.
    assert "never writes the database, a ProductFactsRevision" in invariants["AC-04"]
    assert "only ProductFactsRevision writer" in invariants["AC-05"]
    assert "ACTIVE is forbidden until a separate cutover ADR" in invariants["AC-05"]
    assert "in addition to the existing loopback client and CSRF checks" in invariants["AC-06"]
    assert "never sends cookies, request headers" in invariants["AC-07"]
    # Provenance, never identity; observed evidence still drifts.
    assert "first introduced by this ADR" in invariants["AC-08"]
    assert "never by itself a drift event" in invariants["AC-08"]
    assert "EVIDENCE_DRIFT under the existing rules" in invariants["AC-09"]
    # The capture-topology owner and its fixed order (the C1 correction).
    assert "separate from EPR and PTR" in invariants["AC-10"]
    assert (
        "CollectionProfile keeps owning host, path, query, pacing and transport"
        in (invariants["AC-11"])
    )
    assert "a whole authenticated page is never sent or retained" in invariants["AC-12"]
    assert "security material" in invariants["AC-13"]
    assert "inside the product scope still refuses fail closed" in invariants["AC-13"]
    assert "a browser byte relay is not authorized" in invariants["AC-14"]
    assert "a missing cap refuses fail closed" in invariants["AC-15"]
    assert "No legacy ICBM extension code" in invariants["AC-16"]
    assert "by contract only" in invariants["AC-17"] and "belong to E1" in invariants["AC-17"]
    assert "authorizes no extension code, endpoint, migration" in invariants["AC-18"]
    # Ruling 5845080203: UI ownership and state semantics are contract (section 12).
    assert "only the capture UX" in invariants["AC-19"]
    assert "owns canonical run management" in invariants["AC-20"]
    assert "never collapsed in any UI" in invariants["AC-21"]
    assert "REVIEW is not a run outcome and AUTH is not a field state" in invariants["AC-21"]
    assert "appear only after the server-owned result or read-back" in invariants["AC-22"]
    assert "a JSON download is never the primary handoff" in invariants["AC-22"]
    assert "no image download or source-asset action" in invariants["AC-23"]
    assert "never causes authenticated whole-DOM local persistence" in invariants["AC-24"]
    assert "never an in-app capture button" in invariants["AC-25"]
    assert "no general BrowserCapturePolicy editor" in invariants["AC-26"]
    # The E3 queue contract (section 8.1): discovery reads nothing, the bounds are declared twice,
    # every read is server-issued and durable, a queue stops and never skips forward.
    assert "reads only the operator's already-loaded page" in invariants["AC-27"]
    assert "the server judges every link with check_target" in invariants["AC-27"]
    assert "fully match the supplier's reviewed product path form" in invariants["AC-27"]
    assert "nothing of the list page itself" in invariants["AC-27"]
    assert "CollectionProfile declares its queue limits" in invariants["AC-28"]
    assert "a missing or out-of-range bound refuses before any read" in invariants["AC-28"]
    assert "written durably before the read" in invariants["AC-29"]
    assert "never reissued or retried" in invariants["AC-29"]
    assert "goes on past an item that fails, never reissuing or retrying it" in invariants["AC-30"]
    assert "ordinary EXTENSION run" in invariants["AC-30"]
    assert "never a run outcome" in invariants["AC-31"]
    ui = _section(adr, r"^12\. UI ownership and state semantics")
    assert "`documents/contracts/ui/UI_SOURCE_OF_TRUTH.md` is not changed by this ADR" in ui
    assert "`REVIEW` is not a run outcome, and `AUTH` is not a field state" in ui
    # ADR-0019 §10 (AC-17): admitting EXTENSION belonged to E1, which did it. The policed gateway
    # still sends for an HTTP profile only, and the server-side BROWSER is still not a collection
    # transport.
    from integrations.suppliers.base import SupplierTransport

    assert {t.value for t in SupplierTransport} == {"HTTP", "BROWSER", "EXTENSION"}


def test_the_live_authorization_contract_is_recorded_and_pinned() -> None:
    """ADR-0018 (Gate 3 G3-0): the pre-LIVE safety contract, before any schema or runtime."""
    from app.platform.system.execution_mode import M0_POLICY, ExecutionModeService

    adr = _read(LIVE_ADR)
    assert re.search(r"^Status: \*\*ACCEPTED\*\*", adr, re.M)
    assert "5821078540" in adr
    # The roadmap names the contract file and its kickoff; the other canonical docs cite it.
    roadmap = _read(ROADMAP_MD)
    assert (
        f"contract `documents/decisions/adr/{LIVE_ADR.name}`" in roadmap and "5821078540" in roadmap
    )
    for canonical in (ARCHITECTURE_MD, M5_ACCEPTANCE, GLOSSARY_MD):
        assert "ADR-0018" in _read(canonical), canonical.name
    block = adr.split("\n## Invariants", 1)[1].split("```text", 1)[1].split("```", 1)[0]
    invariants = dict(re.findall(r"^(G3-\d\d)\s+(.*\S)\s*$", block, re.M))
    assert list(invariants) == [f"G3-{n:02d}" for n in range(1, 32)]
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
    # Issue #89 5847528940: adoption never waits for the verdict to be overturned, in any
    # canonical document; each endpoint needs its own separately authorized adoption slice.
    matrix = _read(
        REPO_ROOT / "documents" / "contracts" / "platforms" / "smartstore" / "ENDPOINT_MATRIX.md"
    )
    stale = re.compile(
        r"(NOT_ADOPTED`?\s+until|adoption\s+\*\*only\s+if\*\*)\s+new\s+official\s+evidence", re.I
    )
    for canonical in (
        LIVE_ADR,
        REPO_ROOT
        / "documents"
        / "decisions"
        / "adr"
        / "0014-smartstore-register-idempotency-readback.md",
    ):
        assert not stale.search(_read(canonical)), canonical.name
    assert not stale.search(matrix)
    assert "is not the adoption condition (ADR-0014 §17.2, §28; ADR-0018 §6.1)" in " ".join(
        matrix.split()
    )
    assert "Adoption is not part of it and never waits for the verdict to be overturned" in adr
    assert "no durable upload owner |" not in matrix
    # Architect decision 5845062336: the revised strategy keeps the verdict and adds a risk gate.
    assert (
        "stays INSUFFICIENT for idempotent replay and remote-absence proof" in invariants["G3-14"]
    )
    for element in (
        "never resend while the outcome is unknown, positive-only reconcile and durable"
        " ambiguity isolation",
        "recorded explicit user and architect acceptance of the residual risk",
    ):
        assert element in invariants["G3-30"], element
    assert "never inherits an earlier visual acceptance" in invariants["G3-31"]
    strategy = _section(adr, r"^6\.1 The revised safety strategy")
    for element in (
        "**This is a deliberate change of the canary's safety strategy, not a rewording.**",
        "**Opening any real canary under this contract requires a separate, explicit user and"
        " architect\n  acceptance of this residual risk**",
        "no path needs remote absence",
    ):
        assert element in strategy, element
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
        "selected replay-key identities for ASSET",
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
        ("G3-26", "a started or unresolved UPLOAD_UNKNOWN blocks that exact key"),
        ("G3-26", "never depends on an ADR-0014 §26 scope row"),
        ("G3-27", "needs a new CREATE grant and a fresh restore proof"),
        ("G3-16", "never an ADR-0014 §26 row"),
    ):
        assert element in invariants[key], (key, element)
    owner = _section(adr, r"^3\.4 The durable ASSET upload-attempt owner")
    for element in (
        "**Therefore `ASSET_MUTATION_READY` is necessarily `BLOCKED` whenever that owner is"
        " absent.**",
        # Issue #89 5847528940: area 1 created the owner; the text no longer claims it is missing.
        "The separately authorized Gate 3 area 1 slice (§12) created it, provider-zero",
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
        ("G3-29", "replay fence inspects the whole exact-key scope"),
        ("G3-29", "only NOT_APPLIED_PROVEN clears it for a retry"),
        (
            "G3-29",
            "an APPLIED_PROVEN keeps a fresh upload with that key blocked, while its "
            "sanitizer-safe provider identity may be locally rebound without a provider request "
            "only when canonical account, endpoint, exact content SHA-256 and asset profile all "
            "match",
        ),
        ("G3-16", "upload-attempt state over every selected replay-conflict scope"),
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
        "ASSET restore proof remains valid",
        "**The same key governs `APPLIED_PROVEN`.**",
        "**keeps a fresh upload with that key blocked**",
        "**never re-sent merely because the file name, MIME or type metadata, derivation, local "
        "artifact kind, candidate, preparation, grant, profile or local contract label changed**",
        "The adopted local rebind path may reuse its sanitizer-safe provider identity",
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
        "one ASSET proof binds the immutable inputs of its exact selected-artifact batch",
        "taken after the freeze",
        "which it may never record as absent",
        "a pre-freeze proof never gates a CREATE",
    ):
        assert element in invariants["G3-16"], element
    stages = _section(adr, r"^10\. Mutation-stage readiness")
    assert "**it is a mandatory layer of the send-time" in stages
    assert "**The ASSET stage never depends on a `PREPARED` Intent**" in stages
    assert "**`ASSET_MUTATION_READY` is `BLOCKED` at this main.**" in stages
    assert "exists (area 1), so it is not what blocks" in stages
    assert "§3.4 does not exist" not in " ".join(stages.split())
    assert "but **no implementation**" not in adr
    assert "None of them is implemented at this main" not in adr
    assert "A later slice that implements a grant or the brake adds a migration" not in adr
    assert "added migration `0026_g3_live_authority`" in " ".join(adr.split())
    assert "each artifact is judged on its own replay-conflict scope" in stages
    assert "a started or unresolved `UPLOAD_UNKNOWN` blocks that exact key" in stages
    assert (
        "**The ASSET readiness queries the whole replay-conflict scope of each artifact**" in stages
    )
    assert "another selected key remains independently uploadable" in stages
    safety = _section(adr, r"^4\.3 The whole safety stack")
    assert "the stage's mutation readiness is `READY`" in safety
    assert "**for the CREATE stage only**" in safety
    assert "**The ASSET stage has no §26\n   scope owner" in safety
    assert "`ASSET_MUTATION_READY` before an upload" in safety
    assert "`CREATE_MUTATION_READY` before a CREATE" in safety
    # G3-0 changes no runtime: the M0 policy still refuses LIVE outside a bounded window. M5 was
    # accepted later, only on its exact-main record (2026-10-07, Issue #219 6033126992).
    assert M0_POLICY == "M0_DRY_RUN_ONLY"
    assert "live_writes_permitted=False" in inspect.getsource(ExecutionModeService.state)
    assert "Status: **ACCEPTED**" in _read(M5_ACCEPTANCE).split("\n---", 1)[0]
    assert "authorizes nothing to run" in adr.split("\n---", 1)[0]


def test_the_standing_authorization_orders_every_missing_pre_canary_prerequisite() -> None:
    """ADR-0020 §4 (post-merge audits of main ``a523c55add2b``, ``a10e4b79dbd3`` and
    ``cfb0aa4f3af1``): the standing authorization selects the next slice from this order, so the
    order may never omit a mandatory pre-canary prerequisite. Every one is closed by its own
    slice, and the order records each as closed rather than dropping it: a production ASSET sender
    that can transmit and an executable committed-session read-back, the second half of ADR-0014
    §11's success proof, by the committed-session bearer seam (ROADMAP §14 item 4, under ADR-0022
    §7 — neither was authorized by ADR-0020), and three under their own architect resolutions:
    the ADR-0014 §27 authoring-revision owners (5907626428, ADR-0014 §27.1),
    the durable canary-eligibility owner (5910018106, ADR-0018 §5.1) and the comparison that
    proves published state (5915900049 D1, ADR-0014 §11 amendment note)."""
    from app.capabilities.live_safety.proofs import DurableStageProofs
    from app.stages.register.authoring_revisions import AuthoringRevisionKind
    from app.stages.register.preparation import AUTHORING_REVISIONS_UNOWNED
    from app.stages.register.target_policy import SERVER_OWNED_AUTHORING_REVISIONS
    from integrations.marketplaces.smartstore import readback as smartstore_readback

    adr = _read(STANDING_ADR)
    order = _section(adr, r"^4\. The current order under this ADR")
    flat = " ".join(order.split())
    for element in (
        "mandatory pre-canary prerequisites that no slice has closed",
        "the mutation-stage prerequisites of ADR-0018 §10 and the read-back success proof of"
        " ADR-0014 §11",
        "This ADR authorizes none of them, and none may be skipped",
        "~~the **production ASSET sender** (ADR-0018 §10)~~ — **closed by its own slice**"
        " (ROADMAP §14 item 4, PR #196; ADR-0022 §7)",
        "`app/container.py` wires `SmartStoreAssetSender`",
        "**to the CONNECT owner's read-only committed bearer**",
        "without one it is unavailable and refuses every send (`LIVE_SENDER_NOT_WIRED`)",
        "`ASSET_MUTATION_READY` is a mandatory send-time layer (ADR-0018 §10, G3-19)",
        "a sender that transmits to the provider is not provider-zero",
        "The wired adapter transmits nothing; giving it a committed session is that user decision",
        "**Amendment note (production ASSET sender adapter).**",
        "**not closed**",
        "Three prerequisites are therefore still missing",
        "~~the **durable canary-eligibility owner** (ADR-0018 §5)~~ — **closed by its own slice**"
        " (ADR-0018 §5.1; Issue #89 architect resolution `5910018106`; migration `0033`)",
        "both stages require `CANARY_NON_REGULATED` (ADR-0018 §10, G3-13)",
        "an operator assertion is never it",
        "still unproven for every lineage that has no current `PROVEN_OUTSIDE` record",
        "the eligibility record's data model was explicitly undecided (ADR-0018 §13)",
        "~~the **authoring-revision owners** for the category mapping and the detail composition"
        " (ADR-0014 §27)~~ — **closed by its own slice** (ADR-0014 §27.1; Issue #89 architect"
        " resolution `5907626428`; migration `0032`)",
        "the candidate preflight answered `AUTHORING_REVISIONS_UNOWNED`",
        "each stage's own gate is a mandatory requirement (ADR-0018 §10)",
        "no Snapshot and no Intent could exist",
        "nothing is backfilled",
        'ADR-0014 §27 recorded real owners for both revisions as "a later, separately authorized'
        ' decision"',
        "never this standing authorization",
        "the **executable committed-session read-back** (ADR-0014 §11)",
        "~~the **executable committed-session read-back** (ADR-0014 §11)~~ — **closed by its own"
        " slice** (ROADMAP §14 item 4, PR #196; ADR-0022 §7)",
        "otherwise it is `False`, `verify` refuses",
        "**Amendment note (committed-session bearer seam; ROADMAP §14 item 4, PR #196).**",
        "No row of the second table is still missing",
        "a bearer exists only while CONNECT holds a proven current committed session",
        "~~a **read-back comparison that proves published state** (ADR-0014 §11)~~ — **closed by"
        " its own slice** (ADR-0014 §11 amendment note; Issue #89 architect resolution"
        " `5915900049` D1)",
        "`channelProductDisplayStatusType = ON`",
        "the expected published state `SALE/ON` (`expected_published_state`),"
        " `proves_published_state()` is `True`",
        "`SALE` with `SUSPENSION`, any other sale status and a missing or unreadable half still"
        " prove nothing",
        "which display status ICBM registers was a product decision with no owner",
        "Being provable confirms no registration by itself",
        "**Amendment note (published-state read).**",
        "**Amendment note (published-state proof; Issue #89 `5915900049` D1).**",
        "Two prerequisites are still missing",
        "Closing that row confirms no registration by itself",
        "a CREATE that cannot be read back is never `CONFIRMED`",
        "Neither contract fixes an order among them",
        "all of them precede any canary",
        "Nothing here shortens that remaining work",
        "**Correction note (post-merge full audit of main `a523c55add2b`).**",
        "**Correction note (post-merge full audit of main `a10e4b79dbd3`).**",
        "**Correction note (post-merge full audit of main `cfb0aa4f3af1`).**",
        "This correction grants nothing",
        "**Amendment note (authoring-revision owners slice; Issue #89 `5907626428`).**",
        "Four prerequisites are still",
        "note grants none of them",
        "Closing that row makes no unit `READY` by itself",
        "**Amendment note (canary-eligibility owner slice; Issue #89 `5910018106`).**",
        "Three prerequisites",
        "Closing that row proves no lineage by itself",
    ):
        assert element in flat, element
    # The prerequisites carry no numbered position, so the order above never contradicts the
    # user decision that fixes their order relative to each other.
    assert re.search(r"^\| still-missing prerequisite \|", order, re.M)
    assert not re.search(r"^\|\s*[34]\s*\|", order, re.M)
    assert len(re.findall(r"^\| (?:the|a) \*\*", order, re.M)) == 0
    assert len(re.findall(r"^\| ~~the \*\*production ASSET sender\*\*", order, re.M)) == 1
    assert (
        len(re.findall(r"^\| ~~the \*\*executable committed-session read-back", order, re.M)) == 1
    )
    assert len(re.findall(r"^\| ~~a \*\*read-back comparison that proves", order, re.M)) == 1
    assert len(re.findall(r"^\| ~~the \*\*authoring-revision owners\*\*", order, re.M)) == 1
    assert len(re.findall(r"^\| ~~the \*\*durable canary-eligibility owner\*\*", order, re.M)) == 1
    # The remaining user-decision steps are still listed, and now after those prerequisites.
    assert flat.index("production ASSET sender") < flat.index("the residual-risk acceptance, the")
    block = adr.split("\n## Invariants", 1)[1].split("```text", 1)[1].split("```", 1)[0]
    invariants = dict(re.findall(r"^(SA-\d\d)\s+(.*\S)\s*$", block, re.M))
    assert list(invariants) == [f"SA-{n:02d}" for n in range(1, 11)]
    for element in (
        "never omits a mandatory pre-canary prerequisite of ADR-0018 §10 or ADR-0014 §11",
        "the ADR-0014 §27 authoring-revision owners were closed by their own slice, ADR-0014 §27.1",
        "the durable canary-eligibility owner by its own, ADR-0018 §5.1",
        "the production ASSET sender and the executable committed-session read-back were not"
        " authorized here and were closed by their own slice, ROADMAP §14 item 4 under ADR-0022 §7",
        "a read-back comparison that proves published state by its own, the ADR-0014 §11"
        " amendment note",
        "canary stays BLOCKED until every condition of ADR-0018 §6 and §10 and the ADR-0014 §11"
        " success proof is green",
    ):
        assert element in invariants["SA-10"], element
    # The roadmap order the standing authorization reads carries the same prerequisites,
    # before the user-decision steps, and §14.2 keeps them as LIVE preconditions.
    roadmap = _read(ROADMAP_MD)
    ordering = " ".join(
        roadmap.split("**Standing authorization (ADR-0020", 1)[1]
        .split("\n\n**Registration", 1)[0]
        .split()
    )
    for element in (
        "production ASSET sender",
        "`app/container.py` wires `SmartStoreAssetSender`, `SmartStoreReadback` and the CREATE and"
        " SEARCH seams to the CONNECT owner's committed bearer",
        # ADR-0022 §7, rule §14.5: wiring the session is implementation, and a committed-session
        # read-back is a routine read-only operation; the side-effecting use stays the user's.
        "Wiring both to the existing committed session is implementation, item 4 above",
        "A committed-session read-back is a routine read-only provider operation, not a separate"
        " user approval",
        "a real image upload and a real CREATE are real external actions (ADR-0022 §2 D)",
        "opened as the bounded LIVE action on the final main",
        "~~**The residual-risk acceptance proof** (ADR-0018 §6.1, ADR-0014 §28.7)~~ — **done**"
        " (PR #192, `HIGH_RISK`)",
        "~~**The committed-session bearer seam**~~ — **done** (PR #196, `HIGH_RISK`)",
        "~~**The bounded LIVE runtime transition of ADR-0018**~~ — **done** (PR #197, `HIGH_RISK`)",
        "~~**The user-facing registration read state** (ADR-0014 §28.5, M5-35)~~ — **done**",
        "**executable committed-session read-back**",
        "`READBACK_EXECUTABLE` is unproven",
        "**Pre-canary prerequisite closed — the committed-session bearer seam** (item 4; PR #196,",
        "It is read-only: it never issues, renews or commits a token and never clears a session",
        "A bearer permits no mutation",
        "The read-back is the remaining half of ADR-0014 §11's own success proof",
        "a comparison that proves published state, is closed by its own slice below",
    ):
        assert element in ordering, element
    # The stale classifications are gone (post-ADR-0022 structure audit, F5).
    for stale in (
        "no provider-zero M5 slice remains in this order",
        "the standing authorization does **not** cover any of them",
        "ADR-0022 leaves this unchanged",
    ):
        assert stale not in ordering, stale
    later = "Only then do the residual-risk acceptance, the bounded LIVE grant use,"
    assert later in ordering
    assert ordering.index("ASSET sender") < ordering.index(later)
    # The adapter is recorded as groundwork, never as a closed prerequisite.
    for element in (
        "**Pre-canary groundwork, prerequisite still open — the production ASSET sender adapter**"
        " (ADR-0018 §10 amendment note)",
        "With no committed session it is unavailable, the sender layer refuses"
        " (`LIVE_SENDER_NOT_WIRED`)",
        "anything possibly transmitted is `UPLOAD_UNKNOWN` and is never resent",
        "**It closes no prerequisite**",
        "**Pre-canary prerequisite closed — the published-state proof** (ADR-0014 §11 amendment"
        " note; Issue #89 architect resolution `5915900049` D1;",
        "every CREATE projection registers the SmartStore channel with"
        " `channelProductDisplayStatusType = ON`",
        "Only `SALE/ON` read back states a published state",
        "never buyer visibility or read-after-write timing",
        "the executable read-back and every other prerequisite still refuse",
    ):
        assert element in ordering, element
    assert "**read-back comparison that proves published state**" not in ordering
    assert "`PUBLISHED_STATE_PROVABLE` stays unproven" not in ordering
    assert "prerequisite still open — the published-state read" not in ordering
    assert "`app/container.py` wires `UnwiredAssetSender`" not in ordering
    assert "Pre-canary prerequisite closed — the production ASSET sender" not in ordering
    # "After those two" still refers to the two adoption slices: every recorded slice follows it.
    assert ordering.index("After those two, the mandatory") < ordering.index(
        "**Pre-canary prerequisite closed"
    )
    # The closed prerequisite is recorded as closed, with what it does not grant.
    for element in (
        "**Pre-canary prerequisite closed — the authoring-revision owners** (ADR-0014 §27.1;",
        "architect resolution `5907626428`; migration `0032`",
        "outside the standing authorization",
        "nothing is backfilled",
        "No provider call, no session, no LIVE, no canary; every other prerequisite below still"
        " refuses",
    ):
        assert element in ordering, element
    assert "authoring-revision owners** of ADR-0014 §27 for" not in ordering
    for element in (
        "**Pre-canary prerequisite closed — the canary-eligibility owner** (ADR-0018 §5.1;",
        "architect resolution `5910018106`; migration `0033`",
        "`CANARY_NON_REGULATED` is proven only for the exact lineage whose current record is"
        " `PROVEN_OUTSIDE`",
        "an operator assertion is never evidence",
        "It is never a `COMPLIANCE PASS`, adds nothing to `CategoryMetadata` and implements no"
        " ComplianceGate",
    ):
        assert element in ordering, element
    assert "canary-eligibility owner** of ADR-0018 §5 (" not in ordering
    preconditions = " ".join(_section(roadmap, r"^14\.2 Preconditions").split())
    for element in (
        "mutation-stage prerequisites of ADR-0018 §10 that no slice has closed",
        "a **production ASSET sender** for the ASSET stage that can transmit"
        " (`SmartStoreAssetSender`",
        "reads the CONNECT owner's committed bearer since §14 item 4: without a proven current"
        " committed session it refuses every send with `LIVE_SENDER_NOT_WIRED`",
        "which is not authorized by the ADR-0020 standing authorization",
        "The **durable canary-eligibility owner** (ADR-0018 §5) is closed by its own slice"
        " (ADR-0018 §5.1)",
        "`CANARY_NON_REGULATED` still has to be proven for the exact canary lineage by a current"
        " `PROVEN_OUTSIDE` record",
        "**owners for the category-mapping and detail-composition authoring revisions** — is"
        " closed by its own slice (ADR-0014 §27.1)",
        "still has to be `READY` on every other rule",
        "the **read-back success proof of ADR-0014 §11**, of which both halves are closed by their"
        " own slices",
        "an **executable committed-session read-back**",
        "a **comparison that proves published state**, is closed by its own slice (ADR-0014 §11"
        " amendment note; architect resolution `5915900049` D1)",
        "A CREATE that cannot be read back and compared is never `CONFIRMED`",
    ):
        assert element in preconditions, element
    # The runtime facts behind the rows. The ASSET sender is the adopted upload; without a
    # committed session it is unavailable (production now hands it the committed bearer).
    # The authoring-revision owner exists and is wired;
    # a client still names neither revision. The eligibility owner exists and is the only thing
    # the durable proof source reads.
    from integrations.marketplaces.smartstore.assets import SmartStoreAssetSender
    from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller

    sender = SmartStoreAssetSender(SmartStoreEndpointCaller(), bearer=lambda: None)
    assert sender.endpoint_adopted() is True and sender.available() is False
    assert (
        inspect.getsource(DurableStageProofs.canary_non_regulated)
        .rstrip()
        .endswith("return self._eligibility.proven(stage, unit_ref, binding)")
    )
    assert "CanaryEligibilityService(" in _read(REPO_ROOT / "app" / "container.py")
    assert "SmartStoreAssetSender(" in _read(REPO_ROOT / "app" / "container.py")
    assert AUTHORING_REVISIONS_UNOWNED == "AUTHORING_REVISIONS_UNOWNED"
    assert SERVER_OWNED_AUTHORING_REVISIONS == (
        "category_mapping_revision",
        "detail_composition_revision",
    )
    assert [kind.value for kind in AuthoringRevisionKind] == [
        "CATEGORY_MAPPING",
        "DETAIL_COMPOSITION",
    ]
    assert "AuthoringRevisionStore(db, clock, audit)" in _read(REPO_ROOT / "app" / "container.py")
    # And ADR-0014 §11's two halves: the read-back reads the committed bearer, and the comparison
    # can prove the published state (5915900049 D1).
    # ROADMAP §14 item 4: the seams read one canonical bearer source, the CONNECT owner's
    # read-only committed bearer; none is a hard-coded absent session any more.
    container = _read(REPO_ROOT / "app" / "container.py")
    assert "committed_bearer = smartstore.committed_bearer" in container
    # The CREATE, read-back, SEARCH and ASSET seams, and the DELETE slice's sender and read-back
    # (ADR-0018 §3.5), the notice-schema capture (notice coverage S0) and the M6-A listing-state
    # sync's read-back (ADR-0023 §3): eight seams, one source.
    assert container.count("bearer=committed_bearer") == 8
    assert "bearer=lambda: None" not in container
    assert smartstore_readback.proves_published_state() is True
    assert smartstore_readback.reads_published_state() is True
    # A Snapshot that cannot be projected still expects nothing, so nothing is proven for it.
    assert smartstore_readback.expected_published_state({}).display_status is None


# ---------------------------------------------------------------- Gate 3 area 1 (ADR-0018 §12)

LIVE_OWNER = "app/capabilities/live_safety/store.py"
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
    never asserted. The one writer records only what ``app.capabilities.live_safety.visual``
    verified, and the only
    caller of that recorder is the ``icbm live record-visual-acceptance`` command: no route, page
    or other module can record or assert a visual acceptance."""
    modules = _production_modules()
    writers = {
        path
        for path, tree in modules.items()
        for call in _calls(tree)
        if _callee(call) == "record_visual_acceptance"
    }
    assert writers == {"app/capabilities/live_safety/visual.py"}
    recorders = {
        path
        for path, tree in modules.items()
        for call in _calls(tree)
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "record"
        and isinstance(call.func.value, ast.Attribute)
        and call.func.value.attr == "visual_acceptance"
    }
    assert recorders == {"app/interface/cli.py"}
    importers = {
        path
        for path, tree in modules.items()
        if "app.capabilities.live_safety.visual" in _imported_modules(tree)
    }
    assert importers <= {"app/container.py", "app/capabilities/live_safety/proofs.py"}, importers
    for path, tree in modules.items():
        if path.startswith("app/interface/api/"):
            assert not [
                m for m in _imported_modules(tree) if m.startswith("app.capabilities.live_safety")
            ], path
    for page in (REPO_ROOT / "ui/web").rglob("*.js"):
        text = page.read_text("utf-8")
        assert "visual-acceptance" not in text and "record-visual" not in text, page


def test_the_residual_risk_acceptance_has_one_recording_path() -> None:
    """ADR-0018 §6.1, G3-30: the store's one writer of ``residual_risk_acceptances`` is called only
    by the residual-risk owner, whose ``record`` is called only by the ``icbm live
    record-residual-risk-acceptance`` command: no route, page or other module records or asserts
    an acceptance, and the stage proofs only read it."""
    modules = _production_modules()
    writers = {
        path
        for path, tree in modules.items()
        for call in _calls(tree)
        if _callee(call) == "record_residual_risk_acceptance"
    }
    assert writers == {"app/capabilities/live_safety/residual_risk.py"}
    recorders = {
        path
        for path, tree in modules.items()
        for call in _calls(tree)
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "record"
        and isinstance(call.func.value, ast.Attribute)
        and call.func.value.attr == "residual_risk"
    }
    assert recorders == {"app/interface/cli.py"}
    importers = {
        path
        for path, tree in modules.items()
        if "app.capabilities.live_safety.residual_risk" in _imported_modules(tree)
    }
    assert importers <= {"app/container.py", "app/capabilities/live_safety/proofs.py"}, importers
    for page in (REPO_ROOT / "ui/web").rglob("*.js"):
        text = page.read_text("utf-8")
        assert "residual-risk" not in text and "residual_risk" not in text, page
    # The stage proofs answer only from the owner, for the stage's own account.
    proofs = _read(REPO_ROOT / "app" / "capabilities" / "live_safety" / "proofs.py")
    assert "return self._residual_risk.accepted(marketplace_key, marketplace_account_id)" in proofs
    assert (
        "return False" not in proofs.split("def residual_risk_accepted", 1)[1].split("def ", 1)[0]
    )


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
        "app.capabilities.audit",
        "app.stages.connect.accounts",
        "app.platform.core.clock",
        "app.platform.core.errors",
        "app.platform.core.execution",
        "app.platform.db.base",
        "app.platform.db.database",
        # Gate 3 area 2: the expected schema the shipped migrations build (drill and retention).
        "app.platform.db.schema_contract",
        "app.platform.db.types",
        "app.capabilities.live_safety",
        "app.stages.products.image_model",
        "app.stages.products.model",
        "app.stages.register.model",
        "app.stages.register.preparation",
        "app.stages.register.sanitize",
        "app.stages.register.store",
    )
    modules = {
        p: t
        for p, t in _production_modules().items()
        if p.startswith("app/capabilities/live_safety/")
    }
    assert {
        "app/capabilities/live_safety/stack.py",
        "app/capabilities/live_safety/assets.py",
        LIVE_OWNER,
    } <= set(modules)
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            assert any(name == a or name.startswith(f"{a}.") for a in allowed), f"{path}: {name}"


def test_the_container_wires_the_deny_by_default_stack_and_a_sessionless_sender() -> None:
    """At this main the stack reads the M0 execution-mode owner; the CREATE owner is wired to that
    stack, and the ASSET path to the adopted sender, whose bearer is the CONNECT owner's read-only
    committed bearer (ROADMAP §14 item 4). A bearer permits nothing: the stack still refuses."""
    tree = ast.parse((REPO_ROOT / "app/container.py").read_text("utf-8"))
    (stack,) = _calls(tree, "SafetyStack")
    mode, proofs = _keyword(stack, "mode"), _keyword(stack, "proofs")
    assert isinstance(mode, ast.Name) and mode.id == "execution_mode"
    # Area 2: the restore and retention proofs are durable owners. Area 3: visual acceptance is the
    # reviewed record of exactly the running code (its digest, taken once at composition) at the
    # current head. Eligibility (§5.1) is the durable owner's answer for the exact lineage the
    # stack derived, never a constant.
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
    source = importlib.import_module("app.capabilities.live_safety.proofs").DurableStageProofs
    eligibility = _keyword(durable, "eligibility")
    assert isinstance(eligibility, ast.Name) and eligibility.id == "canary_eligibility"
    proven = inspect.getsource(source.canary_non_regulated)
    assert proven.rstrip().endswith("return self._eligibility.proven(stage, unit_ref, binding)")
    recorded = inspect.getsource(source.visual_acceptance_recorded)
    assert recorded.rstrip().endswith("return self._visual.recorded()")
    owner = inspect.getsource(
        importlib.import_module("app.capabilities.live_safety.visual").VisualAcceptanceService
    )
    assert "unit.visual_accepted(sha, self._code(), head)" in owner
    assert 'if not sha or sha != report["code_sha"]:' in owner
    (execution,) = _calls(tree, "RegistrationExecutionService")
    authority = _keyword(execution, "authority")
    assert isinstance(authority, ast.Name) and authority.id == "safety_stack"
    (uploads,) = _calls(tree, "AssetUploadService")
    sender = _keyword(uploads, "sender")
    assert isinstance(sender, ast.Call) and _callee(sender) == "SmartStoreAssetSender"
    # Like every provider seam production wires, its bearer is the one canonical source.
    bearer = _keyword(sender, "bearer")
    assert isinstance(bearer, ast.Name) and bearer.id == "committed_bearer"
    for seam in (
        "SmartStoreCreateSender",
        "SmartStoreReadback",
        "SmartStoreReconcileLookup",
        "SmartStoreDeleteSender",
    ):
        for built in _calls(tree, seam):
            source = _keyword(built, "bearer")
            assert isinstance(source, ast.Name) and source.id == "committed_bearer", seam
    connect = inspect.getsource(
        importlib.import_module(
            "app.stages.connect.smartstore.service"
        ).SmartStoreConnectService.committed_bearer
    )
    # Read-only: it never issues, renews or commits a token, and never clears a session.
    for forbidden in (
        "self._session_for(",
        "self._issue(",
        "self._commit(",
        "self._sessions.load(",
        "self._sessions.save(",
        "self._sessions.clear(",
    ):
        assert forbidden not in connect, forbidden
    assert "self._sessions.peek(KEY)" in connect
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
        # The one production sender is the SmartStore adapter's; the capability defines none.
        expected = (
            ["SmartStoreAssetSender"]
            if path == "integrations/marketplaces/smartstore/assets.py"
            else []
        )
        assert senders == expected, (path, senders)
    wired = inspect.getsource(
        importlib.import_module("integrations.marketplaces.smartstore.assets").SmartStoreAssetSender
    )
    # Without a session it is unavailable and proves that nothing left the process.
    assert "return self.endpoint_adopted() and self._bearer() is not None" in wired
    assert "raise TransmissionPrecluded(SESSION_UNAVAILABLE)" in wired


def test_no_application_module_constructs_the_image_upload_adapter() -> None:
    """The one-artifact adapter is constructed by no application module: the upload endpoint is
    reached only through the production ASSET sender, behind the attempt owner and the stack."""
    users = {
        path for path, tree in _production_modules().items() if _calls(tree, "ImageUploadAdapter")
    }
    assert users == set()


def test_the_asset_replay_key_is_the_wire_boundary_and_the_fence_is_in_the_schema() -> None:
    """G3-28, G3-29: four key fields; the partial unique index fences every non-proven state."""
    from app.capabilities.live_safety.model import (
        FENCING_UPLOAD_STATES,
        UploadAttemptState,
        replay_key,
    )
    from app.capabilities.live_safety.models import AssetUploadAttempt

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
    migration = (
        REPO_ROOT / "app/platform/db/migrations/versions/0026_g3_live_authority.py"
    ).read_text("utf-8")
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
        "app/stages/register/store.py",
    ),
    **dict.fromkeys(
        ("RegistrationTargetPolicy", "RegistrationTargetPolicyRevision",
         "RegistrationTargetPolicyCurrent"),
        "app/stages/register/target_policy.py",
    ),
    **dict.fromkeys(
        ("RegistrationCategoryMetadata", "RegistrationCategoryMetadataRevision",
         "RegistrationCategoryMetadataCurrent"),
        "app/stages/register/category_metadata.py",
    ),
    **dict.fromkeys(
        ("ProductGroup", "ProductItem", "GroupMember", "GroupMembershipRevision",
         "ListingComposition", "SourceBinding", "SourceProduct", "CurrentSourceRevisionMove",
         "QuantityOffer"),
        "app/stages/products/store.py",
    ),
    **dict.fromkeys(
        ("PricingSnapshot", "CurrentPricingSnapshotMove"), "app/stages/products/pricing_store.py"
    ),
    **dict.fromkeys(
        ("DerivedImageArtifact", "DerivedImageDerivation", "DerivedImageDerivationInput",
         "DerivedImageDerivationRoot", "ImageSelectionRevision", "ImageSelectionOutput",
         "ImageSelectionSourceDecision", "CurrentImageSelectionMove", "ImageQaResult"),
        "app/stages/products/image_store.py",
    ),
    **dict.fromkeys(("ProductFactsRevision", "ProductFactsField", "ProductFactsEvidence",
                     "ProductFactsImageRef"), "app/stages/collect/revisions.py"),
    "SourceAsset": "app/stages/collect/assets.py",
    **dict.fromkeys(("MarketplaceAccount", "SellerEntity"), "app/stages/connect/accounts.py"),
    **dict.fromkeys(("MarketplaceCapability", "MarketplaceWorkflowOverlay"),
                    "app/stages/connect/marketplace/service.py"),
    "MarketplaceConnection": "app/stages/connect/smartstore/service.py",
    **dict.fromkeys(("LiveGrant", "ProtectedWriteBrake", "AssetUploadAttempt", "RestoreDrill",
                     "RetentionProof", "VisualAcceptance"),
                    "app/capabilities/live_safety/store.py"),
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
        "app/stages/products/store.py": (
            "app/stages/products/materialization.py",
            "app/stages/products/service.py",
        ),
        "app/stages/products/pricing_store.py": ("app/stages/products/pricing.py",),
        "app/stages/products/image_store.py": ("app/stages/products/images.py",),
        "app/stages/collect/revisions.py": (
            "app/stages/collect/revisions.py",
            "app/stages/collect/collection.py",
        ),
        "app/stages/collect/assets.py": (
            "app/stages/collect/assets.py",
            "app/stages/collect/collection.py",
        ),
    }
    modules = _production_modules()
    # A driver that names no production module would be skipped silently, so each must exist.
    assert {owner for owners in drivers.values() for owner in owners} <= set(modules)
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
    tree = ast.parse((REPO_ROOT / "app/capabilities/live_safety/drill.py").read_text("utf-8"))
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
    tree = ast.parse((REPO_ROOT / "app/capabilities/live_safety/drill.py").read_text("utf-8"))
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
    for path in (
        "app/capabilities/live_safety/drill.py",
        "app/capabilities/live_safety/retention.py",
    ):
        tree = ast.parse((REPO_ROOT / path).read_text("utf-8"))
        texts = [node.value for node in _code_strings(tree)]
        assert not [text for text in texts if write.search(text)], path
        assert not _calls(tree, "add") and not _calls(tree, "delete"), path
        assert not _calls(tree, "commit"), path


def test_no_production_code_deletes_protected_evidence() -> None:
    """ADR-0018 §8: no automatic deletion of canary-scope evidence is authorized."""
    from app.capabilities.live_safety.retention import PROTECTED_TABLES

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
            # The review owner is ``app.capabilities.review`` (B-UX2: the prefix this check matched
            # before named no package, so it could never fail).
            offenders += [
                f"{module}: {n}" for n in names if n.split(".")[:3] == REVIEW_OWNER_PACKAGE
            ]
    assert offenders == []


REVIEW_OWNER_PACKAGE = ["app", "capabilities", "review"]


def test_the_review_owner_package_is_the_one_the_boundary_checks() -> None:
    """The G2-02 check names a package that exists, so a reviewed owner importing it is caught."""
    assert (REPO_ROOT / Path(*REVIEW_OWNER_PACKAGE) / "owner.py").is_file()
    tree = ast.parse("from app.capabilities.review.model import ReviewItem")
    (node,) = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert (node.module or "").split(".")[:3] == REVIEW_OWNER_PACKAGE


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
    assert deciders <= {"app/platform/core/ownership.py"}


# ---------------------------------------------------------------- the connection owner

# Issue #52 comments 5687814715 and 5688150031: ICBM-NEW decides its own data root in one place,
# and CONNECT alone owns the logins, the connection state and the sessions. No other code may
# redefine where that owner lives, or stand up a second one.
DATA_ROOT_RESOLVER = "app/config.py"
_CODE_ROOTS = (REPO_ROOT / "app", REPO_ROOT / "integrations", REPO_ROOT / "automation")
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
M3_HARNESS = REPO_ROOT / "automation" / "acceptance" / "m3" / "recon"
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
    icbm = ast.parse((REPO_ROOT / "app" / "interface" / "cli.py").read_text("utf-8"))
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
    for path in (README_MD, REPO_ROOT / "documents" / "architecture" / "ARCHITECTURE.md"):
        text = _read(path)
        assert "app/config.py" in text and r"%USERPROFILE%\ICBM-NEW\data" in text, path.name
        assert "runtime/owner.lock" in text, path.name
        for retired in (r"%LOCALAPPDATA%\ICBM-NEW", "XDG_DATA_HOME", r"var\icbm.db", "var/icbm.db"):
            assert retired not in text, (path.name, retired)


# PR #64 review 5217542767 §3: the local scan reads cookie material, never a session.
SCAN_BOUNDARY = "session_cookies_for_scan"
SCAN_BOUNDARY_CALLERS = {"app/stages/connect/service.py", "automation/acceptance/m3/recon/cli.py"}


def test_the_local_scan_boundary_is_not_a_session_transport() -> None:
    files = _code_files()
    service = files["app/stages/connect/service.py"]
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
    "app.stages.collect.facts",
}
# Issue #52 ruling 5702780630 P2: a supplier parser turns immutable documents into facts. It never
# executes, retries, hashes, stores, persists or schedules anything — those stay in COLLECT core.
PARSER_MAY_NOT_OWN = {
    "app.stages.collect.assets",
    # Stage-B2 (ruling 5706133893): the run, its durable result and the job stay in COLLECT core.
    "app.stages.collect.collection",
    "app.stages.collect.runs",
    "app.capabilities.jobs.registry",
    "app.stages.collect.revisions",
    "app.stages.collect.sourceassets",
    "app.stages.collect.readback",
    "app.platform.core.egress",
    "app.platform.db.database",
    "app.capabilities.jobs.service",
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
    "app/platform/core/egress.py": "resolves the granted hosts' addresses for the guard (socket)",
    "app/platform/core/ownership.py": "hostname for the diagnostic owner metadata (socket)",
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
        if path.startswith(("app/stages/collect/", "integrations/suppliers/"))
        and not path.startswith("integrations/suppliers/transport/")
        and path
        != "app/stages/collect/urls.py"  # the persisted-URL sanitizer, not a fetch decision
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
    # production collection builds allows exactly one product read — except the budget of a run
    # whose document the operator's browser captured (ADR-0019 E2), which allows none.
    tree = ast.parse((REPO_ROOT / "app/stages/collect/collection.py").read_text("utf-8"))
    budgets = {
        function.name: call
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for call in _calls(function)
        if _callee(call) == "RunBudget"
    }
    everywhere = [
        call
        for _, module in _production_modules().items()
        for call in _calls(module)
        if _callee(call) == "RunBudget"
    ]
    assert len(everywhere) == len(budgets), "a collection budget is built by the collection owner"
    allowed = {}
    for name, call in budgets.items():
        reads = {
            keyword.arg: keyword.value
            for keyword in call.keywords
            if keyword.arg == "max_product_reads"
        }
        value = reads.get("max_product_reads")
        assert isinstance(value, ast.Constant), ast.dump(call)
        allowed[name] = value.value
    assert allowed == {"collect": 1, "record_captured_document": 0}


def test_the_collection_job_belongs_to_collect_core() -> None:
    # The durable job is generic: a supplier contributes site knowledge, never a job type.
    from app.stages.collect.collection import COLLECT_PRODUCT_JOB

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
            ("app/stages/connect/", "integrations/suppliers/", "integrations/marketplaces/")
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
SOURCE_TRUTH_ROOTS = ("app/stages/collect/", "integrations/suppliers/")
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
    # app.ai is a reserved AI namespace that has never existed; it is not a moved package.
    "app.ai",
    # ADR-0026 (AIF-02): the AI capability, its prompt registry and later its provider port.
    "app.capabilities.ai",
    # ADR-0026 (AIF-02): PRODUCT DB's enrichment owner and its job.
    "app.stages.products.enrichment",
    "integrations.ai",
    "integrations.marketplaces",
    "app.stages.connect.marketplace",
    "app.stages.connect.smartstore",
)


def test_collect_source_truth_path_imports_no_ai_ocr_or_marketplace_code() -> None:
    modules = {p: t for p, t in _production_modules().items() if p.startswith(SOURCE_TRUTH_ROOTS)}
    assert {"app/stages/collect/service.py", "integrations/suppliers/kmretail/__init__.py"} <= set(
        modules
    )
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            forbidden = [f for f in SOURCE_TRUTH_FORBIDDEN if name == f or name.startswith(f"{f}.")]
            assert not forbidden, f"{path}: {name}"


# ADR-0026 AIF-11 (ADR-0012 consequences): no AI vendor SDK is imported anywhere in production code
# until the owner decides the provider, model, key, cost cap and data transfer. The port's only
# binding is NoAIProvider.
AI_VENDOR_SDKS = (
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
)


def test_no_production_module_imports_an_ai_vendor_sdk() -> None:
    modules = _production_modules()
    assert "app/capabilities/ai/provider.py" in modules
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            vendor = [f for f in AI_VENDOR_SDKS if name == f or name.startswith(f"{f}.")]
            assert not vendor, f"{path}: {name}"


def test_the_register_owner_imports_no_ai_module() -> None:
    """ADR-0026 AIF-4: the register owner reads enrichment results only through the read the
    composition root hands it, so an acceptance run that loads the register owners loads no AI
    module (the harness guards forbid them)."""
    forbidden = ("app.capabilities.ai", "app.stages.products.enrichment")
    for path, tree in _production_modules().items():
        if not path.startswith("app/stages/register/"):
            continue
        for name in _imported_modules(tree):
            assert not any(name == f or name.startswith(f"{f}.") for f in forbidden), (
                f"{path}: {name}"
            )


def test_the_container_binds_no_ai_provider() -> None:
    source = (REPO_ROOT / "app" / "container.py").read_text("utf-8")
    assert "AIExecution(NoAIProvider())" in source
    providers = [
        node.name
        for path, tree in _production_modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name.endswith("Provider")
        and path.startswith("app/capabilities/ai/")
    ]
    assert providers == ["AIProvider", "NoAIProvider"]


# ADR-0017 P1 (Issue #110 5821999699): no dynamic-import or code-evaluation escape in the source
# truth path, so an import rule can never be walked around at run time.
DYNAMIC_ESCAPES = frozenset({"__import__", "exec", "eval", "compile", "__builtins__"})


def test_production_code_has_no_dynamic_import_escape() -> None:
    # P1 held this for the source-truth path; P2 wires persistence into ``app/`` (Issue #110
    # 5822923514 carry-forward 1), so it now holds for every production module.
    modules = _production_modules()
    assert {
        "app/stages/collect/adaptive/engine/engine.py",
        "app/stages/collect/adaptive/store/store.py",
    } <= set(modules)
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
ADAPTIVE_ROOT = "app/stages/collect/adaptive/engine/"
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
ADAPTIVE_MAY_IMPORT = frozenset({"pydantic", "app.stages.collect.facts"})


def test_the_adaptive_core_imports_only_pure_modules() -> None:
    modules = {p: t for p, t in _production_modules().items() if p.startswith(ADAPTIVE_ROOT)}
    assert f"{ADAPTIVE_ROOT}validation.py" in modules
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            allowed = (
                name in ADAPTIVE_STDLIB
                or name in ADAPTIVE_MAY_IMPORT
                or name == "app.stages.collect.adaptive.engine"
                or name.startswith("app.stages.collect.adaptive.engine.")
            )
            assert allowed, f"{path}: {name}"


# ADR-0017 P2 (Issue #110 5822024807): the persistence owner may reach the database, the clock,
# the error vocabulary, the pure core and the supplier registry (its gate) — and nothing that
# acts: no network, browser, gateway, session, COLLECT runtime, review or audit owner.
ADAPTIVE_STORE_ROOT = "app/stages/collect/adaptive/store/"
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
        "app.platform.core.clock",
        "app.platform.core.errors",
        "app.platform.db.base",
        "app.platform.db.database",
        "app.platform.db.types",
        "integrations.suppliers.base",
        "integrations.suppliers.collection",
        "integrations.suppliers.registry",
    }
)


ADAPTIVE_PACKAGES = (
    "app.stages.collect.adaptive.engine",
    "app.stages.collect.adaptive.store",
    "app.stages.collect.adaptive.shadow",
    "app.stages.collect.adaptive.phase_c_capture",
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
        "production Adaptive code is written fresh under app/stages/collect/adaptive/. "
        "Remove the "
        "directory (a leftover local checkout, or a copy) rather than weakening this rule."
    )


# ADR-0017 P3 (Issue #110 5824551569): the shadow owner may read the canonical run and fact
# models and the canonical side of the shadow seam, and write only its own tables. It may not
# import a canonical writer (the revision store, source-asset recorder, run store, collection,
# review, product, audit or job owners), a transport, a session or anything that reaches a network
# (S1, S4, S6).
ADAPTIVE_SHADOW_ROOT = "app/stages/collect/adaptive/shadow/"
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
        "app.platform.core.clock",
        "app.platform.core.errors",
        "app.platform.db.base",
        "app.platform.db.database",
        "app.platform.db.types",
        "app.stages.collect.facts",
        "app.stages.collect.urls",
        "app.stages.collect.models",
        "app.stages.collect.shadow",
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
            and node.module in ("app.stages.collect.models", "app.capabilities.jobs.models")
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
    store = ast.parse((REPO_ROOT / "app/stages/collect/adaptive/store/store.py").read_text("utf-8"))
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "_switch_transition"
        for node in ast.walk(store)
    )


def test_only_the_recovery_branch_settles_a_run_by_recovery() -> None:
    # Review 5312254605 B2: settled_by_recovery decides the one non-blocking missing-shadow cause,
    # so exactly one call site — the collection's revision-recovery — may set it true. Both
    # transports' jobs reach it through the same method (ADR-0019 E2).
    callers = [
        (path, node.lineno)
        for path, tree in _production_modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "recovered"
    ]
    assert [path for path, _ in callers] == ["app/stages/collect/collection.py"], callers
    tree = ast.parse((REPO_ROOT / "app/stages/collect/collection.py").read_text("utf-8"))
    (recover,) = (
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "recover_recorded"
    )
    # The method reads the revision the run already appended, returns when there is none, and only
    # then sets the marker.
    statements = recover.body
    lookup = next(
        index
        for index, node in enumerate(statements)
        if any(isinstance(c, ast.Attribute) and c.attr == "for_run" for c in ast.walk(node))
    )
    absent = next(
        index
        for index, node in enumerate(statements)
        if isinstance(node, ast.If) and any(isinstance(c, ast.Return) for c in node.body)
    )
    marker = next(
        index
        for index, node in enumerate(statements)
        if any(isinstance(c, ast.Attribute) and c.attr == "recovered" for c in ast.walk(node))
    )
    assert lookup < absent < marker, "the marker is set only after an appended revision is found"


def test_the_canonical_collection_knows_only_the_shadow_seam() -> None:
    # The canonical owners depend on the protocols of app.stages.collect.shadow, never on an
    # Adaptive
    # package; only the container composes the two.
    for path in (
        "app/stages/collect/collection.py",
        "app/stages/collect/runs.py",
        "app/stages/collect/shadow.py",
    ):
        tree = ast.parse((REPO_ROOT / path).read_text("utf-8"))
        assert not any(_is_adaptive(name) for name in _imported_modules(tree)), path


# Who may import the Adaptive packages at all: the packages themselves, the schema aggregate for
# their models, and the container that composes them (P3). No COLLECT owner, route, job or script
# imports them.
ADAPTIVE_IMPORTERS = {
    "app/platform/db/metadata.py": {
        "app.stages.collect.adaptive.store",
        "app.stages.collect.adaptive.shadow",
        "app.stages.collect.adaptive.phase_c_capture",
    },
    "app/container.py": {
        # ADR-0019 §6.1: the capture owner's boundary naming, used by the extension security gate.
        "app.stages.collect.adaptive.engine.capture",
        "app.stages.collect.adaptive.engine.hooks",
        "app.stages.collect.adaptive.phase_c_capture.accounting",
        "app.stages.collect.adaptive.phase_c_capture.commands",
        "app.stages.collect.adaptive.phase_c_capture.runner",
        "app.stages.collect.adaptive.phase_c_capture.store",
        "app.stages.collect.adaptive.shadow.runner",
        "app.stages.collect.adaptive.shadow.store",
        "app.stages.collect.adaptive.shadow.switch",
        "app.stages.collect.adaptive.store.gate",
        "app.stages.collect.adaptive.store.store",
    },
}
# Phase C C0 (Issue #110 5826469852 item 2, 6): the harness under
# automation/adaptive/phase_c/harness is the only other
# importer, and the only non-test caller of the Phase C operator actions.
PHASE_C_HARNESS = "automation/adaptive/phase_c/harness/"
ADAPTIVE_CAPTURE_ROOT = "app/stages/collect/adaptive/phase_c_capture/"


def test_only_the_container_wires_the_adaptive_collector() -> None:
    roots = [*PRODUCTION_ROOTS, REPO_ROOT / "automation"]
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
            assert not any(n.startswith("app.stages.collect.adaptive.shadow") for n in names), path


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
        "app.stages.collect.adaptive.shadow.switch",
        "app.stages.collect.adaptive.shadow.store",
        "app.stages.collect.adaptive.phase_c_capture.store",
        "app.stages.collect.adaptive.phase_c_capture.commands",
        "app.stages.collect.adaptive.phase_c_capture.accounting",
    }
)


def _phase_c_modules() -> dict[str, ast.Module]:
    roots = [*PRODUCTION_ROOTS, REPO_ROOT / "automation"]
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
        "app.platform.db.database",
    )
    for path, tree in _phase_c_modules().items():
        if not path.startswith((PHASE_C_HARNESS, "automation/adaptive/phase_c/phase_c.py")):
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
    tree = ast.parse((REPO_ROOT / "app/stages/collect/collection.py").read_text("utf-8"))
    assert "app.stages.collect.shadow" in _imported_modules(tree)
    assert not any(_is_adaptive(name) for name in _imported_modules(tree))


# Issue #52 ruling 5711123764 §1: the REAL acceptance harness orchestrates and never collects.
CAMPAIGN_MAY_NOT_IMPORT = (
    "integrations.suppliers.kmretail.collect",
    "app.stages.collect.assets",
    "app.stages.collect.sourceassets",
    "app.stages.collect.revisions",
    "app.stages.collect.imagedecode",
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
        *sorted((REPO_ROOT / "automation" / "acceptance" / "m3" / "campaign").rglob("*.py")),
        REPO_ROOT / "automation" / "acceptance" / "m3" / "m3_accept.py",
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
    from automation.acceptance.m3.campaign.prep import HARD_ZERO_MODULES

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
        "app.stages.connect.marketplace",
        "app.platform.core.errors",
        "app.platform.core.clock",
        "app.platform.core.safe_payload",
        # A0 (PR-C): the keyed fingerprint and its key in the OS secret store — no network.
        "base64",
        "hashlib",
        "hmac",
        "os",
        "app.platform.core.secrets",
        "app.capabilities.audit",
        "app.platform.db.base",
        "app.platform.db.types",
        "app.platform.db.database",
    )
    modules = {
        path: tree
        for path, tree in _production_modules().items()
        if path.startswith("app/stages/connect/marketplace/")
    }
    assert "app/stages/connect/marketplace/service.py" in modules
    for path, tree in modules.items():
        for name in _imported_modules(tree):
            assert any(name == a or name.startswith(f"{a}.") for a in allowed), f"{path}: {name}"


def test_s17_19_only_the_operator_entry_point_records_contract_freshness() -> None:
    # CAPABILITY_MAPPING F8: contract freshness is contract-governance truth, not provider runtime
    # evidence. PR-B's local operator entry point is its only production recorder; an adapter
    # (PR-A) may consume the recorded value but must never set, infer, seed or fabricate CURRENT.
    modules = _production_modules()
    assert {p for p, t in modules.items() if _calls(t, "record_contract_freshness")} == {
        "app/interface/api/routes/connect.py"
    }
    assert {p for p, t in modules.items() if _calls(t, "record_freshness")} == {
        "app/stages/connect/marketplace/service.py"
    }


def test_s17_18_only_pr_a_supplies_the_mapping_revision_and_the_application_identity() -> None:
    # M2 instructions §5.1/§5.2/§6.7: A0 consumes the endpoint-mapping revision and the
    # application identity through seams, and PR-A supplies their one authoritative
    # implementation each: the endpoint registry's revision and the committed SmartStore
    # credential bundle. No other production module may implement either — no hardcoded
    # "v1"-style revision and no typed-in identity. The seams are class methods; a module-level
    # function of the same name (the Alembic schema revision in app/platform/db/migrate.py) is not
    # one.
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
        "app/stages/connect/marketplace/revision.py:EndpointMappingRevisionProvider.current_revision",
        "app/stages/connect/marketplace/sources.py:ApplicationIdentitySource.current_identity",
    }
    assert implementers == {
        "integrations/marketplaces/smartstore/registry.py:RegistryMappingRevision.current_revision",
        "app/stages/connect/smartstore/service.py:SmartStoreConnectService.current_identity",
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
        if path.startswith("app/stages/connect/marketplace/")
    }
    assert {
        "app/stages/connect/marketplace/attestation.py",
        "app/stages/connect/marketplace/attestation_service.py",
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
    from app.platform.db.metadata import metadata

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
        # The official marketplace leaf-category catalog: immutable provider snapshots and their
        # leaf entries. Selection validity is derived against the current snapshot.
        "marketplace_category_catalog_snapshots",
        "marketplace_category_catalog_entries",
        "registration_bulk_runs",
        "registration_bulk_items",
        # M6-A (ADR-0023 §3): the listing-state sync runs and their append-only observations.
        "operate_listing_sync_runs",
        "operate_listing_observations",
        "operate_stock_rechecks",
        "operate_order_sync_runs",
        "operate_orders",
        "operate_order_status_history",
        "operate_adopted_listings",
        "operate_adopted_observations",
        "operate_order_adoption_links",
        "operate_supplier_orders",
        "operate_supplier_order_history",
        # ADR-0026 AIF-1: the PromptTemplate and PlatformPolicy stores.
        "ai_prompt_templates",
        "ai_prompt_template_revisions",
        "ai_prompt_template_current",
        "ai_platform_policies",
        "ai_platform_policy_revisions",
        "ai_platform_policy_current",
        # ADR-0026 AIF-3: PRODUCT DB's structured enrichment results.
        "product_enrichment_results",
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
        # The SEARCH positive-only reconcile slice (ADR-0014 §28.4; Issue #89 5904349289 H-S2):
        # the append-only reconcile-check owner, recorded by every reconcile of an Intent.
        "registration_reconcile_checks",
        # The authoring-revision owners (ADR-0014 §27.1; Issue #89 5907626428): the append-only
        # category-mapping and detail-composition profile revisions.
        "registration_authoring_revisions",
        # The canary-eligibility owner (ADR-0018 §5.1; Issue #89 5910018106): the append-only
        # eligibility record of one exact canary lineage; never a COMPLIANCE PASS.
        "canary_eligibility_records",
        # The extension list queue (ADR-0019 §8.1, E3): each queue read issued durably before it
        # happens. Collection state; no product fact and no page content.
        "extension_queues",
        "extension_queue_items",
        "residual_risk_acceptances",
        "registration_deletions",
        "synthetic_test_products",
        # The operator's decisions on supplier common images (Issue #219).
        "supplier_common_image_decisions",
        "common_sales_option_revisions",
        "common_sales_option_axes",
        "common_sales_option_values",
        "current_common_sales_option_revision_moves",
        "common_option_fact_mapping_revisions",
        "common_option_fact_axis_mappings",
        "common_option_fact_value_mappings",
        "current_common_option_fact_mapping_moves",
        "atomic_sku_set_revisions",
        "atomic_skus",
        "atomic_sku_selections",
        "atomic_sku_revision_members",
        "atomic_sku_revision_selection_evidence",
        "current_atomic_sku_set_moves",
        "atomic_sku_product_items",
        "atomic_sku_source_bindings",
        "atomic_sku_pricing_snapshots",
        "current_atomic_sku_pricing_snapshot_moves",
    }
    offenders = [
        path
        for path in _production_modules()
        if path.startswith(("app/stages/connect/", "integrations/suppliers/"))
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
    """``automation.adaptive.phase_c.harness`` is among the strictly type-checked packages.

    Removing it fails CI.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))["tool"]["mypy"]
    assert "automation.adaptive.phase_c.harness" in config["packages"]
    assert config["strict"] is True and config.get("warn_unused_ignores") is True


def test_only_the_collection_and_connect_send_points_reach_the_send_guard() -> None:
    """Every accounted send point is a known one: the collection's request budget and its
    guard scope, and CONNECT's fetch and login. Nothing else reserves, and only the collection
    sets a guard."""
    importers: dict[str, set[str]] = {}
    for path, tree in _production_modules().items():
        if "app.platform.core.send_guard" in _imported_modules(tree):
            called = {
                c.func.id
                for c in ast.walk(tree)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            }
            importers[path] = called & {"reserve_send", "guarding"}
    assert importers == {
        "app/stages/collect/collection.py": {"reserve_send", "guarding"},
        "app/stages/connect/service.py": {"reserve_send"},
        "app/stages/collect/shadow.py": set(),
    }, importers


def test_only_the_phase_c_harness_persists_adaptive_profiles() -> None:
    """C1 PREP-1 (Issue #110 5843047094): outside the Adaptive packages, the one reviewed operator
    path that stores a PTR or an EPR is the Phase C harness, through the P2 owner's public API."""
    adaptive = (ADAPTIVE_ROOT, ADAPTIVE_STORE_ROOT, ADAPTIVE_SHADOW_ROOT, ADAPTIVE_CAPTURE_ROOT)
    callers: dict[str, set[str]] = {}
    for path, tree in _phase_c_modules().items():
        if path.startswith(adaptive):
            continue
        reached = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} & {
            "save_template",
            "save_draft",
        }
        if reached:
            callers[path] = reached
    assert callers == {f"{PHASE_C_HARNESS}harness.py": {"save_template", "save_draft"}}, callers


# ---------------------------------------------------------------- test browsers stay on loopback

BROWSER_OWNER = "tests/support/browser.py"
# Any spelling of a browser start: ``launch``, ``launch_persistent_context``, ``launch_server``,
# attaching to a browser the test did not launch, an engine other than Chromium, or the async API.
_BROWSER_START = re.compile(
    r"\.(launch\w*|connect_over_cdp)\(|\bchromium\.connect\(|\.(firefox|webkit)\b|\basync_playwright\b"
)
# The launches production and acceptance code make for a real operator. No test may reach them:
# they are not the loopback-only owner's.
_REAL_LAUNCHERS = (
    "automation/acceptance/gate3_visual/harness/harness.py",
    "automation/acceptance/m0/visual_check.py",
    "integrations/suppliers/transport/gateway.py",
)


def test_every_test_browser_is_launched_by_the_one_loopback_only_owner() -> None:
    """Issue #126 ``5909188774`` F-1 and owner amendment ``5909645067`` §3: a repository test
    starts a browser only through ``tests/support/browser.py``, which always applies the
    loopback-only resolver rule. The scan reads every test file as text, so a start written inside
    a child-process script, behind an alias or in a helper that never says "playwright" is found
    as well. There is no exception list."""
    starters: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative == "tests/contracts/test_repository_rules.py":
            continue  # this rule's own pattern and names
        found = sorted(
            {match.group(0) for match in _BROWSER_START.finditer(path.read_text("utf-8"))}
        )
        if found:
            starters[relative] = found
    assert starters == {BROWSER_OWNER: [".launch(", ".launch_persistent_context("]}, starters
    # No test module imports or calls a real launcher of production or acceptance code.
    real = {
        path: sorted(set(_BROWSER_START.findall((REPO_ROOT / path).read_text("utf-8"))))
        for path in _REAL_LAUNCHERS
    }
    assert all(real.values()), real
    reached = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
        if re.search(
            r"visual_check\.(main|run|capture)\(|harness\.run\(|\._browse\(|PlaywrightLogin",
            path.read_text("utf-8"),
        )
        and path.name != "test_repository_rules.py"
    ]
    assert reached == [], reached

    owner = (REPO_ROOT / BROWSER_OWNER).read_text("utf-8")
    assert 'NETWORK_BLOCK = "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"' in owner
    # Both launches take their arguments from the one function that puts the block first and
    # refuses a caller's own resolver rule.
    assert owner.count("args=_arguments(") == 2 == owner.count(".launch")
    assert "return [NETWORK_BLOCK, *extra]" in owner
    assert '_OWNED_ARGUMENTS = ("--host-resolver-rules", "--host-rules", "--proxy")' in owner
    assert "**options" not in owner
    # The block is defined once: nothing else in the test tree spells a resolver rule.
    spelled = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
        if "--host-resolver-rules=" in path.read_text("utf-8")
        and path.relative_to(REPO_ROOT).as_posix()
        not in {
            BROWSER_OWNER,
            "tests/contracts/test_repository_rules.py",
            # The owner's own unit test, which proves a caller's rule is refused.
            "tests/unit/test_browser_support.py",
        }
    ]
    assert spelled == [], spelled
