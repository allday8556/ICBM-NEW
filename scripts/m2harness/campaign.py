"""The M2 campaign runner: the frozen main path over the real application (Issue #46 §3, §4;
docs/acceptance/M2.md §5, §6.1, §9).

    BASELINE_CONNECT  T1, A1   a token, then the unbound seller-account observation, shown to
                               the operator, who alone confirms it
    BASELINE_BIND     A1b      explicit first binding of the confirmed account on a fresh read
    BASELINE_RESTART  A2       a new process: persisted READY is not trusted; fresh proof
    recheck                    regression floor, artifact scan, ledger/caller reconciliation
    CRASH_T4A         T4a      fresh crash data dir: a token, then death at the commit boundary
    CRASH_T4B         T4b      only if T4a missed the boundary, and only with operator consent
    CRASH_RECOVERY    T5, A3   restart: bounded recovery issuance and the A3 read, left unbound
    closeout                   final floor, scan, reconciliation, slot rows

Each phase runs the unmodified application (``create_app``) in a child process on loopback, with
the budget gate as the SmartStore caller's transport, and drives it through the PR-E operator
API. A phase is opened in the ledger before its requests and closed after them. A failed phase
stops the run; nothing is retried automatically. A later invocation with fresh approval resumes at
the first group that has not passed, and what earlier runs spent stays spent.
"""

import base64
import contextlib
import json
import os
import re
import secrets as random
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, NoReturn, Protocol

import httpx
import keyring.errors

from app import __version__
from app.connect.sessions import MARKETPLACE_SESSIONS_DIR_NAME, SupplierSessionStore
from app.connect.smartstore.credentials import ApplicationCredentialStore
from app.connect.smartstore.service import CommittedSession
from app.core.ownership import DataDirOwnershipError, acquire_data_dir
from app.core.secrets import KeyringSecretStore
from app.db.migrate import upgrade_to_head
from app.system.secret_scan import scan
from scripts.m2harness import crash
from scripts.m2harness.evidence import (
    SCHEMA_VERSION,
    EvidenceRejected,
    EvidenceWriter,
    Fingerprinter,
    write_bytes,
)
from scripts.m2harness.fake_provider import Scenario
from scripts.m2harness.gates import (
    PREFLIGHT_KIND,
    RENEWAL_MARGIN_ENV,
    RENEWAL_MARGIN_S,
    Checkout,
    Gate,
    GitCheckout,
    dedicated_problems,
    digest,
)
from scripts.m2harness.keyrings import (
    DRY_BACKEND,
    DRY_FILE_ENV,
    SCOPE_ENV,
    SCOPED_BACKEND,
    CampaignScopedKeyring,
    DryFileKeyring,
)
from scripts.m2harness.ledger import (
    CAMPAIGN_ID,
    CAPS,
    CRASH_ATTEMPT_CAP,
    CRASH_ATTEMPTS,
    SELLER,
    TERMINAL,
    TOKEN,
    Campaign,
    CrashVerdict,
    Ledger,
    Mode,
    Phase,
    State,
    now,
)
from scripts.m2harness.paths import CampaignPaths

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "m2_acceptance.py"
CLIENT = {"X-ICBM-Client": "m2-acceptance"}
KEY = "smartstore"
API = f"/api/v1/connect/marketplaces/{KEY}"
BASELINE, CRASH = "baseline", "crash"
ROLES = (BASELINE, CRASH)
FINGERPRINT_KEY = "m2-acceptance:fingerprint_key"
REGISTRATION = "m2-acceptance:registration"
HTTP_TIMEOUT_S = 120.0
READY_TIMEOUT_S = 60.0
FLOOR_TIMEOUT_S = 3600
# The five required CI jobs of .github/workflows/ci.yml.
REQUIRED_CHECKS = (
    "Lint, format, type check",
    "Tests (ubuntu-latest)",
    "Tests (windows-latest)",
    "Migration check",
    "M0 acceptance (Windows, clean checkout)",
)
_CHILD_ENV = frozenset({"PYTHON_KEYRING_BACKEND", SCOPE_ENV, DRY_FILE_ENV})
# The caller's own sanitized evidence line (integrations/marketplaces/smartstore/caller.py).
_CALL_FIELDS = (
    "endpoint_id",
    "result_class",
    "http_status",
    "latency_ms",
    "started_at",
    "provider_trace_id",
    "credential_generation",
    "session_generation",
    "transmission_phase",
    "remote_outcome",
    "predicate_revision",
)
# Caller outcomes that never reached a transport: nothing was sent, nothing was reserved.
_UNSENT = frozenset({"LOCAL_PREFLIGHT", "EGRESS_BLOCKED"})
_CODE = re.compile(r"^[A-Z][A-Z0-9_.]{0,79}$")

if sys.platform == "win32":
    _NEW_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    _NEW_GROUP = 0


class HarnessError(RuntimeError):
    pass


class ChildExited(HarnessError):
    def __init__(self, code: int) -> None:
        super().__init__(f"the application process exited with {code}")
        self.code = code


class Stop(Exception):
    """The run ends in ``state``: a failed step, a refusal, a measured contradiction."""

    def __init__(self, state: State, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


def iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(value).isoformat(timespec="milliseconds")
    return None


def _detail(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"[^A-Za-z0-9 _.,:;=/()<>#%+-]", "_", text)[:300]


def _code(text: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", text.upper())[:60] or "STOP"


def _error_code(response: httpx.Response) -> str:
    with contextlib.suppress(ValueError):
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and _CODE.fullmatch(code):
            return code
    return f"HTTP_{response.status_code}"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---------------------------------------------------------------- campaign secrets and identity


def campaign_store(paths: CampaignPaths, campaign_id: str, mode: Mode) -> KeyringSecretStore:
    backend: Any = (
        DryFileKeyring(paths.dry_keyring)
        if mode is Mode.DRY
        else CampaignScopedKeyring(campaign_id)
    )
    return KeyringSecretStore(backend=backend)


def registered(paths: CampaignPaths, campaign: Campaign) -> bool:
    try:
        raw = campaign_store(paths, campaign.campaign_id, campaign.mode).get(REGISTRATION)
    except (keyring.errors.KeyringError, RuntimeError):
        return False
    try:
        record = json.loads(raw) if raw else None
    except ValueError:
        return False
    return isinstance(record, dict) and (record.get("campaign_id"), record.get("nonce")) == (
        campaign.campaign_id,
        campaign.nonce,
    )


def fingerprinter(store: KeyringSecretStore) -> Fingerprinter:
    raw = store.get(FINGERPRINT_KEY)
    if not raw:
        raise HarnessError("the campaign fingerprint key is missing")
    return Fingerprinter(base64.b64decode(raw))


def campaign_secrets(paths: CampaignPaths, campaign: Campaign) -> dict[str, str]:
    """The application credentials, in memory only: values to scan for, never to write."""
    credentials = ApplicationCredentialStore(
        campaign_store(paths, campaign.campaign_id, campaign.mode)
    ).load(KEY)
    if credentials is None:
        return {}
    return {"client_secret": credentials.client_secret, "client_id": credentials.client_id}


def init_campaign(
    paths: CampaignPaths, *, campaign_id: str, mode: Mode, scenario: Scenario | None = None
) -> Ledger:
    """Create a campaign: its registration and fingerprint key, its ledger, and two fresh,
    migrated data directories. A campaign ID registered before is refused, so starting over
    can never reset its budget. The caller holds the campaign directory's lease."""
    if not CAMPAIGN_ID.fullmatch(campaign_id):
        raise HarnessError("a campaign id is m2- followed by lowercase letters, digits, hyphens")
    if mode is Mode.DRY:
        if scenario is None:
            raise HarnessError("a DRY campaign needs its fixture scenario")
        scenario.save(paths.scenario)
    store = campaign_store(paths, campaign_id, mode)
    if store.get(REGISTRATION) is not None:
        raise HarnessError("this campaign ID is already registered here; its budget is never reset")
    nonce = random.token_hex(16)
    store.set(FINGERPRINT_KEY, base64.b64encode(random.token_bytes(32)).decode("ascii"))
    store.set(REGISTRATION, json.dumps({"campaign_id": campaign_id, "nonce": nonce}))
    ledger = Ledger.create(paths.ledger, campaign_id=campaign_id, mode=mode, nonce=nonce)
    for role in ROLES:
        data_dir = paths.data_dir(role)
        with acquire_data_dir(data_dir, app_version=__version__) as lease:
            upgrade_to_head(f"sqlite:///{(data_dir / 'icbm.db').as_posix()}", ownership=lease)
    marker = {"campaign_id": campaign_id, "mode": mode.value, "nonce": nonce, "created_at": now()}
    paths.marker.write_text(json.dumps(marker, indent=2) + "\n", "utf-8")
    return ledger


def child_env(paths: CampaignPaths, campaign: Campaign) -> dict[str, str]:
    """The application process environment: no ambient ICBM setting, the campaign's secret
    store, and the campaign's renewal margin."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ICBM_") and name not in _CHILD_ENV
    }
    env["PYTHONIOENCODING"] = "utf-8"
    env[RENEWAL_MARGIN_ENV] = str(RENEWAL_MARGIN_S)
    if campaign.mode is Mode.DRY:
        env["PYTHON_KEYRING_BACKEND"] = DRY_BACKEND
        env[DRY_FILE_ENV] = str(paths.dry_keyring)
    else:
        env["PYTHON_KEYRING_BACKEND"] = SCOPED_BACKEND
        env[SCOPE_ENV] = campaign.campaign_id
    return env


# ---------------------------------------------------------------- application processes


@dataclass(frozen=True)
class CrashArm:
    crash_attempt: int
    nonce: bytes


class AppProcess:
    """The unmodified application on one data directory, in its own process on loopback."""

    def __init__(
        self,
        paths: CampaignPaths,
        campaign: Campaign,
        role: str,
        *,
        name: str,
        phases: Sequence[Phase] = (),
        crash_arm: CrashArm | None = None,
        port: int | None = None,
    ) -> None:
        self.port = port or _free_port()
        argv = [
            sys.executable,
            str(SCRIPT),
            "_serve",
            "--campaign-dir",
            str(paths.root),
            "--role",
            role,
            "--port",
            str(self.port),
            "--phases",
            ",".join(phase.value for phase in phases),
        ]
        if crash_arm is not None:
            argv += ["--crash-attempt", str(crash_arm.crash_attempt)]
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        self._output = (paths.run_dir / f"{name}.log").open("ab")
        self.proc = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=child_env(paths, campaign),
            stdin=subprocess.PIPE if crash_arm is not None else subprocess.DEVNULL,
            stdout=self._output,
            stderr=subprocess.STDOUT,
            creationflags=_NEW_GROUP,
        )
        if crash_arm is not None:
            assert self.proc.stdin is not None
            # The nonce authenticates this attempt's marker; it is never stored anywhere.
            self.proc.stdin.write(crash_arm.nonce.hex().encode("ascii") + b"\n")
            self.proc.stdin.close()
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}", timeout=HTTP_TIMEOUT_S, trust_env=False
        )

    def __enter__(self) -> "AppProcess":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            code = self.proc.poll()
            if code is not None:
                raise ChildExited(code)
            with contextlib.suppress(httpx.TransportError):
                if self.http.get("/api/ready").status_code in (200, 503):
                    return
            time.sleep(0.1)
        raise HarnessError("the application never answered readiness")

    def get(self, path: str) -> httpx.Response:
        return self.http.get(path)

    def post(self, path: str, body: object = None) -> httpx.Response:
        return self.http.post(path, headers=CLIENT, json=body)

    def put(self, path: str, body: object) -> httpx.Response:
        return self.http.put(path, headers=CLIENT, json=body)

    def capability(self) -> dict[str, Any]:
        response = self.get(f"{API}/capability")
        response.raise_for_status()
        return dict(response.json())

    def wait_exit(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        if self.proc.poll() is None:
            if sys.platform == "win32":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.send_signal(signal.SIGTERM)
            if self.wait_exit(60) is None:
                self.proc.kill()
                self.proc.wait(30)
        self.http.close()
        self._output.close()


# ---------------------------------------------------------------- the operator


class Operator(Protocol):
    def confirm_binding(self, account_uid: str, account_id: str | None) -> bool: ...

    def confirm_crash_retry(self, reasons: Sequence[str]) -> bool: ...


class TerminalOperator:
    """The local operator at an interactive terminal (REAL runs). The observed account is shown
    on this screen only and is never written to a file by the harness."""

    @staticmethod
    def interactive() -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    def confirm_binding(self, account_uid: str, account_id: str | None) -> bool:
        print("\nA1 observed this SmartStore seller account (shown here only, never recorded):")
        print(f"  accountUid: {account_uid}")
        print(f"  accountId:  {account_id or '(none)'}")
        expected = f"BIND {account_uid[-4:]}"
        typed = input(f"Type '{expected}' to bind exactly this account; anything else stops: ")
        return typed.strip() == expected

    def confirm_crash_retry(self, reasons: Sequence[str]) -> bool:
        print(f"\nT4a did not exercise the crash boundary ({', '.join(reasons)}).")
        print("T4b is the last crash-boundary attempt; there is no T4c.")
        return input("Type 'SPEND T4B' to spend it; anything else stops: ").strip() == "SPEND T4B"


@dataclass
class ScriptedOperator:
    """DRY rehearsals and tests only."""

    bind: bool = True
    crash_retry: bool = True
    bindings_shown: int = 0

    def confirm_binding(self, account_uid: str, account_id: str | None) -> bool:
        self.bindings_shown += 1
        return self.bind

    def confirm_crash_retry(self, reasons: Sequence[str]) -> bool:
        return self.crash_retry


# ---------------------------------------------------------------- the regression floor


class Floor(Protocol):
    def run(self, stage: str, out: Path) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class SkippedFloor:
    reason: str = "DRY rehearsal"

    def run(self, stage: str, out: Path) -> list[dict[str, Any]]:
        row = {"stage": stage, "name": "regression_floor", "result": "SKIPPED"}
        return [row | {"exit_code": None, "detail": _detail(self.reason)}]


def _floor_env() -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ICBM_") and name not in _CHILD_ENV
    }
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _m0_detail(evidence: Path) -> str | None:
    with contextlib.suppress(OSError, ValueError):
        steps = json.loads(evidence.read_text("utf-8")).get("steps", [])
        passed = sum(1 for step in steps if step.get("result") == "PASS")
        return f"{passed}/{len(steps)} checks"
    return None


@dataclass(frozen=True)
class CommandFloor:
    """M2.md §8: the CI gates run locally, and M0 acceptance from its own fresh data."""

    visual: bool = True

    def run(self, stage: str, out: Path) -> list[dict[str, Any]]:
        out.mkdir(parents=True, exist_ok=True)
        m0 = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "m0_acceptance.py"),
            "--out",
            str(out / "m0"),
        ]
        commands = [
            ("pytest", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]),
            ("ruff_check", [sys.executable, "-m", "ruff", "check", "."]),
            ("ruff_format", [sys.executable, "-m", "ruff", "format", "--check", "."]),
            ("mypy", [sys.executable, "-m", "mypy"]),
            ("m0_acceptance", m0 + (["--visual"] if self.visual else [])),
        ]
        rows = []
        for name, argv in commands:
            print(f"[....] {stage}.{name}", flush=True)
            with (out / f"{name}.log").open("wb") as log:
                completed = subprocess.run(
                    argv,
                    cwd=REPO_ROOT,
                    env=_floor_env(),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=FLOOR_TIMEOUT_S,
                    check=False,
                )
            detail = _m0_detail(out / "m0" / "evidence.json") if name == "m0_acceptance" else None
            result = "PASS" if completed.returncode == 0 else "FAIL"
            rows.append(
                {
                    "stage": stage,
                    "name": name,
                    "result": result,
                    "exit_code": completed.returncode,
                    "detail": detail,
                }
            )
            print(f"[{result}] {stage}.{name}" + (f" ({detail})" if detail else ""), flush=True)
        return rows


def _stage_dir(paths: CampaignPaths, stage: str) -> Path:
    return paths.regression_dir / f"{stage}-{time.strftime('%Y%m%dT%H%M%S')}"


# ---------------------------------------------------------------- caller evidence


def caller_calls(data_dir: Path, role: str) -> list[dict[str, Any]]:
    """The caller's ``smartstore.request`` lines of one data directory, allowlisted fields only."""
    rows: list[dict[str, Any]] = []
    log_dir = data_dir / "logs"
    for path in sorted(log_dir.glob("icbm.jsonl*")) if log_dir.is_dir() else []:
        for line in path.read_text("utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("msg") != "smartstore.request":
                continue
            row = {"data_dir": role} | {name: record.get(name) for name in _CALL_FIELDS}
            row["started_at"] = iso(row["started_at"])
            rows.append(row)
    return rows


def reconcile(ledger: Ledger, paths: CampaignPaths) -> dict[str, Any]:
    """Every request the ledger reserved has exactly one caller line that reached a transport, and
    every refusal a transport raised has exactly one caller line classified EGRESS_BLOCKED."""
    calls = [call for role in ROLES for call in caller_calls(paths.data_dir(role), role)]
    sent = {TOKEN: 0, SELLER: 0}
    refused = 0
    unexpected = 0
    for call in calls:
        if call["transmission_phase"] == "EGRESS_BLOCKED":
            refused += 1
        elif call["transmission_phase"] == "LOCAL_PREFLIGHT":
            continue
        elif call["endpoint_id"] in sent:
            sent[call["endpoint_id"]] += 1
        else:
            unexpected += 1
    counts = ledger.counts()
    # pid 0 marks the crash sub-budget refusal: no process attempted that request.
    refused_ledger = sum(1 for row in ledger.rows("refusals") if row["pid"] != 0)
    return {
        "match": counts == sent and refused_ledger == refused and unexpected == 0,
        "ledger": counts,
        "caller_log": sent,
        "refused_ledger": refused_ledger,
        "refused_caller_log": refused,
    }


# ---------------------------------------------------------------- preflight (zero provider calls)


@dataclass(frozen=True)
class PreflightPlan:
    floor: Floor
    ci_checks: Callable[[str], Gate] | None = None
    rehearsal: Callable[[], Gate] | None = None
    require_clean_tree: bool = True


def _budget(ledger: Ledger) -> str:
    counts = ledger.counts()
    return f"token {counts[TOKEN]}/{CAPS[TOKEN]}, seller {counts[SELLER]}/{CAPS[SELLER]}, other 0/0"


def _baseline_readiness(paths: CampaignPaths, campaign: Campaign) -> list[tuple[str, bool, str]]:
    """Issue #46 §3.B/§3.C on the baseline dir, twice across a restart, with no phase open: any
    SmartStore request would be refused before send, and egress shows none was even tried."""
    views: list[dict[str, Any]] = []
    attempts = grants = 0
    for start in (1, 2):
        with AppProcess(paths, campaign, BASELINE, name=f"preflight-baseline-{start}") as app:
            app.wait_ready()
            credentials = app.get(f"{API}/credentials").json()
            capability = app.capability()
            egress = app.get("/api/v1/system/egress").json()
        views.append(
            {
                "configured": credentials.get("configured"),
                "credential_generation": credentials.get("credential_generation"),
                "auth": capability["auth"],
                "contract_freshness": capability["contract_freshness"],
                "write_scope": capability["write_scope"],
                "write": capability["write"]["status"],
            }
        )
        attempts += int(egress.get("external_attempts") or 0)
        grants += int((egress.get("granted_events") or {}).get(f"marketplace:{KEY}", 0))
    first, scope = views[0], views[0]["write_scope"]
    return [
        ("credentials_committed", bool(first["configured"]), f"configured={first['configured']}"),
        (
            "contract_freshness_current",
            first["contract_freshness"] == "CURRENT",
            f"contract_freshness={first['contract_freshness']}",
        ),
        ("auth_not_ready_without_proof", first["auth"] != "READY", f"auth={first['auth']}"),
        (
            "a0_operator_attested_limited",
            scope.get("evidence_strength") == "OPERATOR_ATTESTED"
            and scope.get("evidence_grade") == "LIMITED",
            f"strength={scope.get('evidence_strength')} grade={scope.get('evidence_grade')}",
        ),
        ("write_not_ready", first["write"] != "READY", f"write={first['write']}"),
        ("a0_survives_restart", views[0] == views[1], f"identical={views[0] == views[1]}"),
        (
            "zero_provider_egress",
            attempts == 0 and grants == 0,
            f"external_attempts={attempts} smartstore_grants={grants}",
        ),
    ]


def github_checks_gate(head: str) -> Gate:
    """The five required CI jobs are green at ``head`` (read through gh, never SmartStore)."""
    completed = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/commits/{head}/check-runs",
            "--paginate",
            "--jq",
            '.check_runs[] | [.name, .status, (.conclusion // "")] | @tsv',
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        return Gate("ci_required_checks", False, "gh could not read the check runs of HEAD")
    runs: dict[str, set[tuple[str, str]]] = {}
    for line in completed.stdout.splitlines():
        name, status, conclusion = ([*line.split("\t"), "", ""])[:3]
        runs.setdefault(name, set()).add((status, conclusion))
    green = [name for name in REQUIRED_CHECKS if ("completed", "success") in runs.get(name, set())]
    return Gate(
        "ci_required_checks",
        len(green) == len(REQUIRED_CHECKS),
        f"{len(green)}/{len(REQUIRED_CHECKS)} required checks green at HEAD",
    )


def dry_rehearsal_gate() -> Gate:
    """The whole harness at this SHA, end to end against the fake provider."""
    with tempfile.TemporaryDirectory(prefix="icbm-m2-dry-", ignore_cleanup_errors=True) as tmp:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "dry", "--out", str(Path(tmp) / "rehearsal")],
            cwd=REPO_ROOT,
            env=_floor_env(),
            capture_output=True,
            timeout=FLOOR_TIMEOUT_S,
            check=False,
        )
    return Gate("dry_rehearsal", completed.returncode == 0, f"exit {completed.returncode}")


def run_preflight(
    paths: CampaignPaths,
    ledger: Ledger,
    *,
    checkout: Checkout,
    plan: PreflightPlan,
    environ: Mapping[str, str],
) -> bool:
    """The zero-provider preflight (Issue #46 §3). On PASS it records the status file's digest
    and leaves the campaign at AWAITING_REAL_PROVIDER_APPROVAL. It never sends, never continues."""
    campaign = ledger.campaign()
    gates: list[dict[str, Any]] = []

    def gate(name: str, passed: bool, detail: str = "", *, skipped: bool = False) -> None:
        result = "SKIPPED" if skipped else "PASS" if passed else "FAIL"
        gates.append({"name": name, "result": result, "detail": _detail(detail)})
        print(f"[{result}] preflight.{name}" + (f" ({detail})" if detail else ""), flush=True)

    head, dirty = checkout.head(), checkout.dirty()
    gate(
        "ledger_state",
        campaign.state in (State.INITIALIZED, State.AWAITING_REAL_PROVIDER_APPROVAL),
        campaign.state.value,
    )
    gate("provider_requests_zero_before", sum(ledger.counts().values()) == 0, _budget(ledger))
    gate("head_recorded", bool(re.fullmatch(r"[0-9a-f]{40}", head)), head[:12])
    gate(
        "tree_clean",
        dirty == 0,
        f"{dirty} changed or untracked path(s)",
        skipped=dirty > 0 and not plan.require_clean_tree,
    )
    gate(
        "renewal_margin_600",
        environ.get(RENEWAL_MARGIN_ENV) == str(RENEWAL_MARGIN_S),
        f"{RENEWAL_MARGIN_ENV}={environ.get(RENEWAL_MARGIN_ENV)}",
    )
    if campaign.mode is Mode.REAL:
        problems = dedicated_problems(paths.root, environ)
        gate("dedicated_campaign_dir", not problems, "; ".join(problems))
        gate("campaign_registered", registered(paths, campaign), "no matching registration")
    if plan.ci_checks is not None:
        ci = plan.ci_checks(head)
        gate(ci.name, ci.passed, ci.detail)
    else:
        gate("ci_required_checks", True, "not checked in a DRY rehearsal", skipped=True)
    rows = plan.floor.run("preflight", _stage_dir(paths, "preflight"))
    summary = ", ".join(f"{row['name']}={row['result']}" for row in rows)
    gate("regression_floor", all(row["result"] != "FAIL" for row in rows), summary)
    if plan.rehearsal is not None:
        rehearsal = plan.rehearsal()
        gate(rehearsal.name, rehearsal.passed, rehearsal.detail)
    else:
        gate("dry_rehearsal", True, "this run is the DRY rehearsal", skipped=True)
    for name, passed, detail in _baseline_readiness(paths, campaign):
        gate(name, passed, detail)
    gate("provider_requests_zero_after", sum(ledger.counts().values()) == 0, _budget(ledger))
    passed = all(entry["result"] != "FAIL" for entry in gates)
    status = {
        "kind": PREFLIGHT_KIND,
        "campaign_id": campaign.campaign_id,
        "mode": campaign.mode.value,
        "head": head,
        "tree_clean": dirty == 0,
        "generated_at": now(),
        "result": "PASS" if passed else "FAIL",
        "gates": gates,
        "regression": rows,
        "budget": {**ledger.counts(), "OTHER": 0},
        "refusals": len(ledger.rows("refusals")),
    }
    data = (json.dumps(status, indent=2) + "\n").encode("utf-8")
    EvidenceWriter(campaign_secrets(paths, campaign)).guard(data)
    write_bytes(paths.preflight, data)
    if passed:
        ledger.mark_preflight_passed(digest(paths.preflight), head)
        print(
            "\nSTOP: AWAITING_REAL_PROVIDER_APPROVAL. SmartStore requests sent: 0 "
            f"({_budget(ledger)}). Nothing continues from here: a real run needs the user's "
            "explicit go-ahead, `approve`, then a separate `real` invocation.",
            flush=True,
        )
    return passed


def prepare_dry_baseline(paths: CampaignPaths, campaign: Campaign, scenario: Scenario) -> None:
    """What the operator does through ``serve`` before a REAL preflight, with fixture values."""
    with AppProcess(paths, campaign, BASELINE, name="dry-prepare") as app:
        app.wait_ready()
        responses = [
            app.put(
                f"{API}/credentials",
                {"client_id": scenario.client_id, "client_secret": scenario.client_secret},
            ),
            app.post(f"{API}/contract-freshness", {"contract_freshness": "CURRENT"}),
            app.post(
                f"{API}/permission-attestation", {"observed_groups": ["SELLER_INFO", "PRODUCT"]}
            ),
        ]
    failed = [response.status_code for response in responses if response.status_code != 200]
    if failed:
        raise HarnessError(f"the DRY baseline could not be prepared (HTTP {failed})")


# ---------------------------------------------------------------- the run


def _slot(
    evidence_id: str,
    kind: str,
    status: str,
    required: bool,
    *,
    observed_at: dict[str, str | None] | None = None,
    application_fingerprint: str | None = None,
    credential_generation: int | None = None,
    session_generation: int | None = None,
    requests: dict[str, int] | None = None,
    evidence_ref: str,
    summary: str,
    contract_impact: str = "NONE",
) -> dict[str, Any]:
    counts = requests or {}
    return {
        "evidence_id": evidence_id,
        "evidence_kind": kind,
        "status": status,
        "required_for_m2_acceptance": required,
        "observed_at": observed_at,
        "application_fingerprint": application_fingerprint,
        "credential_generation": credential_generation,
        "session_generation": session_generation,
        "endpoint_ids": [endpoint for endpoint in (TOKEN, SELLER) if counts.get(endpoint)],
        "request_count": {endpoint: n for endpoint, n in counts.items() if n},
        "evidence_ref": evidence_ref,
        "result_summary": summary,
        "contract_impact": contract_impact,
    }


class Run:
    """One invocation of the frozen main path, on an approved REAL or a DRY campaign."""

    def __init__(
        self,
        paths: CampaignPaths,
        ledger: Ledger,
        *,
        operator: Operator,
        floor: Floor,
        head: str,
        tree_clean: bool,
        approved_sha: str | None = None,
    ) -> None:
        self.paths = paths
        self.ledger = ledger
        self.operator = operator
        self.floor = floor
        self.head = head
        self.tree_clean = tree_clean
        self.approved_sha = approved_sha
        self.campaign = ledger.campaign()
        self.store = campaign_store(paths, self.campaign.campaign_id, self.campaign.mode)
        self.fingerprints = fingerprinter(self.store)
        credentials = ApplicationCredentialStore(self.store).load(KEY)
        self.application_fingerprint = (
            self.fingerprints.application(credentials.client_id) if credentials else None
        )
        self.started_at = now()
        self.steps: list[dict[str, Any]] = []
        self.regression: list[dict[str, Any]] = []
        self.scan_report: dict[str, Any] | None = None
        self.reconcile_report: dict[str, Any] | None = None
        self._sessions: dict[str, dict[str, Any]] = {}
        # Observed account identities, in memory only: shown to the operator and scanned for.
        self._observed: set[str] = set()
        self._current: tuple[Phase, int] | None = None

    # ------------------------------------------------------------------ bookkeeping

    def check(self, name: str, ok: bool, detail: str | None = None) -> bool:
        self.steps.append(
            {
                "name": name,
                "result": "PASS" if ok else "FAIL",
                "at": now(),
                "detail": _detail(detail),
            }
        )
        print(
            f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail else ""), flush=True
        )
        return ok

    def require(
        self, name: str, ok: bool, detail: str | None = None, *, stop: State = State.FAILED
    ) -> None:
        if not self.check(name, ok, detail):
            self._stop(stop, name)

    def _stop(self, state: State, reason: str, *, result: str | None = None) -> NoReturn:
        self._close(result or f"FAIL:{_code(reason)}")
        raise Stop(state, reason)

    def _open(self, phase: Phase) -> int:
        attempt = self.ledger.open_phase(phase)
        self._current = (phase, attempt)
        return attempt

    def _close(self, result: str) -> None:
        if self._current is not None:
            phase, attempt = self._current
            self.ledger.close_phase(phase, attempt, result)
            self._current = None

    def _app(self, role: str, name: str, phases: Sequence[Phase]) -> AppProcess:
        app = AppProcess(self.paths, self.campaign, role, name=name, phases=phases)
        try:
            app.wait_ready()
        except BaseException:
            app.close()
            raise
        return app

    def _connect(self, app: AppProcess, step: str) -> dict[str, Any]:
        response = app.post(f"{API}/connect")
        if response.status_code == 200:
            return dict(response.json())
        self._fail(step, _error_code(response))

    def _fail(self, step: str, code: str) -> NoReturn:
        self.check(step, False, code)
        exhausted = self.ledger.campaign().state is State.BUDGET_EXHAUSTED
        self._stop(
            State.BUDGET_EXHAUSTED if exhausted else State.STOPPED_STEP_FAILED, f"{step} {code}"
        )

    def _observe(self, label: str, role: str, observed: Mapping[str, Any]) -> str:
        uid = str(observed["observed_account_uid"])
        self._observed.add(uid)
        if observed.get("observed_account_id"):
            self._observed.add(str(observed["observed_account_id"]))
        fingerprint = self.fingerprints.account(uid)
        self.ledger.record_event(
            "OBSERVATION",
            {
                "label": label,
                "data_dir": role,
                "account_fingerprint": fingerprint,
                "bound": bool(observed["bound"]),
                "auth": observed["capability"]["auth"],
            },
        )
        return fingerprint

    def _baseline_identity(self) -> dict[str, Any] | None:
        event = self.ledger.last_event("BASELINE_IDENTITY")
        return dict(event["detail"]) if event else None

    # ------------------------------------------------------------------ the frozen main path

    def _baseline(self) -> None:
        if self.ledger.phase_passed(Phase.BASELINE_BIND):
            return
        phases = (Phase.BASELINE_CONNECT, Phase.BASELINE_BIND)
        with self._app(BASELINE, "baseline-connect", phases) as app:
            before = app.capability()
            self.require(
                "baseline.not_ready_before_proof", before["auth"] != "READY", before["auth"]
            )
            self._open(Phase.BASELINE_CONNECT)
            observed = self._connect(app, "t1_a1.connect")
            self.require(
                "a1.observation_never_binds",
                observed["bound"] is False and observed["capability"]["auth"] != "READY",
                f"bound={observed['bound']} auth={observed['capability']['auth']}",
            )
            fingerprint = self._observe("A1", BASELINE, observed)
            self._close("PASS")
            uid = str(observed["observed_account_uid"])
            if not self.operator.confirm_binding(uid, observed.get("observed_account_id")):
                self.check("a1.operator_confirms_account", False, "declined")
                raise Stop(State.STOPPED_OPERATOR_DECLINED, "the operator did not confirm A1")
            self.check("a1.operator_confirms_account", True)
            self._open(Phase.BASELINE_BIND)
            response = app.post(f"{API}/bind", {"confirmed_account_uid": uid})
            if response.status_code != 200:
                self._fail("a1b.bind", _error_code(response))
            bound = dict(response.json())
            bound_fingerprint = self._observe("A1b", BASELINE, bound)
            self.require(
                "a1b.binds_the_confirmed_account",
                bound["bound"] is True
                and bound["capability"]["auth"] == "READY"
                and bound_fingerprint == fingerprint,
                f"bound={bound['bound']} auth={bound['capability']['auth']}",
            )
            credentials = app.get(f"{API}/credentials").json()
            self.ledger.record_event(
                "BASELINE_IDENTITY",
                {
                    "account_fingerprint": fingerprint,
                    "application_fingerprint": self.application_fingerprint,
                    "credential_generation": credentials.get("credential_generation"),
                },
            )
            self._close("PASS")

    def _restart(self) -> None:
        if self.ledger.phase_passed(Phase.BASELINE_RESTART):
            return
        with self._app(BASELINE, "baseline-restart", (Phase.BASELINE_RESTART,)) as app:
            before = app.capability()
            self.require(
                "a2.persisted_ready_not_trusted", before["auth"] != "READY", before["auth"]
            )
            attempt = self._open(Phase.BASELINE_RESTART)
            observed = self._connect(app, "a2.connect")
            fingerprint = self._observe("A2", BASELINE, observed)
            baseline = self._baseline_identity()
            if baseline is None or fingerprint != baseline["account_fingerprint"]:
                self.check("a2.same_account_as_bound", False, "a different account fingerprint")
                self._stop(
                    State.STOPPED_FOR_CONTRACT_REVIEW,
                    "A2 read another account than the bound one",
                    result="MEASURED_CONTRACT_REVIEW_REQUIRED",
                )
            self.require(
                "a2.ready_only_after_fresh_proof",
                observed["bound"] is True and observed["capability"]["auth"] == "READY",
                f"auth={observed['capability']['auth']}",
            )
            labels = [
                row["label"] for row in self.ledger.requests_for(Phase.BASELINE_RESTART, attempt)
            ]
            self.require("a2.committed_session_reused", labels == ["A2"], ",".join(labels))
            self._close("PASS")

    def _recheck(self) -> None:
        if self.ledger.last_event("RECHECK_PASSED") is not None:
            return
        rows = self.floor.run("recheck", _stage_dir(self.paths, "recheck"))
        self.regression.extend(rows)
        self.require(
            "recheck.regression_floor",
            all(row["result"] != "FAIL" for row in rows),
            ", ".join(f"{row['name']}={row['result']}" for row in rows),
            stop=State.STOPPED_STEP_FAILED,
        )
        self._scan_and_reconcile("recheck")
        self.ledger.record_event("RECHECK_PASSED", {})

    def _crash_boundary(self) -> None:
        attempts = {row["attempt"]: row for row in self.ledger.rows("crash_attempts")}
        if any(row["verdict"] == CrashVerdict.BOUNDARY_EXERCISED for row in attempts.values()):
            return
        if not attempts:
            state = crash.data_dir_state(self.paths.data_dir(CRASH))
            self.require(
                "crash_dir.fresh_without_session",
                not state.committed and not state.bound,
                f"session_generation_hwm={state.session_generation_hwm}",
            )
        for phase, (number, label) in CRASH_ATTEMPTS.items():  # T4a, then T4b; no third
            row = attempts.get(number)
            if row is not None:
                verdict = CrashVerdict(row["verdict"]) if row["verdict"] else None
                if verdict is None:  # a run died inside this attempt; its nonce died with it
                    verdict = CrashVerdict.BOUNDARY_NOT_EXERCISED
                    self.ledger.record_crash_verdict(
                        number, verdict, [crash.Reason.ATTEMPT_INTERRUPTED.value]
                    )
                if verdict is not CrashVerdict.BOUNDARY_NOT_EXERCISED:
                    self._stop(State.FAILED, f"{label} {verdict.value}")
                continue
            if number > 1:
                reasons = [
                    reason
                    for event in self.ledger.events("CRASH_RESULT")
                    for reason in event["detail"]["reasons"]
                ]
                if not self.operator.confirm_crash_retry(reasons):
                    self.check("t4b.operator_consents", False, "declined")
                    raise Stop(State.STOPPED_OPERATOR_DECLINED, "the operator declined T4b")
            result = self._crash_attempt(phase)
            if result.verdict is CrashVerdict.BOUNDARY_EXERCISED:
                return
            if result.verdict is not CrashVerdict.BOUNDARY_NOT_EXERCISED:
                raise Stop(State.FAILED, f"{label} {result.verdict.value}")
        raise Stop(State.BUDGET_EXHAUSTED, "T4a and T4b both missed the boundary; there is no T4c")

    def _crash_attempt(self, phase: Phase) -> crash.CrashResult:
        number, label = CRASH_ATTEMPTS[phase]
        nonce = random.token_bytes(32)
        marker = self.paths.marker_file(number)
        marker.unlink(missing_ok=True)
        self._open(phase)
        connect_result: str | None = None
        exit_code: int | None = None
        app = AppProcess(
            self.paths,
            self.campaign,
            CRASH,
            name=f"crash-{label}",
            phases=(phase,),
            crash_arm=CrashArm(number, nonce),
        )
        try:
            app.wait_ready()
            try:
                response = app.post(f"{API}/connect")
            except httpx.TransportError:
                exit_code = app.wait_exit(READY_TIMEOUT_S)  # died mid-request, as intended
            else:
                connect_result = (
                    "HTTP_200" if response.status_code == 200 else _error_code(response)
                )
                exit_code = app.wait_exit(3.0)
        except HarnessError:
            exit_code = app.proc.poll()
        finally:
            app.close()
        result = crash.verify_boundary(
            exit_code=exit_code,
            marker_path=marker,
            nonce=nonce,
            ledger=self.ledger,
            campaign_id=self.campaign.campaign_id,
            crash_attempt=number,
            data_dir=self.paths.data_dir(CRASH),
        )
        self.ledger.record_crash_verdict(number, result.verdict, [r.value for r in result.reasons])
        self.ledger.record_event("CRASH_RESULT", result.evidence(connect_result))
        self._close(result.verdict.value)
        self.check(
            f"{label.lower()}.crash_boundary",
            result.verdict is CrashVerdict.BOUNDARY_EXERCISED,
            ",".join(reason.value for reason in result.reasons) or result.verdict.value,
        )
        return result

    def _recovery(self) -> None:
        if self.ledger.phase_passed(Phase.CRASH_RECOVERY):
            return
        fingerprint: str | None = None
        with self._app(CRASH, "crash-recovery", (Phase.CRASH_RECOVERY,)) as app:
            before = app.capability()
            self.require(
                "t5.nothing_trusted_after_crash", before["auth"] != "READY", before["auth"]
            )
            credentials = app.get(f"{API}/credentials").json()
            attempt = self._open(Phase.CRASH_RECOVERY)
            response = app.post(f"{API}/connect")
            connect_result = "HTTP_200" if response.status_code == 200 else _error_code(response)
            if response.status_code == 200:
                observed = dict(response.json())
                fingerprint = self._observe("A3", CRASH, observed)
                self.require(
                    "a3.crash_dir_not_promoted",
                    observed["bound"] is False and observed["capability"]["auth"] != "READY",
                    f"bound={observed['bound']} auth={observed['capability']['auth']}",
                )
        state = crash.data_dir_state(self.paths.data_dir(CRASH))
        rows = {
            row["label"]: row for row in self.ledger.requests_for(Phase.CRASH_RECOVERY, attempt)
        }
        self.ledger.record_event(
            "RECOVERY",
            {
                "connect_result": connect_result,
                "t5_http_status": rows.get("T5", {}).get("http_status"),
                "a3_http_status": rows.get("A3", {}).get("http_status"),
                "crash_dir_bound": state.bound,
                "crash_dir_session_generation": state.session_generation_hwm,
            },
        )
        self.require("a3.no_binding_in_crash_dir", not state.bound, f"bound={state.bound}")
        result, reason = self._compare(fingerprint, credentials.get("credential_generation"))
        self.ledger.record_event("A3_COMPARISON", {"result": result, "reason": reason})
        self.check("a3.identity_comparison", result == "MATCH", f"{result} {reason}")
        if result == "MATCH":
            self._close("PASS")
            return
        if result == "MISMATCH":
            self._stop(
                State.STOPPED_FOR_CONTRACT_REVIEW,
                "A3 observed another account than the baseline bound account",
                result="MEASURED_CONTRACT_REVIEW_REQUIRED",
            )
        # Fail closed: a comparison that cannot be made is never a match (M2.md §5.4).
        exhausted = self.ledger.campaign().state is State.BUDGET_EXHAUSTED
        self._stop(
            State.BUDGET_EXHAUSTED if exhausted else State.STOPPED_STEP_FAILED,
            f"A3 comparison not possible {reason}",
            result=f"NOT_COMPARABLE:{reason}",
        )

    def _compare(self, fingerprint: str | None, credential_generation: object) -> tuple[str, str]:
        baseline = self._baseline_identity()
        if baseline is None:
            return "NOT_COMPARABLE", "NO_BASELINE_FINGERPRINT"
        if fingerprint is None:
            return "NOT_COMPARABLE", "NO_SUCCESSFUL_A3_READ"
        if (baseline["application_fingerprint"], baseline["credential_generation"]) != (
            self.application_fingerprint,
            credential_generation,
        ):
            return "NOT_COMPARABLE", "DIFFERENT_APPLICATION_OR_CREDENTIAL_BASIS"
        if fingerprint == baseline["account_fingerprint"]:
            return "MATCH", "SAME_ACCOUNT"
        return "MISMATCH", "DIFFERENT_ACCOUNT"

    def _closeout(self) -> None:
        rows = self.floor.run("final", _stage_dir(self.paths, "final"))
        self.regression.extend(rows)
        self.require(
            "final.regression_floor",
            all(row["result"] != "FAIL" for row in rows),
            ", ".join(f"{row['name']}={row['result']}" for row in rows),
            stop=State.STOPPED_STEP_FAILED,
        )
        self._scan_and_reconcile("final")

    # ------------------------------------------------------------------ scans and evidence

    def _committed(self, role: str) -> CommittedSession | None:
        data_dir = self.paths.data_dir(role)
        sessions = SupplierSessionStore(
            data_dir / MARKETPLACE_SESSIONS_DIR_NAME, self.store, namespace="marketplace"
        )
        if not sessions.exists(KEY):
            return None
        try:
            with acquire_data_dir(data_dir, app_version=__version__):
                payload = sessions.load(KEY)
        except DataDirOwnershipError:
            return None
        session = CommittedSession.decode(payload) if payload else None
        if session is not None:
            self._sessions[role] = {
                "data_dir": role,
                "token_type": session.token_type,
                "expires_in": session.expires_in,
                "expires_at": iso(session.expires_at),
                "credential_generation": session.credential_generation,
                "session_generation": session.session_generation,
                "committed_at": iso(session.committed_at),
            }
        return session

    def _secrets(self, *, evidence: bool) -> dict[str, str]:
        values = campaign_secrets(self.paths, self.campaign)
        for role in ROLES:
            session = self._committed(role)
            if session is not None:
                values[f"bearer_{role}"] = session.access_token
        if evidence:
            for index, value in enumerate(sorted(self._observed)):
                values[f"account_{index}"] = value
        return values

    def _scan_and_reconcile(self, stage: str) -> None:
        paths = self.paths
        targets = [
            path
            for path in (
                paths.root / "data",
                paths.run_dir,
                paths.regression_dir,
                paths.preflight.parent,
                paths.ledger,
                paths.marker,
                paths.approval,
            )
            if path.exists()
        ]
        local = scan(targets, self._secrets(evidence=False))
        self._write_evidence()  # the evidence as it stands, so the scan covers it too
        repository = scan([paths.evidence_dir], self._secrets(evidence=True))
        self.scan_report = {"stage": stage, "local": local, "evidence": repository}
        self.require(
            f"{stage}.artifact_scan_clean",
            local["total_hits"] == 0 and repository["total_hits"] == 0,
            f"local {local['total_hits']} hits, evidence {repository['total_hits']} hits",
        )
        self.reconcile_report = reconcile(self.ledger, paths)
        report = self.reconcile_report
        self.require(
            f"{stage}.ledger_matches_caller_log",
            bool(report["match"]),
            f"ledger {report['ledger']} caller {report['caller_log']} refused "
            f"{report['refused_ledger']}/{report['refused_caller_log']}",
        )

    def _preflight_gate(self, name: str) -> bool | None:
        with contextlib.suppress(OSError, ValueError):
            status = json.loads(self.paths.preflight.read_text("utf-8"))
            for entry in status.get("gates", []):
                if entry.get("name") == name:
                    return bool(entry.get("result") == "PASS")
        return None

    def _slots(self) -> list[dict[str, Any]]:
        requests = self.ledger.rows("requests")
        baseline = self._baseline_identity()
        generation = baseline["credential_generation"] if baseline else None
        observed = {e["detail"]["label"]: e["detail"] for e in self.ledger.events("OBSERVATION")}

        def responded(label: str) -> bool:
            return any(
                row["label"] == label
                and row["outcome"] == "RESPONDED"
                and row["http_status"] == 200
                for row in requests
            )

        def used(labels: Sequence[str]) -> dict[str, int]:
            counts = {TOKEN: 0, SELLER: 0}
            for row in requests:
                if row["label"] in labels:
                    counts[row["endpoint_id"]] += 1
            return counts

        def window(labels: Sequence[str]) -> dict[str, str | None] | None:
            rows = [row for row in requests if row["label"] in labels]
            if not rows:
                return None
            return {
                "started_at": min(row["reserved_at"] for row in rows),
                "finished_at": max(row["finished_at"] or row["reserved_at"] for row in rows),
            }

        slots = []
        session = self._sessions.get(BASELINE)
        if responded("T1") and session is not None:
            slots.append(
                _slot(
                    "SMARTSTORE-R0-TOKEN",
                    "PROVIDER_MEASURED",
                    "PASS",
                    True,
                    observed_at=window(["T1"]),
                    credential_generation=generation,
                    session_generation=session["session_generation"],
                    requests=used(["T1"]),
                    evidence_ref="requests(T1),sessions(baseline)",
                    summary=(
                        f"T1 issued a {session['token_type']} token, expires_in "
                        f"{session['expires_in']}; it became current only as committed session "
                        f"generation {session['session_generation']}"
                    ),
                    application_fingerprint=self.application_fingerprint,
                )
            )
        seller = ("A1", "A1b", "A2")
        if (
            baseline is not None
            and all(responded(label) for label in seller)
            and all(
                observed.get(label, {}).get("account_fingerprint")
                == baseline["account_fingerprint"]
                for label in seller
            )
            and observed["A2"]["auth"] == "READY"
        ):
            slots.append(
                _slot(
                    "SMARTSTORE-R0-SELLER-ACCOUNT",
                    "PROVIDER_MEASURED",
                    "PASS",
                    True,
                    observed_at=window(seller),
                    credential_generation=generation,
                    requests=used(seller),
                    evidence_ref="requests(A1,A1b,A2),identity",
                    summary=(
                        "A1 observed the account unbound, A1b bound the operator-confirmed "
                        "account on a fresh read, A2 proved it after a restart; one account "
                        "fingerprint throughout"
                    ),
                    application_fingerprint=self.application_fingerprint,
                )
            )
        verdicts = [row["verdict"] for row in self.ledger.rows("crash_attempts")]
        comparison = self.ledger.last_event("A3_COMPARISON")
        crash_labels = ("T4a", "T4b", "T5", "A3")
        compared = comparison["detail"]["result"] if comparison else None
        if CrashVerdict.BOUNDARY_EXERCISED in verdicts and compared in ("MATCH", "MISMATCH"):
            match = compared == "MATCH"
            slots.append(
                _slot(
                    "SMARTSTORE-R0-FIRST-TOKEN-CRASH",
                    "PROVIDER_MEASURED",
                    "PASS" if match else "MEASURED_CONTRACT_REVIEW_REQUIRED",
                    True,
                    observed_at=window(crash_labels),
                    credential_generation=generation,
                    requests=used(crash_labels),
                    evidence_ref="crash,identity",
                    summary=(
                        "the child died at the post-response, pre-commit boundary; T5 and A3 "
                        "ran after the restart; A3 "
                        + ("matched" if match else "did not match")
                        + " the baseline account fingerprint"
                    ),
                    contract_impact=(
                        "NONE" if match else "REVIEW: ACCOUNT_IDENTITY section 7, crash-dir A3"
                    ),
                    application_fingerprint=self.application_fingerprint,
                )
            )
        elif verdicts and any(
            verdict in (CrashVerdict.COMMITTED_BEFORE_CRASH, CrashVerdict.CANDIDATE_LEAKED)
            for verdict in verdicts
        ):
            slots.append(
                _slot(
                    "SMARTSTORE-R0-FIRST-TOKEN-CRASH",
                    "PROVIDER_MEASURED",
                    "FAIL",
                    True,
                    observed_at=window(crash_labels),
                    requests=used(crash_labels),
                    evidence_ref="crash",
                    summary="a token candidate was committed or found on disk at the crash point",
                    application_fingerprint=self.application_fingerprint,
                )
            )
        elif len(verdicts) == CRASH_ATTEMPT_CAP and all(
            verdict == CrashVerdict.BOUNDARY_NOT_EXERCISED for verdict in verdicts
        ):
            slots.append(
                _slot(
                    "SMARTSTORE-R0-FIRST-TOKEN-CRASH",
                    "PROVIDER_MEASURED",
                    "FAIL",
                    True,
                    observed_at=window(crash_labels),
                    requests=used(crash_labels),
                    evidence_ref="crash",
                    summary="the boundary was not reached within the sub-budget of two attempts",
                    application_fingerprint=self.application_fingerprint,
                )
            )
        a0 = self._preflight_gate("a0_operator_attested_limited")
        if a0 is not None:
            slots.append(
                _slot(
                    "SMARTSTORE-A0-PERMISSION",
                    "OPERATOR_ATTESTED",
                    "PASS" if a0 else "FAIL",
                    True,
                    credential_generation=generation,
                    evidence_ref="preflight/status.json",
                    summary=(
                        "operator-attested API groups persisted across a restart as LIMITED, "
                        "never machine-verified; 0 provider requests"
                    ),
                    application_fingerprint=self.application_fingerprint,
                )
            )
        slots.append(
            _slot(
                "SMARTSTORE-R0-TOKEN-REISSUE-WINDOW",
                "PROVIDER_MEASURED",
                "DEFERRED_LONG_HORIZON",
                False,
                evidence_ref="M2.md#5.3",
                summary="separate long-horizon session; not part of the main campaign",
            )
        )
        slots.append(
            _slot(
                "SMARTSTORE-R0-APP-REAUTH",
                "PROVIDER_MEASURED",
                "BLOCKED_BY_TIME",
                False,
                evidence_ref="M2.md#5.5",
                summary="not practically observable; never manufactured",
            )
        )
        return slots

    def evidence(self, state: State | None = None) -> dict[str, Any]:
        campaign = self.ledger.campaign()
        final = state if state is not None else campaign.state
        counts = self.ledger.counts()
        baseline = self._baseline_identity()
        baseline_fingerprint = baseline["account_fingerprint"] if baseline else None
        observations = [
            dict(event["detail"])
            | {
                "matches_baseline": None
                if baseline_fingerprint is None
                else event["detail"]["account_fingerprint"] == baseline_fingerprint
            }
            for event in self.ledger.events("OBSERVATION")
        ]
        comparison = self.ledger.last_event("A3_COMPARISON")
        recovery = self.ledger.last_event("RECOVERY")
        request_fields = (
            "seq",
            "label",
            "phase",
            "phase_attempt",
            "endpoint_id",
            "reserved_at",
            "finished_at",
            "outcome",
            "http_status",
            "latency_ms",
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": campaign.campaign_id,
            "mode": campaign.mode.value,
            "generated_at": now(),
            "git": {
                "head": self.head,
                "approved_sha": self.approved_sha,
                "tree_clean": self.tree_clean,
            },
            "observed_range": {"started_at": self.started_at, "finished_at": now()},
            "campaign_state": final.value,
            "campaign_outcome": final.value if final in TERMINAL else None,
            "renewal_margin_s": RENEWAL_MARGIN_S,
            "budget": {
                "caps": {TOKEN: CAPS[TOKEN], SELLER: CAPS[SELLER], "OTHER": 0},
                "used": {TOKEN: counts[TOKEN], SELLER: counts[SELLER], "OTHER": 0},
                "crash_attempt_cap": CRASH_ATTEMPT_CAP,
                "crash_attempts_used": len(self.ledger.rows("crash_attempts")),
                "blocked_before_send": [
                    {name: row[name] for name in ("at", "endpoint_id", "phase", "reason")}
                    for row in self.ledger.rows("refusals")
                ],
            },
            "requests": [
                {name: row[name] for name in request_fields} for row in self.ledger.rows("requests")
            ],
            "calls": [
                call for role in ROLES for call in caller_calls(self.paths.data_dir(role), role)
            ],
            "phases": [
                {
                    name: row[name]
                    for name in ("phase", "attempt", "opened_at", "closed_at", "result")
                }
                for row in self.ledger.rows("phases")
            ],
            "sessions": [self._sessions[role] for role in ROLES if role in self._sessions],
            "identity": {
                "application_fingerprint": self.application_fingerprint,
                "credential_generation": baseline["credential_generation"] if baseline else None,
                "baseline_account_fingerprint": baseline_fingerprint,
                "observations": observations,
                "a3_comparison": comparison["detail"]["result"] if comparison else None,
                "a3_reason": comparison["detail"]["reason"] if comparison else None,
            },
            "crash": {
                "attempts": [event["detail"] for event in self.ledger.events("CRASH_RESULT")],
                "recovery": recovery["detail"] if recovery else None,
            },
            "slots": self._slots(),
            "scan": self.scan_report,
            "reconcile": self.reconcile_report,
            "regression": self.regression,
            "steps": self.steps,
        }

    def _write_evidence(self, state: State | None = None) -> None:
        EvidenceWriter(self._secrets(evidence=True)).write(
            self.paths.evidence, self.evidence(state)
        )

    # ------------------------------------------------------------------ the whole run

    def execute(self) -> State:
        state = State.COMPLETED
        reason = "COMPLETED"
        try:
            self._baseline()
            self._restart()
            self._recheck()
            self._crash_boundary()
            self._recovery()
            self._closeout()
        except Stop as stop:
            state, reason = stop.state, stop.reason
        except KeyboardInterrupt:
            state, reason = State.STOPPED_INTERRUPTED, "operator interrupt"
        except Exception as exc:  # the harness itself failed: fail closed
            state, reason = State.FAILED, type(exc).__name__
            self.check("harness.error", False, type(exc).__name__)
        self._close("INTERRUPTED" if state is not State.COMPLETED else "PASS")
        try:
            self._write_evidence(state)
        except EvidenceRejected:
            self.check("evidence.sanitized", False, "the evidence was refused and not written")
            state = State.FAILED
        final = self.ledger.finish(state, reason=_code(reason))
        with contextlib.suppress(EvidenceRejected):
            self._write_evidence()
        return final


# ---------------------------------------------------------------- DRY rehearsals


def run_dry(
    out: Path,
    *,
    scenario: Scenario | None = None,
    operator: Operator | None = None,
    floor: Floor | None = None,
    checkout: Checkout | None = None,
) -> tuple[State, CampaignPaths]:
    """The whole campaign against the fake provider: zero SmartStore requests, same code path."""
    paths = CampaignPaths(out.resolve())
    if paths.root.exists() and any(paths.root.iterdir()):
        raise HarnessError("the DRY output directory must be new or empty")
    paths.root.mkdir(parents=True, exist_ok=True)
    scenario = scenario or Scenario.fixture()
    checkout = checkout or GitCheckout()
    floor = floor or SkippedFloor()
    with acquire_data_dir(paths.root, app_version=__version__):
        ledger = init_campaign(
            paths, campaign_id=f"m2-dry-{random.token_hex(4)}", mode=Mode.DRY, scenario=scenario
        )
        prepare_dry_baseline(paths, ledger.campaign(), scenario)
        plan = PreflightPlan(floor=floor, require_clean_tree=False)
        environ = {RENEWAL_MARGIN_ENV: str(RENEWAL_MARGIN_S)}
        if not run_preflight(paths, ledger, checkout=checkout, plan=plan, environ=environ):
            return ledger.campaign().state, paths
        # The approval STOP holds in DRY too: only a DRY ledger can leave it without approval.
        ledger.begin_dry_run()
        run = Run(
            paths,
            ledger,
            operator=operator or ScriptedOperator(),
            floor=floor,
            head=checkout.head(),
            tree_clean=checkout.dirty() == 0,
        )
        return run.execute(), paths


def resume_dry(
    paths: CampaignPaths,
    *,
    operator: Operator | None = None,
    floor: Floor | None = None,
    checkout: Checkout | None = None,
) -> State:
    """Continue a stopped DRY campaign, as a freshly approved REAL invocation would."""
    checkout = checkout or GitCheckout()
    with acquire_data_dir(paths.root, app_version=__version__):
        ledger = Ledger.open(paths.ledger)
        ledger.interrupt_if_abandoned()
        ledger.begin_dry_run()
        run = Run(
            paths,
            ledger,
            operator=operator or ScriptedOperator(),
            floor=floor or SkippedFloor(),
            head=checkout.head(),
            tree_clean=checkout.dirty() == 0,
        )
        return run.execute()
