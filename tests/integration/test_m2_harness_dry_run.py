"""The M2 harness end to end in DRY mode (Issue #46 §2).

The unmodified application runs in child processes with the budget gate under the real caller,
through restarts, the crash boundary and the evidence, with a fake provider standing where the
network would be. SmartStore requests sent by these tests: zero.

Besides the default rehearsal, the scenarios drive the gate's contingencies: a missed crash
boundary, a second miss (never a T4c), a measured A3 mismatch, and seller-cap exhaustion across
resumed invocations.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.system.secret_scan import scan
from scripts.m2harness.campaign import resume_dry, run_dry
from scripts.m2harness.evidence import validate
from scripts.m2harness.fake_provider import Scenario
from scripts.m2harness.ledger import SELLER, TOKEN, Ledger, LedgerError, State
from scripts.m2harness.paths import CampaignPaths

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _evidence(paths: CampaignPaths) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(paths.evidence.read_text("utf-8"))
    assert validate(document) == []
    return document


def _slots(document: dict[str, Any]) -> dict[str, str]:
    return {slot["evidence_id"]: slot["status"] for slot in document["slots"]}


def _no_fixture_value_in_evidence(paths: CampaignPaths, *extra: str) -> None:
    scenario = Scenario.load(paths.scenario)
    values = {
        "client_secret": scenario.client_secret,
        "client_id": scenario.client_id,
        "account_uid": scenario.account_uid,
        "account_id": scenario.account_id,
    } | {f"extra_{index}": value for index, value in enumerate(extra)}
    assert scan([paths.evidence_dir], values)["total_hits"] == 0


def test_the_default_dry_rehearsal_runs_the_whole_campaign_without_the_provider(
    tmp_path: Path,
) -> None:
    out = tmp_path / "dry"
    env = {name: value for name, value in os.environ.items() if not name.startswith("ICBM_")}
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "m2_acceptance.py"), "dry", "--out", str(out)],
        cwd=REPO_ROOT,
        env=env | {"PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:]
    # The preflight ends at the durable STOP with nothing spent, before the run is started.
    assert "STOP: AWAITING_REAL_PROVIDER_APPROVAL. SmartStore requests sent: 0" in completed.stdout
    paths = CampaignPaths(out)
    document = _evidence(paths)
    assert (document["mode"], document["campaign_state"]) == ("DRY", "COMPLETED")
    assert document["budget"]["used"] == {TOKEN: 3, SELLER: 4, "OTHER": 0}
    labels = [row["label"] for row in document["requests"]]
    assert labels == ["T1", "A1", "A1b", "A2", "T4a", "T5", "A3"]
    assert {(row["outcome"], row["http_status"]) for row in document["requests"]} == {
        ("RESPONDED", 200)
    }
    assert document["budget"]["blocked_before_send"] == []
    [attempt] = document["crash"]["attempts"]
    assert (attempt["label"], attempt["verdict"], attempt["exit_code"]) == (
        "T4a",
        "BOUNDARY_EXERCISED",
        86,
    )
    assert attempt["marker"]["candidate_scan"]["total_hits"] == 0
    recovery = document["crash"]["recovery"]
    assert (recovery["crash_dir_bound"], recovery["crash_dir_session_generation"]) == (False, 1)
    assert (document["identity"]["a3_comparison"], document["identity"]["a3_reason"]) == (
        "MATCH",
        "SAME_ACCOUNT",
    )
    assert _slots(document) == {
        "SMARTSTORE-R0-TOKEN": "PASS",
        "SMARTSTORE-R0-SELLER-ACCOUNT": "PASS",
        "SMARTSTORE-R0-FIRST-TOKEN-CRASH": "PASS",
        "SMARTSTORE-A0-PERMISSION": "PASS",
        "SMARTSTORE-R0-TOKEN-REISSUE-WINDOW": "DEFERRED_LONG_HORIZON",
        "SMARTSTORE-R0-APP-REAUTH": "BLOCKED_BY_TIME",
    }
    assert document["reconcile"]["match"] is True
    assert (
        document["scan"]["local"]["total_hits"] == document["scan"]["evidence"]["total_hits"] == 0
    )
    assert {call["endpoint_id"] for call in document["calls"]} == {TOKEN, SELLER}
    _no_fixture_value_in_evidence(paths)


def test_t4b_is_spent_only_after_t4a_missed_the_boundary(tmp_path: Path) -> None:
    state, paths = run_dry(tmp_path / "dry", scenario=Scenario.fixture(failures={"T4a": 500}))
    assert state is State.COMPLETED
    document = _evidence(paths)
    first, second = document["crash"]["attempts"]
    assert (first["label"], first["verdict"]) == ("T4a", "BOUNDARY_NOT_EXERCISED")
    assert {"EXIT_NOT_AT_BOUNDARY", "NO_MARKER", "NO_COMPLETED_TOKEN_RESPONSE"} <= set(
        first["reasons"]
    )
    assert (second["label"], second["verdict"]) == ("T4b", "BOUNDARY_EXERCISED")
    assert document["budget"]["used"][TOKEN] == 4


def test_a_second_miss_exhausts_the_crash_sub_budget_and_there_is_no_t4c(tmp_path: Path) -> None:
    scenario = Scenario.fixture(failures={"T4a": 500, "T4b": 500})
    state, paths = run_dry(tmp_path / "dry", scenario=scenario)
    assert state is State.BUDGET_EXHAUSTED
    ledger = Ledger.open(paths.ledger)
    assert [row["label"] for row in ledger.rows("crash_attempts")] == ["T4a", "T4b"]
    assert ledger.counts()[TOKEN] == 3  # T1, T4a, T4b: five tokens remain, and no third attempt
    assert "CRASH_RECOVERY" not in {row["phase"] for row in ledger.rows("phases")}
    document = _evidence(paths)
    reasons = [row["reason"] for row in document["budget"]["blocked_before_send"]]
    assert reasons == ["CRASH_SUB_BUDGET_EXHAUSTED"]
    assert _slots(document)["SMARTSTORE-R0-FIRST-TOKEN-CRASH"] == "FAIL"
    with pytest.raises(LedgerError):
        resume_dry(paths)  # a terminal campaign never runs again


def test_an_a3_mismatch_stops_for_contract_review_and_binds_nothing(tmp_path: Path) -> None:
    other = "dry-account-uid-of-someone-else"
    scenario = Scenario.fixture(account_uid_at={"A3": other})
    state, paths = run_dry(tmp_path / "dry", scenario=scenario)
    assert state is State.STOPPED_FOR_CONTRACT_REVIEW
    document = _evidence(paths)
    identity = document["identity"]
    assert (identity["a3_comparison"], identity["a3_reason"]) == ("MISMATCH", "DIFFERENT_ACCOUNT")
    assert document["crash"]["recovery"]["crash_dir_bound"] is False
    [slot] = [s for s in document["slots"] if s["evidence_id"] == "SMARTSTORE-R0-FIRST-TOKEN-CRASH"]
    assert slot["status"] == "MEASURED_CONTRACT_REVIEW_REQUIRED"
    assert slot["contract_impact"].startswith("REVIEW:")
    _no_fixture_value_in_evidence(paths, other)


def test_the_seller_cap_refuses_the_seventh_read_before_send(tmp_path: Path) -> None:
    """M2.md §6.1: a failed planned read is repeated only by a new invocation, up to 6/6; the
    seventh is refused before send, and the campaign becomes BUDGET_EXHAUSTED."""
    state, paths = run_dry(tmp_path / "dry", scenario=Scenario.fixture(failures={"A2": 503}))
    assert state is State.STOPPED_STEP_FAILED
    ledger = Ledger.open(paths.ledger)
    assert ledger.counts() == {TOKEN: 1, SELLER: 3}
    for spent in (4, 5, 6):
        assert resume_dry(paths) is State.STOPPED_STEP_FAILED
        assert ledger.counts()[SELLER] == spent
    assert resume_dry(paths) is State.BUDGET_EXHAUSTED
    assert ledger.counts() == {TOKEN: 1, SELLER: 6}
    assert [(row["endpoint_id"], row["reason"]) for row in ledger.rows("refusals")] == [
        (SELLER, "CAP_REACHED")
    ]
    document = _evidence(paths)
    # The seventh read reached the caller but no transport: it is logged as never sent.
    blocked = [call for call in document["calls"] if call["transmission_phase"] == "EGRESS_BLOCKED"]
    assert [(call["endpoint_id"], call["remote_outcome"]) for call in blocked] == [
        (SELLER, "NOT_APPLIED_PROVEN")
    ]
