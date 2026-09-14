"""FIRST-TOKEN-CRASH boundary verification (Issue #46 §2.3; docs/acceptance/M2.md §5.4).

A marker is accepted only with the real post-response, pre-commit evidence behind it. A forged,
stale or mismatched marker, a death anywhere else, a token call that did not complete, a committed
session, or the candidate found on disk is never a PASS. The crash data directory is a real,
migrated one. No process here calls a provider.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest

from scripts.m2harness import crash
from scripts.m2harness.ledger import TOKEN, CrashVerdict, Ledger, Mode, Phase

pytestmark = pytest.mark.integration

NONCE = b"n" * 32
CAMPAIGN = "m2-unit-crash"
PID = 4242


@pytest.fixture
def setup(tmp_path: Path, migrated_template: Path) -> tuple[Ledger, Path, Path]:
    data_dir = tmp_path / "crash"
    data_dir.mkdir()
    shutil.copyfile(migrated_template, data_dir / "icbm.db")
    ledger = Ledger.create(
        tmp_path / "ledger.sqlite3", campaign_id=CAMPAIGN, mode=Mode.DRY, nonce="n"
    )
    ledger.mark_preflight_passed("d" * 64, "a" * 40)
    ledger.begin_dry_run()
    ledger.open_phase(Phase.CRASH_T4A)
    return ledger, data_dir, tmp_path / "marker.json"


def _respond(ledger: Ledger, status: int | None = 200) -> int:
    reservation = ledger.reserve(TOKEN, phases=frozenset(Phase), pid=PID)
    if status is not None:
        ledger.complete(reservation.seq, http_status=status, latency_ms=5.0)
    return reservation.seq


def _fields(seq: int, **changes: Any) -> dict[str, Any]:
    fields = {
        "marker": crash.MARKER,
        "campaign_id": CAMPAIGN,
        "phase": "CRASH_T4A",
        "crash_attempt": 1,
        "label": "T4a",
        "reservation_seq": seq,
        "pid": PID,
        "at": "2026-09-15T00:00:00.000+00:00",
        "token_type": "Bearer",
        "expires_in": 10800,
        "credential_generation": 1,
        "candidate_scan": {"files_scanned": 9, "total_hits": 0},
    }
    return fields | changes


def _verify(
    setup: tuple[Ledger, Path, Path], *, exit_code: int | None = crash.EXIT_AT_BOUNDARY
) -> crash.CrashResult:
    ledger, data_dir, marker = setup
    return crash.verify_boundary(
        exit_code=exit_code,
        marker_path=marker,
        nonce=NONCE,
        ledger=ledger,
        campaign_id=CAMPAIGN,
        crash_attempt=1,
        data_dir=data_dir,
    )


def test_the_boundary_is_accepted_only_when_all_the_evidence_agrees(
    setup: tuple[Ledger, Path, Path],
) -> None:
    ledger, _, marker = setup
    seq = _respond(ledger)
    crash.write_marker(marker, _fields(seq), NONCE)
    result = _verify(setup)
    assert (result.verdict, result.reasons) == (CrashVerdict.BOUNDARY_EXERCISED, ())
    assert result.reservation_seq == seq
    assert result.marker is not None and result.marker["token_type"] == "Bearer"


def test_a_marker_without_the_attempt_nonce_is_refused(setup: tuple[Ledger, Path, Path]) -> None:
    ledger, _, marker = setup
    crash.write_marker(marker, _fields(_respond(ledger)), b"x" * 32)  # stale, copied or forged
    result = _verify(setup)
    assert result.verdict is CrashVerdict.BOUNDARY_NOT_EXERCISED
    assert crash.Reason.MARKER_NOT_AUTHENTIC in result.reasons
    assert result.marker is None  # nothing an unauthenticated marker says is kept


@pytest.mark.parametrize("status", [None, 500, 401])
def test_a_marker_without_a_completed_token_response_is_refused(
    setup: tuple[Ledger, Path, Path], status: int | None
) -> None:
    ledger, _, marker = setup
    crash.write_marker(marker, _fields(_respond(ledger, status)), NONCE)
    result = _verify(setup)
    assert result.verdict is CrashVerdict.BOUNDARY_NOT_EXERCISED
    assert crash.Reason.NO_COMPLETED_TOKEN_RESPONSE in result.reasons


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 1},
        {"reservation_seq": 99},
        {"campaign_id": "m2-another"},
        {"crash_attempt": 2, "label": "T4b", "phase": "CRASH_T4B"},
        {"marker": "SOMETHING_ELSE"},
    ],
)
def test_an_authentic_marker_of_another_process_or_attempt_is_refused(
    setup: tuple[Ledger, Path, Path], change: dict[str, Any]
) -> None:
    ledger, _, marker = setup
    crash.write_marker(marker, _fields(_respond(ledger), **change), NONCE)
    result = _verify(setup)
    assert result.verdict is CrashVerdict.BOUNDARY_NOT_EXERCISED
    assert crash.Reason.MARKER_MISMATCH in result.reasons


@pytest.mark.parametrize("exit_code", [None, 0, 1, 2, crash.EXIT_BOUNDARY_REFUSED])
def test_a_death_anywhere_else_is_not_the_boundary(
    setup: tuple[Ledger, Path, Path], exit_code: int | None
) -> None:
    ledger, _, marker = setup
    crash.write_marker(marker, _fields(_respond(ledger)), NONCE)
    result = _verify(setup, exit_code=exit_code)
    assert result.verdict is CrashVerdict.BOUNDARY_NOT_EXERCISED
    assert crash.Reason.EXIT_NOT_AT_BOUNDARY in result.reasons


def test_no_marker_is_no_boundary(setup: tuple[Ledger, Path, Path]) -> None:
    _respond(setup[0])
    result = _verify(setup)
    assert result.verdict is CrashVerdict.BOUNDARY_NOT_EXERCISED
    assert crash.Reason.NO_MARKER in result.reasons


def test_a_committed_session_is_a_failure_not_a_miss(setup: tuple[Ledger, Path, Path]) -> None:
    ledger, data_dir, marker = setup
    crash.write_marker(marker, _fields(_respond(ledger)), NONCE)
    sessions = data_dir / "marketplace_sessions"
    sessions.mkdir()
    (sessions / "smartstore.enc").write_bytes(b"a committed bundle")
    result = _verify(setup)
    assert result.verdict is CrashVerdict.COMMITTED_BEFORE_CRASH
    assert crash.Reason.SESSION_COMMITTED in result.reasons


def test_the_candidate_found_on_disk_at_the_boundary_is_a_failure(
    setup: tuple[Ledger, Path, Path],
) -> None:
    ledger, _, marker = setup
    leaked = {"files_scanned": 9, "total_hits": 1}
    crash.write_marker(marker, _fields(_respond(ledger), candidate_scan=leaked), NONCE)
    result = _verify(setup)
    assert result.verdict is CrashVerdict.CANDIDATE_LEAKED
    assert result.reasons == (crash.Reason.CANDIDATE_FOUND_IN_ARTIFACTS,)
