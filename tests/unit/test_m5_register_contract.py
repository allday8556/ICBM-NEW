"""Repository contract for M5 PR-A (Issue #89, ADR-0014), pinned before any M5 schema exists.

Two kinds of rule:
- **The repository.** No M5 endpoint is adopted, no migration or registration table exists,
  REGISTER does nothing and reaches no provider, and product registration write stays unproven.
- **The decision.** ADR-0014 states each binding rule of kickoff 5740316498 and of the architect
  addendum 5740352676 (R1-R4), and states nothing that contradicts it.

Each checker is a pure function. It runs against the real repository, and also against a small
synthetic violation, so a rule that could never fire is caught as surely as a rule that fails.
"""

import ast
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Table

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
ADR_0014 = DOCS / "adr" / "0014-smartstore-register-idempotency-readback.md"
ADR_PATH = "docs/adr/0014-smartstore-register-idempotency-readback.md"
M5_MD = DOCS / "acceptance" / "M5.md"
ARCHITECTURE_MD = DOCS / "ARCHITECTURE.md"
ROADMAP_MD = REPO_ROOT / "ROADMAP.md"
MIGRATIONS = REPO_ROOT / "app" / "db" / "migrations" / "versions"
REGISTER_SERVICE = "app/register/service.py"
CODE_ROOTS = ("app", "integrations", "scripts")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _code() -> list[tuple[str, str]]:
    return [
        (path.relative_to(REPO_ROOT).as_posix(), path.read_text("utf-8"))
        for root in CODE_ROOTS
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
    ]


# ---------------------------------------------------------------- endpoints (ADR-0014 §17)

M2_ENDPOINTS = frozenset({"SMARTSTORE_AUTH_TOKEN", "SMARTSTORE_SELLER_ACCOUNT"})
M5_ENDPOINTS = frozenset(
    {
        "SMARTSTORE_PRODUCT_CREATE_V2",
        "SMARTSTORE_ORIGIN_PRODUCT_READ_V2",
        "SMARTSTORE_CHANNEL_PRODUCT_READ_V2",
        "SMARTSTORE_PRODUCT_IMAGE_UPLOAD",
        "SMARTSTORE_CATEGORY_LIST",
        "SMARTSTORE_CATEGORY_READ",
        "SMARTSTORE_PRODUCT_ATTRIBUTE_LIST",
        "SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES",
        "SMARTSTORE_STANDARD_OPTIONS",
        "SMARTSTORE_NOTICE_TYPES",
    }
)
M2_MAPPING_REVISION = "m2-connect-r1"


def adoption_problems(adopted: Iterable[str]) -> list[str]:
    """An adopted SmartStore endpoint beyond the two M2 CONNECT endpoints: PR-A adopts nothing."""
    return sorted(set(adopted) - M2_ENDPOINTS)


def test_the_m5_endpoints_stay_not_adopted_and_fail_locally() -> None:
    from integrations.marketplaces.smartstore import registry

    assert adoption_problems(e.value for e in registry.ADOPTED) == []
    assert {e.value for e in registry.NOT_ADOPTED} == M5_ENDPOINTS
    for endpoint in registry.NOT_ADOPTED:
        with pytest.raises(registry.EndpointNotAdoptedError):
            registry.resolve(endpoint)
    # The endpoint-mapping revision and its fingerprint are unchanged: adoption bumps both (§17).
    assert registry.SMARTSTORE_ENDPOINT_MAPPING_REVISION == M2_MAPPING_REVISION
    assert list(registry.MAPPING_FINGERPRINTS) == [M2_MAPPING_REVISION]
    assert registry.mapping_fingerprint() == registry.MAPPING_FINGERPRINTS[M2_MAPPING_REVISION]


def test_the_adoption_detector_fires() -> None:
    adopted = [*M2_ENDPOINTS, "SMARTSTORE_PRODUCT_CREATE_V2", "SMARTSTORE_PRODUCT_IMAGE_UPLOAD"]
    assert adoption_problems(adopted) == [
        "SMARTSTORE_PRODUCT_CREATE_V2",
        "SMARTSTORE_PRODUCT_IMAGE_UPLOAD",
    ]


# ---------------------------------------------------------------- schema (ADR-0014 §3, §25)

M4_HEAD = "0015_m4_quantity_offers"
REGISTRATION_STATE = re.compile(
    r"registration|registerable|listing_draft|draft_listing|duplicate_override"
    r"|marketplace_asset|registration_intent|registration_attempt",
    re.I,
)


def migration_problems(names: Iterable[str]) -> list[str]:
    """A migration after the M4 head, or one that names registration state: PR-A has no schema."""
    head = int(M4_HEAD.split("_", 1)[0])
    return [
        name
        for name in names
        if int(name.split("_", 1)[0]) > head or REGISTRATION_STATE.search(name)
    ]


def registration_schema_problems(metadata: MetaData) -> list[str]:
    """A table or column holding registration state, or a stored registrability truth."""
    problems = [f"table {name}" for name in metadata.tables if REGISTRATION_STATE.search(name)]
    problems += [
        f"{table.name}.{column.name}"
        for table in metadata.tables.values()
        for column in table.columns
        if REGISTRATION_STATE.search(column.name)
    ]
    return sorted(problems)


def test_no_migration_after_the_m4_head() -> None:
    from app.db.migrate import head_revision

    names = sorted(p.name for p in MIGRATIONS.glob("0*.py"))
    assert migration_problems(names) == []
    assert head_revision() == M4_HEAD


def test_the_migration_detector_fires() -> None:
    names = [
        "0015_m4_quantity_offers.py",
        "0016_m5_registration_foundation.py",
        "0009_duplicate_override.py",
    ]
    assert migration_problems(names) == [
        "0016_m5_registration_foundation.py",
        "0009_duplicate_override.py",
    ]


def test_no_registration_state_is_stored_yet() -> None:
    from app.db.metadata import metadata

    assert registration_schema_problems(metadata) == []


def test_the_registration_schema_detector_fires() -> None:
    synthetic = MetaData()
    Table("registration_intents", synthetic, Column("id", Integer, primary_key=True))
    Table(
        "product_items",
        synthetic,
        Column("item_id", Integer, primary_key=True),
        Column("registerable", Integer),
    )
    Table("marketplace_asset_uploads", synthetic, Column("id", Integer, primary_key=True))
    Table("pricing_snapshots", synthetic, Column("id", Integer, primary_key=True))
    assert registration_schema_problems(synthetic) == [
        "product_items.registerable",
        "table marketplace_asset_uploads",
        "table registration_intents",
    ]


# ---------------------------------------------------------------- REGISTER does nothing yet

REGISTER_COUNTS = frozenset({"registration_candidate_count", "registration_count"})
PROVIDER_REACH = re.compile(
    r"^(httpx|requests|urllib3|aiohttp|playwright|integrations\.(marketplaces|suppliers)"
    r"|app\.connect\.(smartstore|marketplace)\.(service|caller|credentials))(\.|$)"
)


def register_service_problems(source: str) -> list[str]:
    """``RegisterService`` may only report zero: another public method, a count other than the
    literal 0, or an import is REGISTER behaviour PR-A does not authorize."""
    problems = []
    tree = ast.parse(source)
    problems += [
        f"import at {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    for cls in (
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RegisterService"
    ):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            if fn.name.startswith("_"):
                continue
            body = [n for n in fn.body if not isinstance(n, ast.Expr)]
            returns_zero = (
                len(body) == 1
                and isinstance(body[0], ast.Return)
                and isinstance(body[0].value, ast.Constant)
                and body[0].value.value == 0
            )
            if fn.name not in REGISTER_COUNTS or not returns_zero:
                problems.append(f"RegisterService.{fn.name}")
    return problems


def register_reach_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    """A REGISTER module importing a provider transport, an HTTP client or a SmartStore caller."""
    offenders = []
    for where, source in sources:
        if not where.startswith("app/register/"):
            continue
        for node in ast.walk(ast.parse(source)):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            if any(PROVIDER_REACH.match(module) for module in modules):
                offenders.append(f"{where}:{node.lineno}")
    return offenders


def test_register_service_stays_unimplemented() -> None:
    from app.register.service import RegisterService

    assert register_service_problems((REPO_ROOT / REGISTER_SERVICE).read_text("utf-8")) == []
    service = RegisterService()
    assert service.registration_candidate_count() == 0
    assert service.registration_count() == 0


def test_the_register_service_detector_fires() -> None:
    source = (
        "import httpx\n"
        "class RegisterService:\n"
        "    def registration_count(self) -> int:\n"
        "        return 1\n"
        "    def create(self, snapshot):\n"
        "        return 0\n"
        "    def registration_candidate_count(self) -> int:\n"
        '        """Zero."""\n'
        "        return 0\n"
    )
    assert register_service_problems(source) == [
        "import at 1",
        "RegisterService.registration_count",
        "RegisterService.create",
    ]


def test_no_register_module_reaches_a_provider() -> None:
    assert register_reach_problems(_code()) == []


def test_the_register_reach_detector_fires() -> None:
    sources = [
        ("app/register/service.py", "import httpx\n"),
        ("app/register/intents.py", "from integrations.marketplaces.smartstore import caller\n"),
        ("app/register/more.py", "from app.connect.smartstore.service import SmartStoreService\n"),
        ("app/register/fine.py", "from app.products.pricing import PricingContextInput\n"),
        ("app/connect/smartstore/service.py", "import httpx\n"),
    ]
    assert register_reach_problems(sources) == [
        "app/register/service.py:1",
        "app/register/intents.py:1",
        "app/register/more.py:1",
    ]


# ---------------------------------------------------------------- capability (ADR-0014 §16)


def test_product_registration_write_stays_unverified() -> None:
    from app.connect.marketplace import capability as cap
    from app.db.metadata import metadata

    assert cap.PRODUCT_WRITE_PROVABLE is False
    assert cap.CapabilityState().write is cap.WriteStatus.UNVERIFIED
    # Even with auth READY, product write cannot be READY before a reviewed M5 proof (W1).
    with pytest.raises(cap.CapabilityInvariantError, match="NOT_ADOPTED"):
        cap.CapabilityState(
            auth=cap.AuthStatus.READY,
            auth_verified_at=datetime(2026, 9, 19, tzinfo=UTC),
            write=cap.WriteStatus.READY,
        )
    # The database refuses a stored READY as well.
    checks = [
        str(constraint.sqltext)
        for constraint in metadata.tables["marketplace_capabilities"].constraints
        if getattr(constraint, "name", "") == "ck_marketplace_capabilities_m2_write_unproven"
    ]
    assert checks == ["write_status IN ('UNVERIFIED', 'BLOCKED')"]


# ---------------------------------------------------------------- the decision (ADR-0014)


def decision_section(adr: str) -> str:
    """The binding part of the ADR, whitespace-normalized: from "## Decision" to its invariants."""
    start = adr.index("\n## Decision")
    end = adr.index("\n## Invariants", start)
    return _normalized(adr[start:end])


def invariants(adr: str) -> dict[str, str]:
    """The ``M5-NN`` lines of the fenced block under "## Invariants"."""
    section = adr[adr.index("\n## Invariants") :]
    block = re.search(r"```text\n(.*?)```", section, re.S)
    assert block, "no invariants block"
    found = {}
    for line in block.group(1).splitlines():
        match = re.match(r"^(M5-\d\d)\s+(.*\S)\s*$", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


EXPECTED_INVARIANTS = {
    "M5-01": "M4 owns Product, Item, binding, PricingSnapshot, image lineage and readiness; M5 owns"
    " Draft, preflight, Snapshot, Intent, Attempt, Registration, DuplicateOverride and provider"
    " asset identity",
    "M5-02": "marketplace-sized binaries are M4 DerivedImageArtifacts with lineage and QA; M5 owns"
    " the upload and the provider asset identity only",
    "M5-03": "preflight is derived; no stored REGISTERABLE truth exists",
    "M5-04": "a provider-listing unit has exactly one immutable RegistrationSnapshot and one CREATE"
    " RegistrationIntent",
    "M5-05": "a historical Snapshot never follows later current-state changes",
    "M5-06": "registration_item_key exists before CREATE; display labels are not identity",
    "M5-07": "a retry never creates a new Snapshot or a new Intent",
    "M5-08": "an UNKNOWN CREATE is reconciled before any resend; no blind CREATE retry",
    "M5-09": "an unresolved UNKNOWN CREATE blocks every new CREATE Intent in its conflict scope,"
    " even for a new Snapshot",
    "M5-10": "a non-overlapping group in the same marketplace and account is not blocked by another"
    " group's UNKNOWN",
    "M5-11": "read-back is compared to the immutable Snapshot, never to the current Draft or Item",
    "M5-12": "a SINGLE_LISTING_WITH_OPTIONS subset read-back is a mismatch of the whole Intent;"
    " missing Items are never resent as CREATE",
    "M5-13": "SEPARATE_LISTINGS partial success is per listing Intent; a CONFIRMED sibling is never"
    " resent",
    "M5-14": "a batch or Draft PARTIAL is derived from child Intents, never stored as authoritative"
    " truth",
    "M5-15": "an operator assertion of external deletion alone never frees duplicate protection",
    "M5-16": "proven remote absence keeps the historical registration evidence and allows a fresh"
    " duplicate preflight and a new Snapshot and Intent",
    "M5-17": "recurring detection of external deletion is M6 OPERATE, not M5",
    "M5-18": "REGISTER raw evidence is sanitized before it is hashed or persisted",
    "M5-19": "no supplier hotlink is ever published",
    "M5-20": "product_registration.write stays UNVERIFIED until the bounded real CREATE is proven"
    " by read-back",
    "M5-21": "M5 registers with no AI provider configured",
}


@dataclass(frozen=True)
class Rule:
    """Sentences the decision must state, and affirmative phrasings that would contradict it."""

    required: tuple[str, ...]
    forbidden: tuple[str, ...] = ()


# The architect addendum 5740352676 requires A1-A9; the kickoff 5740316498 requires the K rules.
RULES: dict[str, Rule] = {
    "A1 a marketplace-sized binary stays in the M4 lineage; M5 owns upload and identity only": Rule(
        (
            "**Marketplace-sized binaries remain in the M4 derived-image lineage.**",
            "**M5 does not create a second derived-image lineage owner**",
            "**A later transformation or upload never mutates a historical Snapshot.**",
        ),
        (
            r"M5 (creates|owns|stores) (the |a |its own )?(derived|transformed|resized)"
            r" (binary|artifact|image)",
            r"M5 (resizes|transforms|re-encodes) ",
        ),
    ),
    "A2 an unresolved UNKNOWN blocks a new Snapshot's CREATE Intent in its scope": Rule(
        (
            "**An unresolved UNKNOWN CREATE blocks every new CREATE Intent in its conflict scope,"
            " even for a new Snapshot.**",
            "**Overlap with any affected group is sufficient.**",
            "**Only after the old ambiguity is resolved as not applied or absent**",
            "**PR-B enforces this with a database and service invariant**",
        ),
        (
            r"new Snapshot (may|can) (open|create|start) a new CREATE",
            r"DuplicateOverride (releases|frees|resolves) (an? )?(unresolved )?UNKNOWN",
        ),
    ),
    "A3 a non-overlapping group is not blocked by another group's UNKNOWN": Rule(
        ("**A non-overlapping group in the same marketplace and account is not blocked**",),
        (r"(every|all|any) (other )?groups? (in the (same )?account )?(is|are) blocked",),
    ),
    "A4 a SINGLE_LISTING_WITH_OPTIONS subset is no per-Item success; no CREATE resend": Rule(
        (
            "**A subset is a registration mismatch of the whole Intent, never a per-Item partial"
            " success.**",
            "**The missing Items are never resent as a CREATE**",
            "**Any repair is an explicit later UPDATE or reconcile operation with its own reviewed"
            " contract and Intent. It is not a CREATE retry.**",
        ),
        (
            r"missing (Items|options) (may|can|are) (be )?(resent|retried|re-sent)",
            r"per-Item partial success is (allowed|permitted|recorded)",
        ),
    ),
    "A5 SEPARATE_LISTINGS splits into Snapshot/Intent units; no confirmed sibling replays": Rule(
        (
            "The Draft is split, before execution, into one Snapshot and one Intent per provider"
            " listing.",
            "**a `CONFIRMED` Intent stays `CONFIRMED` and is never resent**",
            "**one listing's success or failure never rewrites a sibling Intent.**",
            "**Before any execution, the Draft is resolved into provider-listing units. Each unit"
            " has exactly one immutable `RegistrationSnapshot` and one CREATE"
            " `RegistrationIntent`.**",
        ),
        (r"CONFIRMED (Intent|sibling|listing)s? (may|can) be (resent|replayed|retried)",),
    ),
    "A6 a batch or Draft PARTIAL is derived from child Intents": Rule(
        (
            "**`PARTIAL` is a derived summary.**",
            "**No authoritative batch or Draft state is stored that could disagree with its child"
            " Intents.**",
        ),
        (r"PARTIAL is (stored|persisted|recorded) (on|in) the (Draft|batch)",),
    ),
    "A7 an operator assertion alone never frees duplicate protection": Rule(
        (
            "**An operator assertion alone does not prove deletion.**",
            "**If absence cannot be proven,** the registration stays `REVIEW_REQUIRED` and"
            " unresolved, and **duplicate protection is not freed.**",
        ),
        (r"operator (assertion|confirmation) (alone )?(proves|frees|is sufficient|suffices)",),
    ),
    "A8 proven absence keeps history and allows a fresh preflight and a new Intent": Rule(
        (
            "**ICBM never deletes the `MarketplaceRegistration` row or its historical Snapshot,"
            " Attempt and read-back evidence.**",
            "**If provider evidence proves the listing absent,** a terminal **external-absence**"
            " state",
            "a **fresh duplicate preflight** finds no live conflicting SmartStore listing;",
            "a **new immutable `RegistrationSnapshot`** is created from current truth;",
            "a **new CREATE Intent** with a new idempotency identity is created.",
            "**The old seller product code is not assumed reusable.**",
        ),
        (r"ICBM (deletes|removes) the (`)?MarketplaceRegistration",),
    ),
    "A9 recurring external-deletion detection stays M6": Rule(
        (
            "**Recurring detection of a later listing disappearance remains M6 OPERATE.**",
            "**recurring detection of external deletion**, remain M6 OPERATE.",
        ),
        (r"M5 (detects|monitors|polls) (for )?(recurring|later|external)",),
    ),
    "K owner boundary: M4 truth stays M4, no second price": Rule(
        (
            "**No second truth.**",
            "Only the M4 Pricing owner calculates a selling price; M5 never re-decides one, and no"
            " listing-level price exists.",
        ),
        (r"(a|the) listing-level price (is|may be) (set|stored|used)",),
    ),
    "K preflight is derived; no stored registerable truth; freshness fail-closed": Rule(
        (
            "**No stored `REGISTERABLE`**",
            "**Freshness is fail-closed.**",
            "**Its dependency fingerprint and rule version are explicit.**",
            "**`BLOCKED > DUPLICATE > STALE > REVIEW_REQUIRED > READY`**",
        ),
        (r"stores? (a |an )?`?REGISTERABLE`? ?(=|flag|truth)",),
    ),
    "K no blind CREATE replay; unresolved ambiguity is never silently FAILED": Rule(
        (
            "**Never resend blindly.**",
            "**Unresolved ambiguity stays `UNKNOWN` with a `REVIEW_REQUIRED` workflow overlay."
            " It is never silently `FAILED`.**",
            "**and only when `remote_outcome = NOT_APPLIED_PROVEN`**",
            "**A retry never creates a new Snapshot or a new Intent.**",
            "**One durable Intent per operation and exact Snapshot.**",
            "**Concurrent sends cannot compete.**",
        ),
        (
            r"CREATE (may|can) be (resent|retried) (after|on) an? UNKNOWN",
            r"TRANSIENT (implies|means) (a )?safe (replay|retry)",
        ),
    ),
    "K read-back compares the immutable Snapshot, never current state": Rule(
        (
            "**the returned listing is compared to the immutable `RegistrationSnapshot`**, never to"
            " the current editable Draft or the current Item;",
            "**`MarketplaceRegistration` and `MarketplaceRegistrationItem` are the durable result"
            " after verification passes.**",
        ),
        (r"compared (to|against) the current (editable )?(Draft|Item)(?! or)",),
    ),
    "K stable correspondence: registration_item_key before CREATE, round-trip proven": Rule(
        (
            "**`registration_item_key` exists before CREATE.**",
            "**Display labels are not identity.**",
            "**Seller-controlled codes are deterministic from stable local identity, never from a"
            " mutable product name.**",
            "**proven to round-trip**",
        ),
    ),
    "K no supplier hotlink publication": Rule(
        ("**No supplier hotlink is ever published.**",),
        (r"(may|can) publish (a )?supplier(-hosted)? (hotlink|URL)",),
    ),
    "K no AI requirement": Rule(
        ("**M5 registers with no AI provider configured.**",),
        (r"requires? an AI provider",),
    ),
    "K ADR-0011: sanitation before hashing, no M6 inheritance": Rule(
        (
            "**Sanitation happens before the artifact exists.**",
            "**A later redacted view, masked render or restricted read path is not sufficient.**",
            "**The safe-query-key contract.**",
            "**deny-by-default allow-list**",
            "**No automatic M6 inheritance**",
        ),
        (r"sanitiz\w* after (it is |they are )?(hashed|persisted|stored)",),
    ),
    "K product_registration.write stays UNVERIFIED until real proof": Rule(
        (
            "**`product_registration.write` starts and stays `UNVERIFIED`**",
            "**A successful real write never fabricates or backfills declared permission"
            " evidence.**",
        ),
        (r"write_scope = READY (proves|implies|means) write",),
    ),
    "K endpoints stay NOT_ADOPTED; enum presence is no wire proof": Rule(
        (
            "**remains `NOT_ADOPTED`**",
            "**Enum presence and planned paths are not wire-contract proof.**",
        ),
    ),
    "K M5 claims no M6 recurring ingest": Rule(("**M5 claims no recurring operational ingest.**",)),
    "K execution safety: DRY_RUN, explicit bounded authorization, single canary": Rule(
        (
            "**the user explicitly authorizes that write scope**",
            "A real M5 acceptance starts with a **single canary product**",
            "**An upload is a marketplace mutation.**",
            "**An ambiguous upload outcome is never represented as a known provider asset"
            " identity.**",
        ),
    ),
}

# One affirmative sentence per rule that has forbidden phrasings: each must be caught.
VIOLATIONS = {
    "A1": "When SmartStore needs a smaller image, M5 resizes the artifact itself.",
    "A2": "A new Snapshot may open a new CREATE Intent while the old one is UNKNOWN.",
    "A3": "While one Intent is UNKNOWN, all groups in the account are blocked.",
    "A4": "The missing options may be resent as a CREATE after a subset read-back.",
    "A5": "A CONFIRMED sibling can be replayed with the rest of the batch.",
    "A6": "PARTIAL is stored on the Draft as its registration state.",
    "A7": "An operator confirmation is sufficient to free the duplicate scope.",
    "A8": "ICBM deletes the MarketplaceRegistration once the listing is gone.",
    "A9": "M5 monitors for later disappearance of every listing.",
    "K owner": "The listing-level price is set once for all options.",
    "K preflight": "The service stores a REGISTERABLE flag per Item.",
    "K no blind": "A CREATE may be resent after an UNKNOWN timeout.",
    "K read-back": "The read-back is compared against the current Draft.",
    "K no supplier": "The payload can publish a supplier hotlink when no upload exists.",
    "K no AI": "Category selection requires an AI provider.",
    "K ADR-0011": "The raw payload is sanitized after it is hashed.",
    "K product_registration": "Here write_scope = READY implies write is READY.",
}


def rule_problems(decision: str, rules: Mapping[str, Rule]) -> list[str]:
    text = _normalized(decision)
    problems = []
    for name, rule in rules.items():
        problems += [
            f"{name}: missing {phrase!r}"
            for phrase in rule.required
            if _normalized(phrase) not in text
        ]
        problems += [
            f"{name}: contradicted by {match.group(0)!r}"
            for pattern in rule.forbidden
            for match in re.finditer(pattern, text, re.I)
        ]
    return problems


def test_adr_0014_is_recorded_and_referenced_by_the_canonical_documents() -> None:
    adr = ADR_0014.read_text("utf-8")
    header = adr.split("\n## ", 1)[0]
    assert re.search(r"^Status: \*\*(PROPOSED|ACCEPTED)\*\*", header, re.M)
    for ruling in ("5740316498", "5740352676"):
        assert ruling in header
    for document in (ARCHITECTURE_MD, ROADMAP_MD):
        assert ADR_PATH in document.read_text("utf-8")


def test_the_invariants_block_is_pinned() -> None:
    assert invariants(ADR_0014.read_text("utf-8")) == EXPECTED_INVARIANTS


@pytest.mark.parametrize("name", RULES)
def test_the_decision_states_each_rule_and_never_contradicts_it(name: str) -> None:
    decision = decision_section(ADR_0014.read_text("utf-8"))
    assert rule_problems(decision, {name: RULES[name]}) == []


@pytest.mark.parametrize("name", RULES)
def test_each_rule_detector_fires(name: str) -> None:
    rule = RULES[name]
    # Every required sentence is missing from an empty decision.
    assert len(rule_problems("", {name: rule})) == len(rule.required)
    # An affirmative contradiction is caught even beside the real decision.
    violation = next((v for key, v in VIOLATIONS.items() if name.startswith(key)), None)
    if rule.forbidden:
        assert violation is not None, name
        decision = decision_section(ADR_0014.read_text("utf-8"))
        problems = rule_problems(f"{decision} {violation}", {name: rule})
        assert problems and all("contradicted by" in p for p in problems), problems
    else:
        assert violation is None, name


def test_the_addendum_rules_are_all_pinned() -> None:
    # Architect addendum 5740352676 "Required PR-A contract tests": nine rules, A1-A9.
    assert sorted(name.split(" ", 1)[0] for name in RULES if name.startswith("A")) == [
        f"A{n}" for n in range(1, 10)
    ]


# ---------------------------------------------------------------- the M5 acceptance plan

M5_PLAN = (
    "No real marketplace request is authorized by this document or by PR-A.",
    "**Offline and fake first.**",
    "**Then a separately user-authorized single SmartStore canary.**",
    "**the necessary image upload is part of that protected write scope**",
    "restart and replay prove no duplicate listing.",
    "**Exact-main closeout.**",
    "**M5 becomes ACCEPTED only when the architect accepts that record.**",
)


def test_the_m5_acceptance_plan_is_pending_and_bounded() -> None:
    text = M5_MD.read_text("utf-8")
    header = text.split("\n## ", 1)[0]
    assert "Status: **PENDING**" in header
    assert "Status: **ACCEPTED**" not in header
    assert ADR_PATH in header
    normalized = _normalized(text)
    for phrase in M5_PLAN:
        assert _normalized(phrase) in normalized, phrase
