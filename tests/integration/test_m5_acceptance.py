"""The M5 offline acceptance harness, run for real (Issue #89 PR-F §A).

One full run on a fresh root, in its own interpreter, exactly as an operator runs it — then the
report is read: every required proof is a passing check, the hard-zero counters are measured
zero, the canary readiness is BLOCKED with the contracts it waits on named, the report carries no
path, URL or secret-like material, and its digest catches tampering.

The root and checkout gates are tested on their own, because a run that reaches a scenario at all
has already passed them.
"""

import json
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from scripts.m4accept.checkout import Checkout
from scripts.m5accept.root import MARKER, REPORT

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# The command line in a fresh interpreter, exactly as an operator runs it, except that the checkout
# is judged clean: these runs judge the scenarios, and a developer's uncommitted edit (or a
# negative control) must not refuse them. The checkout gate itself is tested separately, and the
# forbidden modules a run finds preloaded are then the harness's own import graph.
RUNNER = "\n".join(
    (
        "import sys",
        "from dataclasses import replace",
        "sys.path.insert(0, sys.argv[1])",
        "from scripts.m5accept import harness",
        "measured = harness.probe_checkout",
        "harness.probe_checkout = lambda: replace(measured(), tracked_changes=0,"
        " hidden_tracked_files=0, untracked_files=0, ignored_sources=0)",
        "from scripts.m5_acceptance import main",
        "raise SystemExit(main(sys.argv[2:]))",
    )
)
CLEAN = Checkout(
    code_sha="0" * 40,
    tracked_changes=0,
    hidden_tracked_files=0,
    untracked_files=0,
    ignored_sources=0,
    code_outside_checkout=0,
)

# Every acceptance-plan case of the kickoff, and the check that proves it.
REQUIRED_CHECKS = {
    "1 one Intent and identity per Snapshot, across a restart": (
        "s1.one_intent_per_snapshot",
        "s1.identity_survives_restart",
    ),
    "2 a double dispatch neither competes nor bypasses the backoff": (
        "s2.one_live_job",
        "s2.retry_scheduled",
        "s2.same_job_after_retry",
        "s2.backoff_not_bypassed",
        "s2.create_count_unchanged",
        "s2.one_attempt_per_send",
    ),
    "3 an UNKNOWN is never resent and blocks its scope": (
        "s3.intent_unknown",
        "s3.no_blind_resend",
        "s3.overlapping_snapshot_blocked",
    ),
    "4 a non-overlapping group stays free": ("s4.non_overlapping_group_free",),
    "5 a subset read-back is a whole-Intent mismatch": (
        "s5.subset_is_whole_mismatch",
        "s5.no_create_for_missing_items",
    ),
    "6 a sibling's success is preserved and PARTIAL is derived": (
        "s6.sibling_success_preserved",
        "s6.batch_partial_is_derived",
    ),
    "7 proven absence keeps history and frees a fresh start": (
        "s7.absence_needs_provider_evidence",
        "s7.history_preserved_after_proven_absence",
        "s7.fresh_unit_allowed_after_absence",
    ),
    "8 the read-back compares to the immutable Snapshot": (
        "s8.readback_compares_to_the_frozen_snapshot",
    ),
    "9 provider option order does not break correspondence": (
        "s9.option_order_does_not_break_correspondence",
    ),
    "10 no supplier hotlink is publishable": (
        "s10.no_supplier_hotlink_in_payload",
        "s10.url_bearing_value_never_reaches_a_payload",
    ),
    "11 a blocked candidate uploads nothing": ("s11.blocked_candidate_permits_no_upload",),
    "12 dependency drift sends nothing": (
        "s12.dependency_drift_sends_nothing",
        "s12.no_attempt_opened_on_drift",
    ),
    "13 an operator assertion alone resolves no UNKNOWN": (
        "s13.no_operator_assertion_evidence",
        "s13.unadopted_lookup_resolves_nothing",
    ),
    "14 every durable digest is over the sanitized canonical": (
        "s14.payload_hash_is_over_the_sanitized_canonical",
    ),
    "15 the brakes, their boundaries and their restarts": (
        "s15.policy_failure_pauses_the_scope",
        "s15.brake_survives_restart",
        "s15.attempts_preserved_across_restart",
        "s15.authentication_never_releases_a_policy_brake",
        "s15.explicit_resume_moves_the_boundary",
        "s15.attempts_unchanged_by_resume",
        "s15.auth_failure_pauses_the_scope",
        "s15.auth_brake_refuses_an_operator_resume",
    ),
    "16 a replay after confirmation is a no-op": ("s16.replay_is_a_no_op",),
    "17 the upstream histories are unchanged": ("s17.upstream_history_unchanged",),
    "the provider boundary": (
        "boundary.declarations_name_their_own_gap",
        "boundary.provider_transport_unloadable",
        "boundary.real_wire_projection_refuses",
        "boundary.create_not_adopted",
        "boundary.upload_adopted_but_unreachable",
        "boundary.search_not_adopted",
        "boundary.product_registration_write_unverified",
        "boundary.no_provider_audit_event",
    ),
    "the hard zero": (
        "hard_zero.no_external_network",
        "hard_zero.no_provider_module_loaded",
        "hard_zero.nothing_provider_shaped_preloaded",
        "hard_zero.no_write_outside_the_root",
    ),
    "18 the durable preparation is authored, survives a restart and needs no job": (
        "s18.preparation_survives_restart",
        "s18.evaluated_without_a_job",
        "s18.authored_inputs_reach_ready",
        "s18.snapshot_proves_its_authored_revision",
        "s18.edit_appends_and_never_rewrites",
    ),
    "the canary plan": (
        "canary.many_units_exist_and_one_is_named",
        "canary.blocked_while_contracts_are_unadopted",
    ),
}


@dataclass(frozen=True)
class Accepted:
    root: Path
    report: dict[str, Any]
    exit_code: int


def _command_line(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", RUNNER, str(REPO_ROOT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        check=False,
    )


@pytest.fixture(scope="module")
def accepted(tmp_path_factory: pytest.TempPathFactory) -> Accepted:
    root = tmp_path_factory.mktemp("m5-acceptance") / "fresh-root"
    done = _command_line("--root", str(root))
    assert (root / REPORT).is_file(), done.stderr[-4000:]
    return Accepted(root, json.loads((root / REPORT).read_text("utf-8")), done.returncode)


def _checks(report: dict[str, Any]) -> dict[str, bool]:
    return {str(check["name"]): bool(check["passed"]) for check in report["checks"]}


# ---------------------------------------------------------------- the passing run


def test_a_fresh_root_passes_every_check(accepted: Accepted) -> None:
    report = accepted.report
    assert accepted.exit_code == 0
    assert report["problems"] == []
    assert report["checks_passed"] == report["checks_total"] >= 50
    assert report["mode"] == "OFFLINE_SYNTHETIC" and report["claim"].startswith("HARNESS_RUN")
    assert report["execution_mode"] == "DRY_RUN"
    assert report["database_revision"] == "0029_g3_restore_retention"


@pytest.mark.parametrize("names", REQUIRED_CHECKS.values(), ids=REQUIRED_CHECKS.keys())
def test_each_required_proof_is_a_passing_check(accepted: Accepted, names: tuple[str, ...]) -> None:
    checks = _checks(accepted.report)
    assert all(checks.get(name) is True for name in names), [n for n in names if not checks.get(n)]


def test_the_report_states_what_the_run_declared_and_what_it_proved(accepted: Accepted) -> None:
    report = accepted.report
    # What is declared instead of proven, and **why** each one is a declaration: the gaps are
    # different, and a PASS here proves a different thing about each.
    declared = report["declared_seams"]
    assert set(declared) == {
        "CREATE_HANDOFF",
        "READ_BACK",
        "RECONCILE_LOOKUP",
        "WIRE_PROJECTION",
        "PUBLISHED_STATE",
        "ACCOUNT_BINDING",
        "LIVE_AUTHORITY",
    }
    # Gate 3 area 1: the production safety stack refuses every CREATE under M0, so the harness
    # authority that admits is declared as a stand-in, never hidden.
    assert declared["LIVE_AUTHORITY"] == {
        "reason": "M0_EXECUTION_POLICY_REFUSES_LIVE",
        "endpoint_id": None,
        "endpoint_adopted": None,
    }
    assert declared["CREATE_HANDOFF"] == {
        "reason": "ENDPOINT_NOT_ADOPTED",
        "endpoint_id": "SMARTSTORE_PRODUCT_CREATE_V2",
        "endpoint_adopted": False,
    }
    assert declared["RECONCILE_LOOKUP"]["reason"] == "ENDPOINT_NOT_ADOPTED"
    # The read-back contract **is** adopted: it is declared because an offline run has no provider
    # to answer it, and it is never reported as unadopted.
    assert declared["READ_BACK"] == {
        "reason": "OFFLINE_SYNTHETIC_PROVIDER_RESPONSE",
        "endpoint_id": "SMARTSTORE_ORIGIN_PRODUCT_READ_V2",
        "endpoint_adopted": True,
    }
    # A wire contract and a published state are gaps of their own, not endpoints.
    assert declared["WIRE_PROJECTION"]["reason"] == "WIRE_CONTRACT_UNPROVEN"
    assert declared["PUBLISHED_STATE"] == {
        "reason": "PUBLISHED_STATE_UNPROVEN",
        "endpoint_id": None,
        "endpoint_adopted": None,
    }
    assert declared["ACCOUNT_BINDING"]["reason"] == "OFFLINE_SYNTHETIC_PROVIDER_RESPONSE"
    assert report["account_scope"]["synthetic_connect_binding"] is True
    adoption = report["endpoint_adoption"]
    assert adoption["SMARTSTORE_PRODUCT_CREATE_V2"] is False
    assert adoption["SMARTSTORE_PRODUCT_IMAGE_UPLOAD"] is True
    assert adoption["SMARTSTORE_PRODUCT_SEARCH"] is False
    assert adoption["SMARTSTORE_ORIGIN_PRODUCT_READ_V2"] is True
    assert report["boundary"]["marketplace_mutations"] == 0
    assert report["boundary"]["real_wire_projection_sendable"] is False


def test_the_canary_plan_is_blocked_and_names_its_missing_contracts(accepted: Accepted) -> None:
    canary = accepted.report["canary_readiness"]
    assert canary["verdict"] == "BLOCKED"
    assert canary["write_status"] == "UNVERIFIED" and canary["execution_mode"] == "DRY_RUN"
    assert "CREATE_ADOPTED" in canary["missing"]
    assert set(canary["unadopted_endpoints"]) >= {
        "SMARTSTORE_PRODUCT_CREATE_V2",
        "SMARTSTORE_PRODUCT_SEARCH",
    }


def test_the_hard_zero_counters_are_measured_zero(accepted: Accepted) -> None:
    guards = accepted.report["guards"]
    assert guards["external_network_attempts"] == 0
    assert guards["egress_grants_opened"] == 0
    assert guards["writes_outside_root"] == 0
    assert guards["forbidden_modules_preloaded"] == []
    assert guards["forbidden_modules_loaded_during_run"] == []
    # The only import the guard refused is the boundary phase's deliberate probe.
    assert guards["forbidden_imports_blocked"] == ["integrations.marketplaces.smartstore.caller"]
    assert accepted.report["preserved_campaign_access"] == 0


def test_the_report_is_sanitized_and_its_digest_catches_tampering(accepted: Accepted) -> None:
    from scripts.m4accept import evidence

    text = json.dumps(accepted.report)
    assert "://" not in text and "C:\\" not in text
    assert evidence.leaks(accepted.report) == []
    assert evidence.verify_report(accepted.report)
    tampered = dict(accepted.report)
    tampered["checks_passed"] = int(tampered["checks_passed"]) + 1
    assert not evidence.verify_report(tampered)


def test_the_root_is_settled_and_never_reused(accepted: Accepted) -> None:
    marker = json.loads((accepted.root / MARKER).read_text("utf-8"))
    assert marker["state"] == "PASSED" and marker["schema"] == "icbm-m5-acceptance-root/v1"
    again = _command_line("--root", str(accepted.root))
    assert again.returncode == 2 and "already been used" in again.stderr


# ---------------------------------------------------------------- the gates


@pytest.mark.parametrize("name", ["m3-accept-01", "m3_recon_02", "M3-Accept-Live"])
def test_a_preserved_campaign_root_is_refused_before_anything_exists(
    tmp_path: Path, name: str
) -> None:
    root = tmp_path / name / "run"
    done = _command_line("--root", str(root))
    assert done.returncode == 2 and "preserved campaign" in done.stderr
    assert not root.exists()


def test_a_root_inside_the_repository_is_refused(tmp_path: Path) -> None:
    done = _command_line("--root", str(REPO_ROOT / "var" / "m5-acceptance"))
    assert done.returncode == 2 and "repository" in done.stderr
    assert not (REPO_ROOT / "var" / "m5-acceptance").exists()


def test_a_root_that_is_not_fresh_is_refused(tmp_path: Path) -> None:
    used = tmp_path / "not-fresh"
    used.mkdir()
    (used / "something.txt").write_text("not this run's", "utf-8")
    done = _command_line("--root", str(used))
    assert done.returncode == 2 and "not empty" in done.stderr


DIRT = {
    "a tracked change": {"tracked_changes": 1},
    "an untracked file": {"untracked_files": 1},
    "an ignored source": {"ignored_sources": 1},
    "a hidden tracked file": {"hidden_tracked_files": 1},
    "code outside the checkout": {"code_outside_checkout": 1},
    "no commit at all": {"code_sha": None},
    "an unmeasurable count": {"untracked_files": None},
}


@pytest.mark.parametrize("dirt", DIRT.values(), ids=DIRT.keys())
def test_a_checkout_that_is_not_exactly_its_commit_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dirt: dict[str, Any]
) -> None:
    from scripts.m4accept.checkout import CheckoutRefused
    from scripts.m5accept import harness

    monkeypatch.setattr(harness, "probe_checkout", lambda: replace(CLEAN, **dirt))
    root = tmp_path / "refused"
    with pytest.raises(CheckoutRefused):
        harness.run_acceptance(root, {})
    assert not root.exists()
