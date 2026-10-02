"""ADR-0018 §6.1, G3-30: the durable proof of the residual-risk acceptance (migration 0036).

The decision is the user's and the architect's, in GitHub. The owner records only a proof that
points at their two exact comments, for one account and the exact current risk contract, and the
existing send-time layer reads it. Every value here is synthetic: no real acceptance exists, and
none is implied by these tests.
"""

import hashlib
import re
import sqlite3
from pathlib import Path

import pytest

from app.capabilities.audit.models import AuditEventType
from app.capabilities.live_safety import residual_risk as rr
from app.capabilities.live_safety.residual_risk import ResidualRiskAcceptanceService
from app.capabilities.live_safety.store import LiveAuthorityStore
from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.errors import InputValidationError, NotFoundError
from app.platform.db.database import Database
from tests.support.register_support import MARKET, establish

pytestmark = pytest.mark.integration

TABLE = "residual_risk_acceptances"
ADR = (
    Path(__file__).resolve().parents[3]
    / "documents"
    / "decisions"
    / "adr"
    / "0018-gate3-pre-live-safety-and-bounded-live-authorization.md"
)
# Synthetic GitHub comment identities: an id and the SHA-256 of a body.
USER = "github_issue_comment:5950000001@" + "a" * 64
ARCHITECT = "github_issue_comment:5950000002@" + "b" * 64


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


def _record(container: Container, account: str, **overrides: str) -> rr.AcceptanceRecord:
    values = {
        "marketplace_key": MARKET,
        "marketplace_account_id": account,
        "risk_contract": rr.RESIDUAL_RISK_CONTRACT_VERSION,
        "user_acceptance": USER,
        "architect_acceptance": ARCHITECT,
        "actor": "operator",
        "correlation_id": "test-residual-risk",
    }
    values.update(overrides)
    return container.residual_risk.record(**values)


def _rows(config: AppConfig) -> int:
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        return int(raw.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])


def _stage_proofs(container: Container) -> object:
    # The proof source the production send-time stack reads.
    return container.safety_stack._proofs


def test_no_proof_is_never_accepted(container: Container, account: str) -> None:
    assert container.residual_risk.accepted(MARKET, account) is False
    assert _stage_proofs(container).residual_risk_accepted(MARKET, account) is False  # type: ignore[attr-defined]


def test_an_exact_scope_proof_is_accepted_and_points_at_the_two_comments(
    container: Container, config: AppConfig, account: str
) -> None:
    record = _record(container, account)
    assert record.seq == 1
    assert record.risk_contract_version == rr.RESIDUAL_RISK_CONTRACT_VERSION
    assert record.risk_statement_digest == rr.RESIDUAL_RISK_STATEMENT_DIGEST
    assert record.user_acceptance.identity == USER
    assert record.architect_acceptance.identity == ARCHITECT
    assert container.residual_risk.accepted(MARKET, account) is True
    # The production stage proofs consume the same owner, for the same exact scope.
    assert _stage_proofs(container).residual_risk_accepted(MARKET, account) is True  # type: ignore[attr-defined]
    assert _rows(config) == 1


def test_another_marketplace_or_account_is_not_accepted(container: Container, account: str) -> None:
    _record(container, account)
    assert container.residual_risk.accepted(MARKET, "acct-not-this-one") is False
    assert container.residual_risk.accepted("coupang", account) is False
    assert container.residual_risk.accepted("", account) is False
    assert container.residual_risk.accepted(MARKET, "") is False


def test_a_stale_risk_contract_or_statement_proves_nothing(
    container: Container, config: AppConfig, account: str
) -> None:
    # The command refuses any contract but the current one, and records nothing.
    with pytest.raises(InputValidationError) as refused:
        _record(container, account, risk_contract="adr-0018-6.1-residual-risk/v0")
    assert refused.value.code == rr.CONTRACT_NOT_CURRENT
    assert _rows(config) == 0
    # A proof of another contract version, or of another statement under the current version,
    # never answers for the current risk.
    store = LiveAuthorityStore(container.db, container.clock, container.audit)
    for version, digest in (
        ("adr-0018-6.1-residual-risk/v0", rr.RESIDUAL_RISK_STATEMENT_DIGEST),
        (rr.RESIDUAL_RISK_CONTRACT_VERSION, "c" * 64),
    ):
        with store.transaction() as unit:
            unit.record_residual_risk_acceptance(
                marketplace_key=MARKET,
                marketplace_account_id=account,
                risk_contract_version=version,
                risk_statement_digest=digest,
                user_comment_id="5950000001",
                user_comment_digest="a" * 64,
                architect_comment_id="5950000002",
                architect_comment_digest="b" * 64,
                actor="operator",
                correlation_id="test-residual-risk",
            )
    assert _rows(config) == 2
    assert container.residual_risk.accepted(MARKET, account) is False


@pytest.mark.parametrize(
    ("user", "architect"),
    [
        ("https://github.com/o/r/issues/89#issuecomment-5950000001", ARCHITECT),
        ("5950000001", ARCHITECT),
        ("github_issue_comment:5950000001", ARCHITECT),
        ("github_pr_review:5950000001@" + "a" * 64, ARCHITECT),
        ("github_issue_comment:59500@" + "a" * 64, ARCHITECT),
        ("github_issue_comment:5950000001@" + "A" * 64, ARCHITECT),
        (USER, "github_issue_comment:5950000002@" + "b" * 63),
        # one comment is not two acceptances
        (USER, "github_issue_comment:5950000001@" + "b" * 64),
    ],
)
def test_malformed_or_unsupported_evidence_is_refused_and_records_nothing(
    container: Container, config: AppConfig, account: str, user: str, architect: str
) -> None:
    with pytest.raises(InputValidationError):
        _record(container, account, user_acceptance=user, architect_acceptance=architect)
    assert _rows(config) == 0
    assert container.residual_risk.accepted(MARKET, account) is False


def test_an_unknown_account_is_refused(container: Container, config: AppConfig) -> None:
    with pytest.raises(NotFoundError) as refused:
        _record(container, "acct-unknown")
    assert refused.value.code == "LIVE_ACCOUNT_NOT_FOUND"
    assert _rows(config) == 0


def test_the_proof_is_append_only_and_audited(
    container: Container, config: AppConfig, account: str
) -> None:
    first = _record(container, account)
    second = _record(container, account)
    assert (first.seq, second.seq) == (1, 2)
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute(f"UPDATE {TABLE} SET recorded_by = 'someone else'")
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute(f"DELETE FROM {TABLE}")
        # A seq that is not the next of its scope is refused by the database itself.
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute(
                f"INSERT INTO {TABLE} VALUES ('x', ?, ?, ?, ?, 7, '5950000003', ?, '5950000004', ?,"
                " 'operator', '2026-10-02 00:00:00')",
                (MARKET, account, rr.RESIDUAL_RISK_CONTRACT_VERSION, "d" * 64, "e" * 64, "f" * 64),
            )
        events = raw.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = ?",
            (AuditEventType.RESIDUAL_RISK_ACCEPTANCE_RECORDED.value,),
        ).fetchone()[0]
    assert _rows(config) == 2 and events == 2
    assert [r.seq for r in container.residual_risk.history(MARKET, account)] == [1, 2]


def test_the_proof_survives_a_restart(
    container: Container, config: AppConfig, account: str
) -> None:
    _record(container, account)
    reopened = Database(config.database_url)
    try:
        again = ResidualRiskAcceptanceService(
            LiveAuthorityStore(reopened, container.clock, container.audit)
        )
        assert again.accepted(MARKET, account) is True
    finally:
        reopened.dispose()


def test_the_accepted_statement_is_the_adrs_own() -> None:
    adr = " ".join(re.sub(r"(?m)^\s*>\s?", "", ADR.read_text(encoding="utf-8")).split())
    assert rr.RESIDUAL_RISK_STATEMENT in adr
    assert (
        hashlib.sha256(rr.RESIDUAL_RISK_STATEMENT.encode("utf-8")).hexdigest()
        == rr.RESIDUAL_RISK_STATEMENT_DIGEST
    )
