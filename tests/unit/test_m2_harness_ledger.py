"""The M2 campaign ledger (Issue #46 §2.1; docs/acceptance/M2.md §5.4, §6): durable reservation
before send, independent hard caps, the approved-run requirement, the frozen plan, the crash
sub-cap and append-only history. Acceptance tooling only; nothing here touches a network."""

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from integrations.marketplaces.smartstore.registry import NOT_ADOPTED
from scripts.m2harness.ledger import (
    CAPS,
    SELLER,
    TOKEN,
    UNRECOGNIZED,
    CrashVerdict,
    Ledger,
    LedgerError,
    Mode,
    Phase,
    Refusal,
    ReservationRefused,
    State,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALL = frozenset(Phase)
HEAD = "a" * 40
DIGEST = "d" * 64


def _ledger(tmp_path: Path, mode: Mode = Mode.DRY) -> Ledger:
    return Ledger.create(
        tmp_path / "ledger.sqlite3", campaign_id="m2-unit-ledger", mode=mode, nonce="nonce"
    )


def _running(tmp_path: Path) -> Ledger:
    ledger = _ledger(tmp_path)
    ledger.mark_preflight_passed(DIGEST, HEAD)
    ledger.begin_dry_run()
    return ledger


def _spend(ledger: Ledger, endpoint: str, times: int) -> None:
    phase = Phase.BASELINE_CONNECT if endpoint == TOKEN else Phase.BASELINE_RESTART
    for _ in range(times):
        attempt = ledger.open_phase(phase)
        reservation = ledger.reserve(endpoint, phases=ALL, pid=1)
        ledger.complete(reservation.seq, http_status=200, latency_ms=1.0)
        ledger.close_phase(phase, attempt, "PASS")


def _refused(ledger: Ledger, endpoint: str, phases: frozenset[Phase] = ALL) -> Refusal:
    with pytest.raises(ReservationRefused) as caught:
        ledger.reserve(endpoint, phases=phases, pid=1)
    return caught.value.reason


def test_a_request_is_durably_reserved_before_its_response_exists(tmp_path: Path) -> None:
    ledger = _running(tmp_path)
    ledger.open_phase(Phase.BASELINE_CONNECT)
    reservation = ledger.reserve(TOKEN, phases=ALL, pid=1)
    # A second connection, as another process opens it, already sees the request as spent.
    other = Ledger.open(ledger.path)
    assert other.counts() == {TOKEN: 1, SELLER: 0}
    assert [row["outcome"] for row in other.rows("requests")] == ["RESERVED"]
    assert reservation.label == "T1"
    ledger.complete(reservation.seq, http_status=200, latency_ms=12.3)
    [row] = other.rows("requests")
    assert (row["outcome"], row["http_status"]) == ("RESPONDED", 200)
    assert other.counts() == {TOKEN: 1, SELLER: 0}


@pytest.mark.parametrize("endpoint", [TOKEN, SELLER])
def test_each_cap_refuses_the_next_request_before_send_and_exhausts_the_campaign(
    tmp_path: Path, endpoint: str
) -> None:
    ledger = _running(tmp_path)
    _spend(ledger, endpoint, CAPS[endpoint])
    ledger.open_phase(Phase.BASELINE_CONNECT if endpoint == TOKEN else Phase.BASELINE_RESTART)
    assert _refused(ledger, endpoint) is Refusal.CAP_REACHED
    assert ledger.counts()[endpoint] == CAPS[endpoint]
    campaign = ledger.campaign()
    assert (campaign.state, campaign.outcome) == (State.BUDGET_EXHAUSTED, "BUDGET_EXHAUSTED")
    assert [row["reason"] for row in ledger.rows("refusals")] == ["CAP_REACHED"]


def test_the_caps_are_independent_and_nothing_is_borrowed(tmp_path: Path) -> None:
    ledger = _running(tmp_path)
    _spend(ledger, SELLER, CAPS[SELLER])
    _spend(ledger, TOKEN, 1)
    ledger.open_phase(Phase.BASELINE_RESTART)
    assert _refused(ledger, SELLER) is Refusal.CAP_REACHED
    assert ledger.counts() == {TOKEN: 1, SELLER: CAPS[SELLER]}


@pytest.mark.parametrize("endpoint", [*sorted(e.value for e in NOT_ADOPTED), UNRECOGNIZED])
def test_every_other_endpoint_has_a_cap_of_zero(tmp_path: Path, endpoint: str) -> None:
    ledger = _running(tmp_path)
    ledger.open_phase(Phase.BASELINE_CONNECT)
    assert _refused(ledger, endpoint) is Refusal.FORBIDDEN_TARGET
    assert ledger.counts() == {TOKEN: 0, SELLER: 0}
    assert ledger.campaign().state is State.BUDGET_EXHAUSTED


def test_nothing_is_accepted_before_an_approved_run(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert _refused(ledger, TOKEN) is Refusal.NOT_RUNNING
    ledger.mark_preflight_passed(DIGEST, HEAD)
    assert ledger.campaign().state is State.AWAITING_REAL_PROVIDER_APPROVAL
    for endpoint in (TOKEN, SELLER):
        assert _refused(ledger, endpoint) is Refusal.NOT_RUNNING
    with pytest.raises(LedgerError):
        ledger.open_phase(Phase.BASELINE_CONNECT)
    # The STOP holds: the refusals are recorded, the budget is untouched, the state unchanged.
    assert ledger.counts() == {TOKEN: 0, SELLER: 0}
    assert ledger.campaign().state is State.AWAITING_REAL_PROVIDER_APPROVAL
    assert [row["reason"] for row in ledger.rows("refusals")] == ["NOT_RUNNING"] * 3


def test_a_real_run_consumes_exactly_the_current_approval(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, Mode.REAL)
    ledger.mark_preflight_passed(DIGEST, HEAD)
    with pytest.raises(LedgerError):
        ledger.begin_real_run("f" * 64)  # nothing was approved
    with pytest.raises(LedgerError):
        ledger.begin_dry_run()  # a REAL ledger is never started without approval
    ledger.issue_approval("1" * 64, approved_sha=HEAD)
    ledger.issue_approval("2" * 64, approved_sha=HEAD)
    with pytest.raises(LedgerError):
        ledger.begin_real_run("1" * 64)  # superseded, so stale
    ledger.begin_real_run("2" * 64)
    assert ledger.campaign().state is State.RUNNING
    ledger.finish(State.STOPPED_STEP_FAILED, reason="TEST")
    with pytest.raises(LedgerError):
        ledger.begin_real_run("2" * 64)  # already used


def test_a_new_preflight_invalidates_an_earlier_approval(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, Mode.REAL)
    ledger.mark_preflight_passed(DIGEST, HEAD)
    ledger.issue_approval("1" * 64, approved_sha=HEAD)
    ledger.mark_preflight_passed("e" * 64, HEAD)
    with pytest.raises(LedgerError):
        ledger.begin_real_run("1" * 64)
    assert ledger.campaign().state is State.AWAITING_REAL_PROVIDER_APPROVAL


def test_a_dry_ledger_never_takes_real_provider_approval(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.mark_preflight_passed(DIGEST, HEAD)
    with pytest.raises(LedgerError):
        ledger.issue_approval("1" * 64, approved_sha=HEAD)
    with pytest.raises(LedgerError):
        ledger.begin_real_run("1" * 64)


def test_preflight_passes_only_while_nothing_was_sent(tmp_path: Path) -> None:
    ledger = _running(tmp_path)
    _spend(ledger, TOKEN, 1)
    ledger.finish(State.STOPPED_STEP_FAILED, reason="TEST")
    with pytest.raises(LedgerError):
        ledger.mark_preflight_passed(DIGEST, HEAD)


def test_only_the_open_phases_frozen_plan_can_be_sent(tmp_path: Path) -> None:
    ledger = _running(tmp_path)
    assert _refused(ledger, TOKEN) is Refusal.NO_OPEN_PHASE
    attempt = ledger.open_phase(Phase.BASELINE_CONNECT)
    # A process started for another phase cannot send under this one.
    assert _refused(ledger, TOKEN, frozenset({Phase.CRASH_T4A})) is Refusal.NO_OPEN_PHASE
    assert ledger.reserve(TOKEN, phases=ALL, pid=1).label == "T1"
    assert _refused(ledger, TOKEN) is Refusal.UNPLANNED_REQUEST
    assert ledger.reserve(SELLER, phases=ALL, pid=1).label == "A1"
    assert _refused(ledger, SELLER) is Refusal.UNPLANNED_REQUEST
    ledger.close_phase(Phase.BASELINE_CONNECT, attempt, "PASS")
    ledger.open_phase(Phase.BASELINE_RESTART)
    assert _refused(ledger, TOKEN) is Refusal.UNPLANNED_REQUEST  # A2 reuses the committed session
    # Unplanned requests are refused before send; they do not exhaust the campaign.
    assert ledger.campaign().state is State.RUNNING
    assert ledger.counts() == {TOKEN: 1, SELLER: 1}


def test_t4b_follows_only_a_t4a_miss_and_there_is_never_a_t4c(tmp_path: Path) -> None:
    ledger = _running(tmp_path)
    with pytest.raises(LedgerError):
        ledger.open_phase(Phase.CRASH_T4B)  # T4b before T4a
    attempt = ledger.open_phase(Phase.CRASH_T4A)
    ledger.reserve(TOKEN, phases=ALL, pid=1)
    assert _refused(ledger, TOKEN) is Refusal.UNPLANNED_REQUEST  # one token per crash attempt
    ledger.close_phase(Phase.CRASH_T4A, attempt, "BOUNDARY_NOT_EXERCISED")
    ledger.record_crash_verdict(1, CrashVerdict.BOUNDARY_NOT_EXERCISED, ["NO_MARKER"])
    with pytest.raises(LedgerError):
        ledger.open_phase(Phase.CRASH_T4A)  # T4a runs once
    attempt = ledger.open_phase(Phase.CRASH_T4B)
    ledger.reserve(TOKEN, phases=ALL, pid=1)
    ledger.close_phase(Phase.CRASH_T4B, attempt, "BOUNDARY_NOT_EXERCISED")
    ledger.record_crash_verdict(2, CrashVerdict.BOUNDARY_NOT_EXERCISED, ["NO_MARKER"])
    # Six tokens remain under the global cap, and still no third attempt can exist.
    assert ledger.counts()[TOKEN] == 2 < CAPS[TOKEN]
    assert ledger.campaign().state is State.BUDGET_EXHAUSTED
    assert "CRASH_SUB_BUDGET_EXHAUSTED" in [row["reason"] for row in ledger.rows("refusals")]
    with closing(sqlite3.connect(ledger.path)) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO crash_attempts (attempt, label, opened_at) VALUES (3, 'T4c', 'x')")


def test_t4b_never_follows_a_t4a_that_exercised_the_boundary(tmp_path: Path) -> None:
    ledger = _running(tmp_path)
    attempt = ledger.open_phase(Phase.CRASH_T4A)
    ledger.reserve(TOKEN, phases=ALL, pid=1)
    ledger.close_phase(Phase.CRASH_T4A, attempt, "BOUNDARY_EXERCISED")
    ledger.record_crash_verdict(1, CrashVerdict.BOUNDARY_EXERCISED, [])
    with pytest.raises(LedgerError):
        ledger.open_phase(Phase.CRASH_T4B)


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM requests",
        "UPDATE requests SET endpoint_id = 'SMARTSTORE_SELLER_ACCOUNT'",
        "UPDATE requests SET outcome = 'RESERVED'",
        "DELETE FROM refusals",
        "UPDATE refusals SET reason = 'NOTHING'",
        "DELETE FROM phases",
        "DELETE FROM events",
        "DELETE FROM meta",
        "UPDATE meta SET token_cap = 80",
        "UPDATE meta SET campaign_id = 'm2-another'",
        "UPDATE meta SET state = 'RUNNING'",
    ],
)
def test_spent_budget_and_history_cannot_be_rewritten(tmp_path: Path, statement: str) -> None:
    ledger = _running(tmp_path)
    _spend(ledger, TOKEN, 1)
    _refused(ledger, SELLER)  # a refusal row to protect
    ledger.finish(State.COMPLETED, reason="TEST")
    with closing(sqlite3.connect(ledger.path)) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(statement)
    reopened = Ledger.open(ledger.path)
    assert reopened.counts() == {TOKEN: 1, SELLER: 0}
    assert reopened.campaign().state is State.COMPLETED


def test_a_ledger_with_a_dropped_guard_is_refused(tmp_path: Path) -> None:
    ledger = _running(tmp_path)
    with closing(sqlite3.connect(ledger.path)) as db:
        db.execute("DROP TRIGGER requests_token_cap")
        db.commit()
    with pytest.raises(LedgerError):
        Ledger.open(ledger.path)


def test_a_campaign_ledger_is_never_recreated_over_its_spent_budget(tmp_path: Path) -> None:
    ledger = _running(tmp_path)
    _spend(ledger, TOKEN, 1)
    with pytest.raises(LedgerError):
        Ledger.create(ledger.path, campaign_id="m2-unit-ledger", mode=Mode.DRY, nonce="again")
    assert Ledger.open(ledger.path).counts()[TOKEN] == 1


def test_the_budget_survives_a_process_that_dies_right_after_reserving(tmp_path: Path) -> None:
    ledger = _running(tmp_path)
    ledger.open_phase(Phase.BASELINE_CONNECT)
    script = "\n".join(
        [
            "import os, sys",
            "from pathlib import Path",
            f"sys.path.insert(0, {str(REPO_ROOT)!r})",
            "from scripts.m2harness.ledger import TOKEN, Ledger, Phase",
            f"ledger = Ledger.open(Path({str(ledger.path)!r}))",
            "ledger.reserve(TOKEN, phases=frozenset(Phase), pid=os.getpid())",
            "os._exit(9)",
        ]
    )
    completed = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, timeout=60)
    assert completed.returncode == 9
    survivor = Ledger.open(ledger.path)
    assert survivor.counts() == {TOKEN: 1, SELLER: 0}
    [row] = survivor.rows("requests")
    # Spent although no response was ever recorded, and the step cannot be sent again.
    assert (row["label"], row["outcome"]) == ("T1", "RESERVED")
    assert _refused(survivor, TOKEN) is Refusal.UNPLANNED_REQUEST
