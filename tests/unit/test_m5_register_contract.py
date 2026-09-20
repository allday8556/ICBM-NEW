"""Repository contract for M5 (Issue #89, ADR-0014). PR-A pinned it before any M5 schema existed;
PR-B (migration 0016) adds exactly the registration foundation, and nothing else.

Two kinds of rule:
- **The repository.** No M5 endpoint is adopted; the only M5 schema is the registration foundation
  of migration 0016, with no stored readiness, registrability or summary; only the registration
  store writes it; REGISTER reaches no provider; and product registration write stays unproven.
  PR #91 review 5255746944 adds: registration state is scoped by the canonical
  ``marketplace_account_id`` that only the CONNECT account owner mints, and a Draft Item pins its
  exact M4 price.
- **The decision.** ADR-0014 states each binding rule of kickoff 5740316498, of the architect
  addendum 5740352676 (R1-R4) and of the PR #90 review 5255157251 (B1-B4), and states nothing
  that contradicts it. The upload gate, the resolution evidence and the durable digest fields are
  checked as structure, not only as sentences.

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
from sqlalchemy import Column, Integer, MetaData, Table, UniqueConstraint

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
# M5 PR-D adopts these two reads (packet 5746489554); nothing else, and nothing mutating.
M5_ADOPTED = frozenset({"SMARTSTORE_ORIGIN_PRODUCT_READ_V2", "SMARTSTORE_CHANNEL_PRODUCT_READ_V2"})
M5_UNPROVEN = frozenset(
    {
        "SMARTSTORE_PRODUCT_CREATE_V2",
        "SMARTSTORE_PRODUCT_IMAGE_UPLOAD",
        "SMARTSTORE_PRODUCT_SEARCH",
        "SMARTSTORE_CATEGORY_LIST",
        "SMARTSTORE_CATEGORY_READ",
        "SMARTSTORE_PRODUCT_ATTRIBUTE_LIST",
        "SMARTSTORE_PRODUCT_ATTRIBUTE_VALUES",
        "SMARTSTORE_STANDARD_OPTIONS",
        "SMARTSTORE_NOTICE_TYPES",
        "SMARTSTORE_NOTICE_TYPE_READ",
    }
)
M5_MAPPING_REVISION = "m5-register-r1"


def adoption_problems(adopted: Iterable[str]) -> list[str]:
    """An adopted SmartStore endpoint beyond the M2 CONNECT pair and the M5 PR-D read-backs."""
    return sorted(set(adopted) - M2_ENDPOINTS - M5_ADOPTED)


def test_only_the_read_backs_are_adopted_and_the_rest_fail_locally() -> None:
    from integrations.marketplaces.smartstore import registry

    assert adoption_problems(e.value for e in registry.ADOPTED) == []
    assert {e.value for e in registry.NOT_ADOPTED} == M5_UNPROVEN
    for endpoint in registry.NOT_ADOPTED:
        with pytest.raises(registry.EndpointNotAdoptedError):
            registry.resolve(endpoint)
    # §17: adoption bumps the mapping revision and its fingerprint in the same change.
    assert registry.SMARTSTORE_ENDPOINT_MAPPING_REVISION == M5_MAPPING_REVISION
    assert registry.mapping_fingerprint() == registry.MAPPING_FINGERPRINTS[M5_MAPPING_REVISION]


def test_no_mutating_endpoint_is_adopted() -> None:
    # ADR-0014 §24 and PR-D §2: adoption creates no mutation authority. CREATE and image upload
    # stay unadopted, so no production path can reach a SmartStore mutation at all.
    from integrations.marketplaces.smartstore import registry

    assert [c.endpoint_id.value for c in registry.ADOPTED.values() if c.mutating] == []
    assert {"SMARTSTORE_PRODUCT_CREATE_V2", "SMARTSTORE_PRODUCT_IMAGE_UPLOAD"} <= {
        e.value for e in registry.NOT_ADOPTED
    }


def test_the_adoption_detector_fires() -> None:
    adopted = [*M2_ENDPOINTS, "SMARTSTORE_PRODUCT_CREATE_V2", "SMARTSTORE_PRODUCT_IMAGE_UPLOAD"]
    assert adoption_problems(adopted) == [
        "SMARTSTORE_PRODUCT_CREATE_V2",
        "SMARTSTORE_PRODUCT_IMAGE_UPLOAD",
    ]


# ---------------------------------------------------------------- schema (ADR-0014 §3, §25)

M5_HEAD = "0016_m5_registration_foundation"
REGISTRATION_STATE = re.compile(
    r"registration|registerable|listing_draft|draft_listing|duplicate_override"
    r"|marketplace_asset|registration_intent|registration_attempt",
    re.I,
)
# ADR-0014 §25: PR-B owns these tables, and only these (Issue #89 §20).
REGISTRATION_TABLES = frozenset(
    {
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
    }
)
# ADR-0014 §3 and §12: preflight is derived and a batch or Draft summary is derived, so no column
# may store readiness, registrability or a PARTIAL-style summary as a truth.
STORED_TRUTH = re.compile(
    r"registerable|readiness|(^|_)ready($|_)|partial|summary|preflight_(status|state)", re.I
)


def migration_problems(names: Iterable[str]) -> list[str]:
    """A migration after the M5 foundation head, or a registration-named migration other than it."""
    head = int(M5_HEAD.split("_", 1)[0])
    return [
        name
        for name in names
        if int(name.split("_", 1)[0]) > head
        or (REGISTRATION_STATE.search(name) and not name.startswith(M5_HEAD))
    ]


def registration_schema_problems(
    metadata: MetaData, expected: frozenset[str] = REGISTRATION_TABLES
) -> list[str]:
    """Registration tables other than exactly ``expected``, or any column storing readiness,
    registrability or a summary as a truth."""
    present = {name for name in metadata.tables if REGISTRATION_STATE.search(name)}
    problems = [f"extra table {name}" for name in present - expected]
    problems += [f"missing table {name}" for name in expected - present]
    problems += [
        f"{table.name}.{column.name}"
        for table in metadata.tables.values()
        for column in table.columns
        if STORED_TRUTH.search(column.name)
    ]
    return sorted(problems)


def test_the_only_m5_migration_is_the_registration_foundation() -> None:
    from app.db.migrate import head_revision

    names = sorted(p.name for p in MIGRATIONS.glob("0*.py"))
    assert migration_problems(names) == []
    assert f"{M5_HEAD}.py" in names
    assert head_revision() == M5_HEAD


def test_the_migration_detector_fires() -> None:
    names = [
        "0015_m4_quantity_offers.py",
        "0016_m5_registration_foundation.py",
        "0017_m5_registration_more.py",
        "0009_duplicate_override.py",
    ]
    assert migration_problems(names) == [
        "0017_m5_registration_more.py",
        "0009_duplicate_override.py",
    ]


def test_the_registration_schema_is_exactly_the_foundation() -> None:
    from app.db.metadata import metadata

    assert registration_schema_problems(metadata) == []


def test_the_registration_schema_detector_fires() -> None:
    synthetic = MetaData()
    Table("registration_intents", synthetic, Column("id", Integer, primary_key=True))
    Table(
        "registration_batches",
        synthetic,
        Column("id", Integer, primary_key=True),
        Column("partial_summary", Integer),
    )
    Table(
        "product_items",
        synthetic,
        Column("item_id", Integer, primary_key=True),
        Column("registerable", Integer),
    )
    Table("marketplace_asset_uploads", synthetic, Column("id", Integer, primary_key=True))
    expected = frozenset({"registration_intents", "registration_batches", "registration_drafts"})
    assert registration_schema_problems(synthetic, expected) == [
        "extra table marketplace_asset_uploads",
        "missing table registration_drafts",
        "product_items.registerable",
        "registration_batches.partial_summary",
    ]


# PR-B: the registration store is the only production writer of the registration tables, so the
# conflict scope, the idempotency identity and the sanitized digests cannot be bypassed.
REGISTRATION_OWNERS = frozenset({"app/register/store.py", "app/register/models.py"})
REGISTRATION_CLASSES = frozenset(
    {
        "RegistrationDraft",
        "RegistrationDraftItem",
        "RegistrationSnapshot",
        "RegistrationItemSnapshot",
        "RegistrationBatch",
        "RegistrationIntent",
        "RegistrationAttempt",
        "MarketplaceRegistration",
        "MarketplaceRegistrationItem",
        "DuplicateOverride",
    }
)
REGISTRATION_TABLE_NAMES = re.compile(
    r"\b(registration_(drafts|draft_items|snapshots|item_snapshots|batches|intents|attempts)"
    r"|marketplace_registrations|marketplace_registration_items|duplicate_overrides)\b"
)


def registration_writer_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    """Production code, other than the registration store, its models and the migrations, that
    names a registration model or table, and so could write around the store."""
    offenders = []
    for where, source in sources:
        if where in REGISTRATION_OWNERS or "/migrations/" in where:
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
            if named in REGISTRATION_CLASSES or (
                isinstance(text, str) and REGISTRATION_TABLE_NAMES.search(text)
            ):
                offenders.append(f"{where}:{getattr(node, 'lineno', 0)}")
    return offenders


def test_only_the_registration_store_writes_registration_state() -> None:
    assert registration_writer_problems(_code()) == []


def test_the_registration_writer_detector_fires() -> None:
    sources = [
        ("app/register/service.py", "from app.register.models import RegistrationIntent\n"),
        ("app/other/raw.py", "SQL = 'UPDATE registration_intents SET state = 1'\n"),
        ("scripts/tool.py", "T = 'duplicate_overrides'\n"),
        ("app/db/migrations/versions/0099_x.py", "T = 'registration_attempts'\n"),
        ("app/register/store.py", "from app.register.models import RegistrationIntent\n"),
    ]
    assert registration_writer_problems(sources) == [
        "app/register/service.py:1",
        "app/other/raw.py:1",
        "scripts/tool.py:1",
    ]


# ---------------------------------------------------------------- account scope and price pin

# PR #91 review 5255746944: every account-scoped registration table names the canonical
# ``marketplace_account_id`` of ACCOUNT_IDENTITY §2 through a foreign key (blocker 2), provider
# product ids are unique per canonical account, and a Draft Item pins its exact M4 price
# (blocker 1). The provider wire ``account_id`` names no registration column.
ACCOUNT_SCOPED_TABLES = frozenset(
    {
        "registration_drafts",
        "registration_snapshots",
        "registration_batches",
        "registration_intents",
        "marketplace_registrations",
        "duplicate_overrides",
    }
)
ACCOUNT_KEY = (
    ["marketplace_key", "marketplace_account_id"],
    ["marketplace_accounts.marketplace_key", "marketplace_accounts.marketplace_account_id"],
)
PROVIDER_ID_PER_ACCOUNT = {"marketplace_key", "marketplace_account_id", "marketplace_product_id"}


def registration_scope_problems(metadata: MetaData) -> list[str]:
    """A registration column named ``account_id``, an account-scoped table without the canonical
    account key, a provider product id unique beyond one account, or a Draft Item without a
    pinned exact price."""
    problems = []
    for name, table in sorted(metadata.tables.items()):
        if not REGISTRATION_STATE.search(name):
            continue
        if "account_id" in table.columns:
            problems.append(f"{name}.account_id")
        if name in ACCOUNT_SCOPED_TABLES and not any(
            (list(fk.column_keys), [e.target_fullname for e in fk.elements]) == ACCOUNT_KEY
            for fk in table.foreign_key_constraints
        ):
            problems.append(f"{name}: no canonical account key")
    registrations = metadata.tables.get("marketplace_registrations")
    if registrations is not None:
        uniques = [
            {column.name for column in constraint.columns}
            for constraint in registrations.constraints
            if isinstance(constraint, UniqueConstraint)
        ]
        if (
            PROVIDER_ID_PER_ACCOUNT not in uniques
            or PROVIDER_ID_PER_ACCOUNT - {"marketplace_account_id"} in uniques
        ):
            problems.append("marketplace_registrations: provider id not unique per account")
    items = metadata.tables.get("registration_draft_items")
    if items is not None:
        pin = items.columns.get("pricing_snapshot_id")
        if (
            pin is None
            or pin.nullable
            or {fk.target_fullname for fk in pin.foreign_keys}
            != {"pricing_snapshots.pricing_snapshot_id"}
        ):
            problems.append("registration_draft_items: no pinned price")
    return problems


def test_registration_state_is_scoped_by_the_canonical_account_and_pins_its_price() -> None:
    from app.db.metadata import metadata

    assert registration_scope_problems(metadata) == []


def test_the_registration_scope_detector_fires() -> None:
    synthetic = MetaData()
    Table("pricing_snapshots", synthetic, Column("pricing_snapshot_id", Integer, primary_key=True))
    Table(
        "registration_drafts",
        synthetic,
        Column("draft_id", Integer, primary_key=True),
        Column("marketplace_key", Integer),
        Column("account_id", Integer),
    )
    Table(
        "registration_draft_items",
        synthetic,
        Column("draft_item_id", Integer, primary_key=True),
        Column("pricing_snapshot_id", Integer, nullable=True),
    )
    Table(
        "marketplace_registrations",
        synthetic,
        Column("registration_id", Integer, primary_key=True),
        Column("marketplace_key", Integer),
        Column("marketplace_product_id", Integer),
        UniqueConstraint("marketplace_key", "marketplace_product_id"),
    )
    assert registration_scope_problems(synthetic) == [
        "marketplace_registrations: no canonical account key",
        "registration_drafts.account_id",
        "registration_drafts: no canonical account key",
        "marketplace_registrations: provider id not unique per account",
        "registration_draft_items: no pinned price",
    ]


# ACCOUNT_IDENTITY §5: only the CONNECT account owner mints a canonical account, from a committed
# binding, so no second writer can create an alias scope. A foreign-key target such as
# "marketplace_accounts.marketplace_key" is a schema reference, not a write.
ACCOUNT_OWNERS = frozenset({"app/connect/accounts.py", "app/connect/account_models.py"})
ACCOUNT_CLASSES = frozenset({"SellerEntity", "MarketplaceAccount"})
ACCOUNT_TABLE_NAMES = re.compile(r"\b(seller_entities|marketplace_accounts)\b(?!\.)")


def account_writer_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    offenders = []
    for where, source in sources:
        if where in ACCOUNT_OWNERS or "/migrations/" in where:
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
            if named in ACCOUNT_CLASSES or (
                isinstance(text, str) and ACCOUNT_TABLE_NAMES.search(text)
            ):
                offenders.append(f"{where}:{getattr(node, 'lineno', 0)}")
    return offenders


def test_only_the_account_owner_writes_canonical_accounts() -> None:
    assert account_writer_problems(_code()) == []


def test_the_account_writer_detector_fires() -> None:
    sources = [
        ("app/register/store.py", "from app.connect.account_models import MarketplaceAccount\n"),
        ("scripts/seed.py", "SQL = 'INSERT INTO marketplace_accounts VALUES (1)'\n"),
        ("app/register/models.py", "FK = 'marketplace_accounts.marketplace_key'\n"),
        ("app/connect/accounts.py", "from app.connect.account_models import SellerEntity\n"),
    ]
    assert account_writer_problems(sources) == ["app/register/store.py:1", "scripts/seed.py:1"]


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
    "M5-22": "no marketplace asset upload happens before a mutation-free non-asset preflight"
    " candidate is READY; the upload is bound to that candidate's fingerprint",
    "M5-23": "resolved_by = USER records or accepts evidence; an operator assertion alone never"
    " establishes NOT_APPLIED_PROVEN or remote absence and never releases an UNKNOWN conflict"
    " scope",
    "M5-24": "every durable payload or request digest hashes the sanitized canonical"
    " representation; secret-bearing wire bytes exist only transiently and are never persisted"
    " or durably hashed",
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
    # PR #90 GPT review 5255157251, blockers B2-B4 (B1 is the ARCHITECTURE owner list, below).
    "B2 no marketplace asset upload before a READY non-asset candidate": Rule(
        (
            "**Before any marketplace asset upload, every non-provider-asset preflight dependency"
            " of the provider-listing unit passes a mutation-free candidate evaluation.**",
            "**If any non-asset result is `BLOCKED`, `DUPLICATE`, `STALE` or `REVIEW_REQUIRED`,"
            " no upload is permitted.**",
            "**The only dependency allowed to be unresolved at asset-preparation time is the"
            " provider asset identity itself.**",
            "**The asset preparation and upload are bound to that candidate's dependency"
            " fingerprint**",
            "**If any dependency changed between the candidate and the final preflight, no CREATE"
            " follows from that upload.**",
            "**No new readiness truth is stored for it.**",
            "and **only after the non-asset preflight candidate is `READY` (§3)**",
        ),
        (
            r"upload(ed|s)? (may|can) (happen|proceed|occur) before (the |any )?"
            r"(preflight|candidate)",
            r"(BLOCKED|DUPLICATE|STALE|REVIEW_REQUIRED) candidate (may|can) (upload|proceed)",
        ),
    ),
    "B3 resolved_by = USER is never the evidence of a remote outcome": Rule(
        (
            "**An operator assertion alone never establishes `NOT_APPLIED_PROVEN` or remote"
            " absence, and never releases an unresolved UNKNOWN conflict scope.**",
            "**Changing `UNKNOWN` to `NOT_APPLIED_PROVEN` or absent requires evidence that"
            " satisfies the adopted, operation-specific proof contract**",
            "**The operator may be the actor who records or accepts that evidence, but is not"
            " itself the evidence.**",
            "USER: who recorded the resolution, never itself the evidence (§10)",
        ),
        (
            r"(operator|user)('s)? (assertion|statement|confirmation|word)[^.]{0,80}"
            r"(may|can) (establish|set|mark|release|free)",
        ),
    ),
    "B4 durable digests hash the sanitized representation, never wire bytes": Rule(
        (
            "**Every durable payload or request digest (`payload_hash`, `request_payload_hash`"
            " and any evidence digest) is computed only from the sanitized canonical evidence"
            " representation**",
            "**Authorization headers and session credentials are never part of a durable digest.**",
            "Secret-bearing or tokenized material is removed **before durable hashing** as well as"
            " before persistence.",
            "**If the provider requires such a value on the wire, it exists only transiently in"
            " memory for that call; its unsanitized bytes are neither persisted nor durably"
            " hashed.**",
            "**If sanitation would remove a field required to prove the registration contract,"
            " the evidence is `REVIEW_REQUIRED`**",
        ),
        (
            r"(payload|request)[_ ]?hash\w* (is|=) (the )?SHA-256 of the"
            r" (raw|wire|full|unsanitized)",
            r"(unsanitized|raw) wire bytes (are|may be) (persisted|hashed|stored)",
        ),
    ),
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
    "B2": "A DUPLICATE candidate may proceed to the marketplace asset upload.",
    "B3": "An operator statement that the listing was not created may establish"
    " NOT_APPLIED_PROVEN.",
    "B4": "The unsanitized wire bytes are hashed for the Attempt.",
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


# ---------------------------------------------------------------- PR #90 review 5255157251
#
# B1-B4 as machine-checked structure: the canonical owner list, the upload gate table and its
# order, the resolution-evidence table, and every durable digest field.

CANDIDATE_STATUSES = ("READY", "REVIEW_REQUIRED", "STALE", "DUPLICATE", "BLOCKED")


def register_owner_problems(architecture: str) -> list[str]:
    """B1: the canonical REGISTER owner list, or the image pipeline, giving binary transformation
    to REGISTER or to a marketplace adapter instead of the M4 derived-image owner (R1)."""
    start = architecture.index("\n### REGISTER")
    end = architecture.index("\n### ", start + 1)
    problems = [
        line.strip()
        for line in architecture[start:end].splitlines()
        if line.startswith("- ") and re.search(r"transform", line, re.I) and "M4" not in line
    ]
    problems += [m.group(0) for m in re.finditer(r"variants belong to [^.;]*adapter", architecture)]
    return problems


def contract_block(adr: str, header: str) -> list[str]:
    """The lines of the fenced ``text`` block whose first line starts with ``header``."""
    for block in re.findall(r"```text\n(.*?)```", adr, re.S):
        lines = block.strip("\n").splitlines()
        if lines and lines[0].startswith(header):
            return lines
    return []


def contract_table(adr: str, header: str) -> dict[str, str]:
    """A two-column contract table: the first column, then the verdict after 2+ spaces."""
    rows = {}
    for line in contract_block(adr, header)[1:]:
        match = re.match(r"^(.*?\S)\s{2,}(\S.*)$", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def upload_gate_problems(table: Mapping[str, str]) -> list[str]:
    """B2: only a READY non-asset candidate permits a marketplace asset upload."""
    problems = [] if set(table) == set(CANDIDATE_STATUSES) else [f"statuses {sorted(table)}"]
    for status, verdict in table.items():
        if verdict.startswith("permitted") != (status == "READY"):
            problems.append(f"{status}: {verdict}")
    return problems


GATE_ORDER = (
    "non-asset preflight candidate",
    "marketplace asset preparation / upload",
    "final preflight",
    "RegistrationSnapshot freeze",
)


def gate_order_problems(lines: list[str]) -> list[str]:
    """B2: candidate, then upload, then final preflight, then the Snapshot."""
    positions = [
        next((i for i, line in enumerate(lines) if step in line), -1) for step in GATE_ORDER
    ]
    if -1 in positions or positions != sorted(positions):
        return [f"order {positions}"]
    return []


def resolution_problems(table: Mapping[str, str]) -> list[str]:
    """B3: an operator assertion never establishes a remote outcome; machine or provider proof
    does. Every row is yes or no, and the operator row exists and says no."""
    problems = [
        f"{row}: {verdict}" for row, verdict in table.items() if verdict not in ("yes", "no")
    ]
    operator = [row for row in table if re.search(r"operator|USER|assertion", row)]
    if not operator:
        problems.append("no operator-assertion row")
    problems += [f"{row}: {table[row]}" for row in operator if table[row] != "no"]
    if not any(v == "yes" and "read-back" in row for row, v in table.items()):
        problems.append("read-back is not proof")
    return problems


DIGEST_FIELDS = frozenset({"payload_hash", "request_payload_hash"})


def digest_field_problems(adr: str) -> list[str]:
    """B4: every durable ``*_hash`` field in a contract block hashes the sanitized canonical
    representation, never wire bytes; both payload digests are present."""
    problems = []
    found = set()
    for block in re.findall(r"```text\n(.*?)```", adr, re.S):
        for line in block.splitlines():
            field = line.split()[0] if line.split() else ""
            if not field.endswith("_hash"):
                continue
            found.add(field)
            if "sanitized canonical" not in line or "never of wire bytes" not in line:
                problems.append(line.strip())
    problems += [f"missing {name}" for name in sorted(DIGEST_FIELDS - found)]
    return problems


def test_the_canonical_register_owner_list_claims_no_image_transformation() -> None:
    assert register_owner_problems(ARCHITECTURE_MD.read_text("utf-8")) == []


def test_the_register_owner_detector_fires() -> None:
    before = (
        "\n### REGISTER\nOwns platform conversion.\n\n- category mapping\n"
        "- image transformation/upload\n\n### OPERATE\n"
        "Derived marketplace variants belong to the M4 image pipeline and to each marketplace"
        " adapter/readiness contract.\n"
    )
    assert register_owner_problems(before) == [
        "- image transformation/upload",
        "variants belong to the M4 image pipeline and to each marketplace adapter",
    ]


def test_only_a_ready_non_asset_candidate_permits_an_upload() -> None:
    adr = ADR_0014.read_text("utf-8")
    assert upload_gate_problems(contract_table(adr, "non-asset candidate status")) == []
    assert gate_order_problems(contract_block(adr, "non-asset preflight candidate")) == []


def test_a_duplicate_or_blocked_candidate_never_proceeds_to_upload() -> None:
    # The negative control the review asks for: a gate that lets a DUPLICATE or BLOCKED
    # candidate upload, or uploads before the candidate, is caught.
    leaky = {
        "READY": "permitted, bound to the candidate fingerprint",
        "REVIEW_REQUIRED": "forbidden",
        "STALE": "forbidden",
        "DUPLICATE": "permitted",
        "BLOCKED": "permitted",
    }
    assert upload_gate_problems(leaky) == ["DUPLICATE: permitted", "BLOCKED: permitted"]
    assert upload_gate_problems({"READY": "permitted"}) != []
    upload_first = [
        "marketplace asset preparation / upload",
        "→ non-asset preflight candidate",
        "→ final preflight",
        "→ RegistrationSnapshot freeze",
    ]
    assert gate_order_problems(upload_first) != []


def test_an_operator_assertion_never_establishes_a_remote_outcome() -> None:
    adr = ADR_0014.read_text("utf-8")
    table = contract_table(adr, "resolution evidence")
    assert resolution_problems(table) == []
    assert table["operator assertion alone (resolved_by = USER)"] == "no"


def test_user_says_it_was_not_created_cannot_free_the_unknown() -> None:
    # The negative control the review asks for: a table where the operator's word proves
    # NOT_APPLIED_PROVEN, or where no operator row exists, is caught.
    trusting = {
        "provider read-back under the adopted contract": "yes",
        "operator assertion alone (resolved_by = USER)": "yes",
    }
    assert resolution_problems(trusting) == ["operator assertion alone (resolved_by = USER): yes"]
    assert resolution_problems({"provider read-back under the adopted contract": "yes"}) == [
        "no operator-assertion row"
    ]


def test_every_durable_digest_field_hashes_the_sanitized_representation() -> None:
    assert digest_field_problems(ADR_0014.read_text("utf-8")) == []


def test_the_durable_digest_detector_fires() -> None:
    wire = (
        "```text\nRegistrationSnapshot\n  payload_hash                     SHA-256 of the wire"
        " request body\n```\n```text\nRegistrationAttempt\n  request_payload_hash\n```\n"
    )
    assert digest_field_problems(wire) == [
        "payload_hash                     SHA-256 of the wire request body",
        "request_payload_hash",
    ]
    assert digest_field_problems("```text\nRegistrationSnapshot\n```\n") == [
        "missing payload_hash",
        "missing request_payload_hash",
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
