"""Real-mode gates (Issue #46 §2.4; comment 5669037896 point 3). Every gate is evaluated before
any reservation and without side effects; each one alone refuses a real run; under pytest or CI
no combination of flags can pass them; and no CLI path satisfies the approval implicitly. No test
here writes to the OS keyring, starts an application process or reaches a network."""

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.m2harness import cli
from scripts.m2harness.gates import (
    APPROVAL_KIND,
    APPROVAL_MAX_AGE,
    ApprovalRefused,
    RealRequest,
    dedicated_problems,
    digest,
    issue_approval,
    real_mode_gates,
)
from scripts.m2harness.keyrings import SCOPE_ENV, SCOPED_BACKEND
from scripts.m2harness.ledger import Ledger, Mode, Phase, ReservationRefused, State
from scripts.m2harness.paths import CampaignPaths

REPO_ROOT = Path(__file__).resolve().parents[2]
HEAD = "c" * 40
CAMPAIGN = "m2-unit-gates"
ENV = {"ICBM_SMARTSTORE_RENEWAL_MARGIN_S": "600"}
# Impossible under pytest by design: the test run itself, and the OS keyring probe it skips.
UNDER_PYTEST = {"not_ci_or_test", "campaign_registered"}


class Checkout:
    def __init__(self, head: str = HEAD, dirty: int = 0) -> None:
        self._head, self._dirty = head, dirty

    def head(self) -> str:
        return self._head

    def dirty(self) -> int:
        return self._dirty


def _approve(paths: CampaignPaths, ledger: Ledger, **changes: object) -> str:
    document = {
        "kind": APPROVAL_KIND,
        "campaign_id": CAMPAIGN,
        "approved_sha": HEAD,
        "preflight_digest": digest(paths.preflight),
        "issued_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "nonce": "n",
    } | changes
    paths.approval.write_text(json.dumps(document), "utf-8")
    value = digest(paths.approval)
    ledger.issue_approval(value, approved_sha=HEAD)
    return value


def _campaign(
    tmp_path: Path, *, mode: Mode = Mode.REAL, approve: bool = True, head: str = HEAD
) -> CampaignPaths:
    paths = CampaignPaths(tmp_path / "campaign")
    paths.root.mkdir()
    ledger = Ledger.create(paths.ledger, campaign_id=CAMPAIGN, mode=mode, nonce="nonce")
    paths.preflight.parent.mkdir()
    paths.preflight.write_text(json.dumps({"kind": "M2_PREFLIGHT_STATUS", "result": "PASS"}))
    ledger.mark_preflight_passed(digest(paths.preflight), head)
    if approve and mode is Mode.REAL:
        _approve(paths, ledger)
    return paths


def _gates(
    paths: CampaignPaths,
    *,
    request: RealRequest | None = None,
    environ: dict[str, str] = ENV,
    checkout: Checkout | None = None,
    interactive: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> tuple[set[str], str | None]:
    extra: dict[str, Any] = {"clock": clock} if clock else {}
    gates, approval = real_mode_gates(
        request or RealRequest(CAMPAIGN, HEAD, CAMPAIGN, True),
        paths,
        environ=environ,
        checkout=checkout or Checkout(),
        interactive=interactive,
        registered=lambda campaign: True,
        **extra,
    )
    return {gate.name for gate in gates if not gate.passed} - UNDER_PYTEST, approval


def _snapshot(paths: CampaignPaths) -> tuple[object, ...]:
    ledger = Ledger.open(paths.ledger)
    return (
        ledger.campaign(),
        ledger.rows("requests"),
        ledger.rows("refusals"),
        ledger.rows("events"),
    )


def test_under_pytest_no_setup_can_pass_the_gates(tmp_path: Path) -> None:
    paths = _campaign(tmp_path)
    gates, approval = real_mode_gates(
        RealRequest(CAMPAIGN, HEAD, CAMPAIGN, True),
        paths,
        environ=ENV,
        checkout=Checkout(),
        interactive=True,
        registered=lambda campaign: True,
    )
    assert {gate.name for gate in gates if not gate.passed} == UNDER_PYTEST
    assert approval is None


@pytest.mark.parametrize(
    ("request_", "gate"),
    [
        (RealRequest(CAMPAIGN, HEAD, CAMPAIGN, False), "real_provider_flag"),
        (RealRequest(None, HEAD, CAMPAIGN, True), "campaign_id_explicit"),
        (RealRequest(CAMPAIGN, HEAD, "m2-another", True), "acknowledgement_matches"),
        (RealRequest(CAMPAIGN, "d" * 40, CAMPAIGN, True), "approved_sha_is_head"),
        (RealRequest(CAMPAIGN, HEAD[:12], CAMPAIGN, True), "approved_sha_is_head"),
        (RealRequest("m2-another", HEAD, "m2-another", True), "campaign_id_matches_ledger"),
    ],
)
def test_each_invocation_gate_refuses_on_its_own(
    tmp_path: Path, request_: RealRequest, gate: str
) -> None:
    failing, approval = _gates(_campaign(tmp_path), request=request_)
    assert gate in failing
    assert approval is None


def test_a_valid_setup_fails_no_other_gate(tmp_path: Path) -> None:
    assert _gates(_campaign(tmp_path)) == (set(), None)


def test_a_dirty_tree_refuses(tmp_path: Path) -> None:
    assert _gates(_campaign(tmp_path), checkout=Checkout(dirty=3))[0] == {"tree_clean"}


@pytest.mark.parametrize("environ", [{}, {"ICBM_SMARTSTORE_RENEWAL_MARGIN_S": "599"}])
def test_the_renewal_margin_must_be_600(tmp_path: Path, environ: dict[str, str]) -> None:
    assert _gates(_campaign(tmp_path), environ=environ)[0] == {"renewal_margin_600"}


def test_an_interactive_operator_is_required(tmp_path: Path) -> None:
    assert _gates(_campaign(tmp_path), interactive=False)[0] == {"interactive_operator"}


def test_the_campaign_dir_must_be_dedicated(tmp_path: Path) -> None:
    paths = _campaign(tmp_path)
    ordinary = ENV | {"ICBM_DATA_DIR": str(tmp_path)}
    assert _gates(paths, environ=ordinary)[0] == {"dedicated_campaign_dir"}
    assert dedicated_problems(REPO_ROOT / "var" / "m2-campaign", {}) != []
    assert dedicated_problems(REPO_ROOT / "scripts", {}) != []
    assert dedicated_problems(tmp_path / "elsewhere", {}) == []


def test_a_missing_or_altered_ledger_refuses(tmp_path: Path) -> None:
    paths = _campaign(tmp_path)
    with closing(sqlite3.connect(paths.ledger)) as db:
        db.execute("DROP TRIGGER requests_seller_cap")
        db.commit()
    assert "ledger_readable" in _gates(paths)[0]
    paths.ledger.unlink()
    assert "ledger_readable" in _gates(paths)[0]


def test_a_dry_ledger_never_runs_for_real(tmp_path: Path) -> None:
    failing, approval = _gates(_campaign(tmp_path, mode=Mode.DRY))
    assert {"ledger_mode_real", "approval_material"} <= failing
    assert approval is None


def test_a_running_campaign_refuses_and_its_approval_is_used(tmp_path: Path) -> None:
    paths = _campaign(tmp_path, approve=False)
    ledger = Ledger.open(paths.ledger)
    ledger.begin_real_run(_approve(paths, ledger))
    assert {"ledger_waiting_for_approval", "approval_current"} <= _gates(paths)[0]
    ledger.finish(State.STOPPED_STEP_FAILED, reason="TEST")
    # Stopped and resumable, but the approval was consumed: it never counts twice.
    assert _gates(paths)[0] == {"approval_current"}


def test_an_exhausted_campaign_refuses(tmp_path: Path) -> None:
    paths = _campaign(tmp_path, approve=False)
    ledger = Ledger.open(paths.ledger)
    ledger.begin_real_run(_approve(paths, ledger))
    ledger.open_phase(Phase.BASELINE_CONNECT)
    with pytest.raises(ReservationRefused):
        ledger.reserve("SMARTSTORE_PRODUCT_CREATE_V2", phases=frozenset(Phase), pid=1)
    assert ledger.campaign().state is State.BUDGET_EXHAUSTED
    assert "budget_not_exhausted" in _gates(paths)[0]


def test_missing_or_altered_preflight_evidence_refuses(tmp_path: Path) -> None:
    paths = _campaign(tmp_path)
    paths.preflight.write_text(json.dumps({"kind": "M2_PREFLIGHT_STATUS", "result": "FAIL"}))
    assert {"preflight_passed", "approval_material"} <= _gates(paths)[0]
    paths.preflight.unlink()
    assert {"preflight_passed", "approval_material"} <= _gates(paths)[0]


def test_a_preflight_at_another_sha_refuses(tmp_path: Path) -> None:
    paths = _campaign(tmp_path, head="e" * 40)
    assert "preflight_passed" in _gates(paths)[0]


def test_missing_approval_refuses(tmp_path: Path) -> None:
    failing, approval = _gates(_campaign(tmp_path, approve=False))
    assert failing == {"approval_material"}
    assert approval is None


@pytest.mark.parametrize(
    "change",
    [
        {"campaign_id": "m2-another"},
        {"approved_sha": "e" * 40},
        {"preflight_digest": "0" * 64},
        {"kind": "SOMETHING_ELSE"},
    ],
)
def test_approval_for_another_campaign_sha_or_preflight_refuses(
    tmp_path: Path, change: dict[str, str]
) -> None:
    paths = _campaign(tmp_path, approve=False)
    _approve(paths, Ledger.open(paths.ledger), **change)
    assert _gates(paths)[0] == {"approval_material"}


def test_an_old_approval_refuses(tmp_path: Path) -> None:
    later = datetime.now(UTC) + APPROVAL_MAX_AGE + timedelta(minutes=1)
    assert _gates(_campaign(tmp_path), clock=lambda: later)[0] == {"approval_fresh"}


def test_an_approval_followed_by_anything_is_stale(tmp_path: Path) -> None:
    paths = _campaign(tmp_path)
    Ledger.open(paths.ledger).mark_preflight_passed(digest(paths.preflight), HEAD)
    assert _gates(paths)[0] == {"approval_current"}


def test_the_gates_have_no_side_effects(tmp_path: Path) -> None:
    paths = _campaign(tmp_path)
    before = _snapshot(paths)
    _gates(paths, request=RealRequest(CAMPAIGN, "d" * 40, "m2-another", False))
    _gates(paths)
    assert _snapshot(paths) == before


def test_approval_is_never_issued_under_pytest_or_ci(tmp_path: Path) -> None:
    paths = _campaign(tmp_path, approve=False)
    ledger = Ledger.open(paths.ledger)

    def confirm(phrase: str) -> bool:
        raise AssertionError("the operator must never be asked under pytest")

    with pytest.raises(ApprovalRefused) as caught:
        issue_approval(
            paths, ledger, approved_sha=HEAD, checkout=Checkout(), environ={}, confirm=confirm
        )
    assert "pytest" in str(caught.value)
    assert not paths.approval.exists()
    assert ledger.last_event("APPROVAL_ISSUED") is None


# ---------------------------------------------------------------- the command line


def test_the_default_command_is_the_dry_rehearsal(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setitem(cli.HANDLERS, "dry", lambda args: seen.append(args.command) or 0)
    assert cli.main([]) == 0
    assert seen == ["dry"]


def test_the_real_command_refuses_under_pytest_before_any_reservation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _campaign(tmp_path)
    before = _snapshot(paths)
    code = cli.main(
        [
            "real",
            "--campaign-dir",
            str(paths.root),
            "--campaign-id",
            CAMPAIGN,
            "--approved-sha",
            HEAD,
            "--acknowledge",
            CAMPAIGN,
            "--real-provider",
        ]
    )
    assert code == 2
    assert "Real SmartStore requests sent: 0." in capsys.readouterr().out
    assert _snapshot(paths) == before
    assert not paths.run_dir.exists()  # no application process was started


def test_the_approve_command_refuses_under_pytest(tmp_path: Path) -> None:
    paths = _campaign(tmp_path, approve=False)
    args = ["approve", "--campaign-dir", str(paths.root), "--campaign-id", CAMPAIGN]
    assert cli.main([*args, "--approved-sha", HEAD]) == 2
    assert not paths.approval.exists()


def test_init_refuses_a_directory_that_is_not_dedicated() -> None:
    target = REPO_ROOT / "var" / "m2-unit-init"
    assert cli.main(["init", "--campaign-dir", str(target), "--campaign-id", "m2-unit-init"]) == 2
    assert not target.exists()


@pytest.mark.parametrize("marker", [{"PYTEST_CURRENT_TEST": "inherited"}, {"CI": "true"}])
def test_a_real_campaign_process_never_builds_the_live_transport_under_ci_or_tests(
    tmp_path: Path, marker: dict[str, str]
) -> None:
    paths = _campaign(tmp_path)
    blocked = ("PYTEST_CURRENT_TEST", "CI", "GITHUB_ACTIONS")
    env = {name: value for name, value in os.environ.items() if name not in blocked}
    env |= marker | ENV | {"PYTHONIOENCODING": "utf-8"}
    # The campaign's own secret store, as the harness starts it: CI or pytest is the only obstacle.
    env |= {"PYTHON_KEYRING_BACKEND": SCOPED_BACKEND, SCOPE_ENV: CAMPAIGN}
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "m2_acceptance.py"),
            "_serve",
            "--campaign-dir",
            str(paths.root),
            "--role",
            "baseline",
            "--port",
            "9",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode != 0
    assert "LiveProviderRefused" in completed.stderr
    # Refused before it owned, opened or served anything.
    assert not paths.data_dir("baseline").exists()


@pytest.mark.parametrize(
    ("mode", "keyring_env"),
    [
        (Mode.DRY, {}),
        (Mode.REAL, {}),
        (Mode.REAL, {"PYTHON_KEYRING_BACKEND": SCOPED_BACKEND, SCOPE_ENV: "m2-another-campaign"}),
    ],
)
def test_an_application_process_runs_only_with_the_campaigns_secret_store(
    tmp_path: Path, mode: Mode, keyring_env: dict[str, str]
) -> None:
    """Started by hand, without the campaign's keyring backend, an application process of the
    campaign refuses before it reads a secret, owns a data directory or builds a transport. CI
    stays set, so a broken check would still meet the live-transport refusal (exit 1, not 2)."""
    paths = _campaign(tmp_path, mode=mode)
    blocked = ("PYTEST_CURRENT_TEST", "GITHUB_ACTIONS", "PYTHON_KEYRING_BACKEND", SCOPE_ENV)
    env = {name: value for name, value in os.environ.items() if name not in blocked}
    env |= keyring_env | ENV | {"CI": "true", "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "m2_acceptance.py"),
            "_serve",
            "--campaign-dir",
            str(paths.root),
            "--role",
            "baseline",
            "--port",
            "9",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 2
    assert "REFUSED" in completed.stdout
    assert not paths.data_dir("baseline").exists()
