"""The Agent Host control loop, end to end against a fixture repository (protocol V3 §9).

ADR-0022.

`automation/agent-host/tests/fx-harness.ps1` builds a local bare origin and a scratch clone, mocks
`gh`, `codex` and `claude`, and runs the real Host scripts. It makes no GitHub write, no provider
call and no AI call. Each scenario pins how the run must end; this module runs the scenarios the
protocol requires and fails on any other ending.

The Host scripts are Windows PowerShell 5.1, so the module runs where that exists and is skipped
elsewhere.
"""

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS = REPO_ROOT / "automation" / "agent-host" / "tests" / "fx-harness.ps1"
POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else None
TIMEOUT_S = 900

# The regression scenarios ADR-0022 requires, by what each one proves.
SCENARIOS = {
    # 1-3: a packet needs no owner amendment, no classification record and no scope record
    "packet-reproducible": "cited evidence only; markers and legacy records are provenance",
    "packet-authority": "only an unreadable stream or a bad manifest stops a packet, technically",
    "packet-no-record": "no classification record anywhere: the PR is audited and merged",
    "packet-unclassified": "an unclassified [OWNER-AMENDMENT] in the stream never holds",
    "packet-edit-to-marker": "an uncited comment edited into a marker changes nothing",
    # 4: bookkeeping never reaches the user
    "gpt-insufficient": "an unusable audit is a technical hold, retried, never human",
    "auto-next-hold": "a per-step authorization sentence is not a user decision",
    # 5-7: the loop
    "gpt-loop": "BLOCKER, repair, new HEAD, new packet, re-audit, DUAL PASS, merge",
    "claude-loop": "a Claude BLOCKER restarts both audits on the new HEAD",
    "max-cycles": "a repeated BLOCKER is re-analysed, never handed over",
    "automerge-success": "DUAL PASS, FULL CI, MERGE_GUARD, expected_head_sha, POST_MERGE_VERIFY",
    # 8-9: after the merge
    "auto-next": "after the verified merge the next canonical slice is built and merged",
    # 10-11: the user's decisions
    "auto-next-human": "a step that needs a new product feature waits for the user",
    "auto-next-live": "a LIVE step waits for the user",
    "gpt-human": "an auditor's HUMAN_DECISION_REQUIRED waits and is never repaired",
    "gpt-human-uncategorised": "a human verdict with no closed-list category is technical",
    "fixer-human": "a fixer's product-direction stop waits for the user",
    "owner-hold": "the owner's own hold file stops the PR before any audit",
    # 12-13: identity
    "guard-head-moved": "a HEAD that moved after the audit is audited again",
    "packet-clsedit": "a cited source edited just before the merge: no merge, a re-audit",
    "packet-source-missing": "a citation no stream holds: nothing is audited or merged",
    "packet-citation-kind": "a review with a cited comment's id never stands in for it",
    "packet-canon-missing": "a cited canonical document not at the audited base stops the packet",
    "packet-cite-added": "a changed declaration just before the merge: no merge, a re-audit",
    # the mechanical checks V3 keeps or adds
    "behind-base": "a PR HEAD behind main is brought up to date before it is audited",
    "guard-main-moved": "main moved after the audit: base sync, re-audit, then merge",
    "post-merge-tree-mismatch": "a merged tree that is not the audited tree is never accepted",
    "scope-expansion": "a fix that needs a file outside the PR is reported, not held",
    "authority-unit": "marker grammar, write guard, hold taxonomy, citation grammar",
}


def _windows_powershell_env() -> dict[str, str]:
    """The environment for Windows PowerShell 5.1, without an inherited PowerShell 7 module path.

    A runner whose shell is PowerShell 7 hands its PSModulePath down; Windows PowerShell then loads
    the PowerShell 7 builds of its own utility modules and loses cmdlets such as Get-FileHash.
    Without the variable, Windows PowerShell builds its own default module path, as it does when
    the Host runs it.
    """
    return {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}


def _run(scenario: str, root: Path) -> dict[str, object]:
    assert POWERSHELL is not None
    completed = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(HARNESS),
            "-Scenario",
            scenario,
            "-Root",
            str(root),
        ],
        capture_output=True,
        timeout=TIMEOUT_S,
        check=False,
        env=_windows_powershell_env(),
    )
    result = root / scenario / "result.json"
    if not result.is_file():
        tail = completed.stdout.decode("utf-8", "replace")[-2000:]
        return {"expect": f"NO_RESULT (exit {completed.returncode}): {tail}"}
    loaded = json.loads(result.read_text("utf-8-sig"))
    checks = loaded.get("checks") or {}
    # What the harness itself reported, so that a failure elsewhere (another runner, another git)
    # says why instead of only which pin it missed.
    output = completed.stdout.decode("utf-8", "replace")
    reported = [
        line.strip()
        for line in output.splitlines()
        if line.lstrip().startswith(("HARNESS_", "FX_EXPECT=", "HOLD_CLASS=", "SUPERVISOR="))
    ]
    return {
        "expect": checks.get("expect", "NOT_PINNED"),
        "status": loaded.get("runtime_status"),
        "reported": reported[-12:],
        "stderr": completed.stderr.decode("utf-8", "replace")[-1500:],
    }


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, object]]:
    if POWERSHELL is None:
        pytest.skip("the Agent Host scripts need Windows PowerShell 5.1")
    if shutil.which("git") is None:
        pytest.skip("the fixture harness needs git")
    # A short root: the harness nests a bare origin, clones and host worktrees under it.
    root = tmp_path_factory.mktemp("fx")
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {name: pool.submit(_run, name, root) for name in SCENARIOS}
        return {name: future.result() for name, future in futures.items()}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_scenario_ends_as_pinned(results: dict[str, dict[str, object]], scenario: str) -> None:
    assert results[scenario]["expect"] == "PASS", (SCENARIOS[scenario], results[scenario])


def test_no_scenario_ends_waiting_for_the_user_unless_it_is_a_user_decision(
    results: dict[str, dict[str, object]],
) -> None:
    waiting = {
        name
        for name, result in results.items()
        if result.get("status") == "HUMAN_DECISION_REQUIRED"
    }
    assert waiting == {
        "auto-next-human",
        "auto-next-live",
        "gpt-human",
        "fixer-human",
        "owner-hold",
    }
