"""M4 PR-F: the acceptance harness's own rules, pure and static (Issue #80 kickoff 5739459941).

Each rule runs on the real harness source and on a small synthetic violation, so a rule that could
never fire is caught as surely as one that fails.
"""

import ast
import re
from collections.abc import Iterable
from pathlib import Path, PurePath

import pytest

from app.products.model import READINESS_PRECEDENCE, ReadinessStatus, Reason, precedence_status
from scripts.m3accept.prep import HARD_ZERO_MODULES
from scripts.m4accept import evidence
from scripts.m4accept.guards import FORBIDDEN_MODULES, forbidden
from scripts.m4accept.root import names_preserved_campaign

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = [
    *sorted((REPO_ROOT / "scripts" / "m4accept").glob("*.py")),
    REPO_ROOT / "scripts" / "m4_acceptance.py",
]
# ORM-bearing packages the schema aggregate loads. Their provider-facing modules are forbidden one
# by one instead.
ORM_PACKAGES = {
    "app.connect.marketplace": (
        "app.connect.marketplace.service",
        "app.connect.marketplace.attestation_service",
    ),
    "app.connect.smartstore": (
        "app.connect.smartstore.service",
        "app.connect.smartstore.credentials",
    ),
}
_SQL_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\s+[A-Z(]")


def _sources(paths: Iterable[Path]) -> list[tuple[str, str]]:
    return [(path.relative_to(REPO_ROOT).as_posix(), path.read_text("utf-8")) for path in paths]


def provider_import_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    """A harness module that imports provider, CONNECT, container or HTTP code."""
    reach = ("app.container", "app.main", "app.api", "app.connect.service", "app.connect.sessions")
    offenders = []
    for where, source in sources:
        for node in ast.walk(ast.parse(source)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                if forbidden(name) or any(name == r or name.startswith(f"{r}.") for r in reach):
                    offenders.append(f"{where}:{node.lineno}")
    return offenders


def sql_write_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    """A harness module holding SQL that writes, or opening SQLite other than read-only."""
    offenders = []
    for where, source in sources:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _SQL_WRITE.search(node.value)
            ):
                offenders.append(f"{where}:{node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and "mode=ro" not in ast.unparse(node)
            ):
                offenders.append(f"{where}:{node.lineno}")
    return offenders


# ---------------------------------------------------------------- static rules


def test_the_harness_imports_no_provider_code() -> None:
    assert provider_import_problems(_sources(HARNESS)) == []


def test_the_provider_import_detector_fires() -> None:
    sources = [
        ("scripts/m4accept/a.py", "from integrations.marketplaces.smartstore import caller\n"),
        ("scripts/m4accept/b.py", "import openai\n"),
        ("scripts/m4accept/c.py", "from app.container import build_container\n"),
        ("scripts/m4accept/fine.py", "from app.products.pricing import PricingContextInput\n"),
    ]
    assert provider_import_problems(sources) == [
        "scripts/m4accept/a.py:1",
        "scripts/m4accept/b.py:1",
        "scripts/m4accept/c.py:1",
    ]


def test_the_harness_writes_no_sql_and_opens_sqlite_read_only() -> None:
    assert sql_write_problems(_sources(HARNESS)) == []


def test_the_sql_write_detector_fires() -> None:
    sources = [
        ("scripts/m4accept/a.py", "SQL = 'INSERT INTO quantity_offers VALUES (1)'\n"),
        ("scripts/m4accept/b.py", "c = sqlite3.connect(path)\n"),
        ("scripts/m4accept/fine.py", "c = sqlite3.connect(f'{uri}?mode=ro', uri=True)\n"),
        ("scripts/m4accept/text.py", "WHY = 'nothing is updated here'\n"),
    ]
    assert sql_write_problems(sources) == ["scripts/m4accept/a.py:1", "scripts/m4accept/b.py:1"]


def test_the_forbidden_imports_cover_the_source_truth_hard_zero() -> None:
    # The M3 campaign's AI, OCR and marketplace list stays covered: every entry is forbidden
    # outright, or it is an ORM-bearing package whose provider modules are.
    for name in HARD_ZERO_MODULES:
        if name in ORM_PACKAGES:
            assert all(module in FORBIDDEN_MODULES for module in ORM_PACKAGES[name]), name
        else:
            assert name in FORBIDDEN_MODULES, name
    for name in ("app.ai", "pytesseract", "integrations.suppliers.transport", "playwright"):
        assert forbidden(f"{name}.anything") and forbidden(name)
    assert not forbidden("app.connect.smartstore.models")
    assert not forbidden("app.products.pricing")


# ---------------------------------------------------------------- roots


@pytest.mark.parametrize(
    "path",
    [
        "m3-accept-04",
        "ICBM-acceptance/m3-accept-01/m4",
        "m3-recon-02",
        "C:/ICBM-acceptance/ICBM-M3-ACCEPT-03",
        "m3_accept_02/data",
        "M3ACCEPT",
    ],
)
def test_a_preserved_campaign_name_is_recognised(path: str) -> None:
    assert names_preserved_campaign(PurePath(path))


@pytest.mark.parametrize(
    "path", ["m4-acceptance/run-1", "ICBM-acceptance/m4-rehearsal-01", "m2-campaign-02", "data"]
)
def test_an_ordinary_name_is_not_a_preserved_campaign(path: str) -> None:
    assert not names_preserved_campaign(PurePath(path))


# ---------------------------------------------------------------- evidence


LEAKS = {
    "a URL": "see https://example.com/x",
    "an absolute Windows path": "C:\\Users\\someone\\data",
    "a UNC or backslash path": "\\\\server\\share",
    "an absolute POSIX path": "/home/someone/data",
    "secret-like material": "session cookie",
    "an approval phrase": "APPROVE m4 0123456789ab",
    "non-ASCII text": "상품명",
}


@pytest.mark.parametrize(("kind", "text"), LEAKS.items(), ids=LEAKS.keys())
def test_every_kind_of_leak_is_found(kind: str, text: str) -> None:
    report = {"schema": evidence.REPORT_SCHEMA, "nested": {"list": ["ok", text]}}
    assert kind in evidence.leaks(report)


def test_a_clean_report_and_a_run_path_are_judged_correctly() -> None:
    clean = {
        "run_id": "0b6f7d0e-3f26-4bb6-9c43-2f64e9c1a7de",
        "sha": "a" * 64,
        "schema": evidence.REPORT_SCHEMA,
        "codes": ["IMAGE_SELECTION_MISSING", "PRICE_LOSS"],
        "amount": 19900,
    }
    assert evidence.leaks(clean) == []
    assert evidence.leaks({"where": "rehearsal-root-17"}, ("rehearsal-root-17",)) == [
        "a local path of this run"
    ]


def test_the_report_digest_covers_everything_but_itself() -> None:
    report: dict[str, object] = {"schema": evidence.REPORT_SCHEMA, "problems": [], "value": 1}
    report[evidence.DIGEST_FIELD] = evidence.report_digest(report)
    assert evidence.verify_report(report)
    report["value"] = 2
    assert not evidence.verify_report(report)


def test_history_allows_only_an_open_binding_to_close() -> None:
    before = {
        "source_bindings": {"b1": {"binding_id": "b1", "valid_to": None, "item_id": "i"}},
        "pricing_snapshots": {"p1": {"pricing_snapshot_id": "p1", "final": 18000}},
    }
    closed = {
        "source_bindings": {"b1": {"binding_id": "b1", "valid_to": "t", "item_id": "i"}},
        "pricing_snapshots": {"p1": {"pricing_snapshot_id": "p1", "final": 18000}},
    }
    assert evidence.history_changes(before, closed) == []
    rewritten = {
        "source_bindings": {"b1": {"binding_id": "b1", "valid_to": None, "item_id": "other"}},
        "pricing_snapshots": {"p1": {"pricing_snapshot_id": "p1", "final": 19000}},
    }
    assert evidence.history_changes(before, rewritten) == [
        "source_bindings: a row changed (item_id)",
        "pricing_snapshots: a row changed (final)",
    ]
    assert evidence.history_changes(before, {"source_bindings": {}}) == [
        "source_bindings: a row disappeared",
        "pricing_snapshots: a row disappeared",
    ]


def test_readiness_precedence_places_duplicate_below_blocked_and_above_stale() -> None:
    # The report states that no M4 rule produces DUPLICATE (M4 performs no MERGE); its place in
    # ruling D's precedence is pinned here.
    assert READINESS_PRECEDENCE == (
        ReadinessStatus.BLOCKED,
        ReadinessStatus.DUPLICATE,
        ReadinessStatus.STALE,
        ReadinessStatus.REVIEW_REQUIRED,
        ReadinessStatus.READY,
    )
    duplicate = Reason("GROUP_ITEM_DUPLICATE", ReadinessStatus.DUPLICATE)
    stale = Reason("PRICING_SNAPSHOT_SUPERSEDED", ReadinessStatus.STALE)
    blocked = Reason("SOURCE_STOCK_SOLD_OUT", ReadinessStatus.BLOCKED)
    assert precedence_status((stale, duplicate)) is ReadinessStatus.DUPLICATE
    assert precedence_status((duplicate, blocked)) is ReadinessStatus.BLOCKED
    assert precedence_status(()) is ReadinessStatus.READY
