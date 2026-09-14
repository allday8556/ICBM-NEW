"""Real-mode gates and approval material (Issue #46 §2.4; comment 5669037896 point 3).

A REAL run starts only when every gate below passes, and all of them are evaluated before any
budget reservation, before the live transport exists, and before any application process starts:

* the explicit ``--real-provider`` flag, outside CI or a test run, with an interactive operator;
* an explicit campaign ID, an acknowledgement typed to match it, and a ledger of that ID;
* the exact approved SHA checked out, with a clean working tree (untracked files included);
* ``ICBM_SMARTSTORE_RENEWAL_MARGIN_S=600`` configured;
* a dedicated campaign directory, outside the repository and every ordinary ICBM data directory;
* a readable, intact REAL ledger that is not exhausted and is waiting at the approval STOP (or
  stopped in a resumable state), registered on this machine under the same nonce;
* the preflight status file that the ledger recorded as PASSED, for this campaign and this SHA;
* current approval material: written by ``approve`` from an interactive terminal after the
  user's explicit go-ahead, bound to the campaign, the SHA and that preflight, no older than
  ``APPROVAL_MAX_AGE``, and still the ledger's latest event, so it has been neither used nor
  superseded.

No flag or default satisfies the approval: ``approve`` refuses non-interactive and CI runs, and
``Ledger.begin_real_run`` consumes the approval atomically.
"""

import hashlib
import json
import re
import secrets
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from app.config import DEFAULT_DATA_DIR
from scripts.m2harness.ledger import (
    CAMPAIGN_ID,
    CAPS,
    RESUMABLE,
    Campaign,
    Ledger,
    LedgerError,
    Mode,
    State,
)
from scripts.m2harness.paths import CampaignPaths
from scripts.m2harness.transport import ci_or_test

REPO_ROOT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"^[0-9a-f]{40}$")
RENEWAL_MARGIN_S = 600
RENEWAL_MARGIN_ENV = "ICBM_SMARTSTORE_RENEWAL_MARGIN_S"
APPROVAL_KIND = "M2_REAL_PROVIDER_APPROVAL"
PREFLIGHT_KIND = "M2_PREFLIGHT_STATUS"
# How long approval material stays usable after the operator types it. Anything recorded in the
# ledger after it (a new preflight, another approval, a run) makes it stale at once.
APPROVAL_MAX_AGE = timedelta(hours=2)


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str = ""


class Checkout(Protocol):
    def head(self) -> str: ...

    def dirty(self) -> int: ...


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


class GitCheckout:
    def head(self) -> str:
        return _git("rev-parse", "HEAD")

    def dirty(self) -> int:
        """Changed and untracked paths, ignored files excluded."""
        status = _git("status", "--porcelain", "--untracked-files=all")
        return sum(1 for line in status.splitlines() if line.strip())


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def dedicated_problems(root: Path, environ: Mapping[str, str]) -> list[str]:
    """Why ``root`` is not a dedicated acceptance directory; empty when it is."""
    resolved = root.resolve()
    problems = []
    if _overlaps(resolved, REPO_ROOT):
        problems.append("it is inside or contains the repository")
    if _overlaps(resolved, DEFAULT_DATA_DIR.resolve()):
        problems.append("it overlaps the default ICBM data directory")
    ordinary = environ.get("ICBM_DATA_DIR")
    if ordinary and _overlaps(resolved, Path(ordinary).resolve()):
        problems.append("it overlaps the ICBM_DATA_DIR of this shell")
    return problems


def approval_phrase(campaign_id: str, approved_sha: str) -> str:
    return f"APPROVE {campaign_id} {approved_sha[:12]}"


class ApprovalRefused(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _preflight_digest(ledger: Ledger, preflight: Path) -> tuple[str | None, str | None]:
    """The digest and SHA of the preflight the ledger recorded as passed, if its file is intact."""
    event = ledger.last_event("PREFLIGHT_PASSED")
    if event is None or not preflight.is_file():
        return None, None
    recorded = event["detail"].get("digest")
    return (
        (recorded, event["detail"].get("head")) if digest(preflight) == recorded else (None, None)
    )


def issue_approval(
    paths: CampaignPaths,
    ledger: Ledger,
    *,
    approved_sha: str,
    checkout: Checkout,
    environ: Mapping[str, str],
    confirm: Callable[[str], bool],
) -> str:
    """Write the campaign-bound approval material and record its digest in the ledger.

    The CLI calls this only from an interactive terminal, after the user's explicit go-ahead;
    ``confirm`` receives the exact phrase the operator has to type.
    """
    campaign = ledger.campaign()
    problems = []
    blocker = ci_or_test(environ)
    if blocker is not None:
        problems.append(f"approval is never issued under {blocker}")
    if campaign.mode is not Mode.REAL:
        problems.append("only a REAL campaign takes real-provider approval")
    if (
        campaign.state is not State.AWAITING_REAL_PROVIDER_APPROVAL
        and campaign.state not in RESUMABLE
    ):
        problems.append(f"the campaign is {campaign.state}, not waiting for approval")
    if not SHA.fullmatch(approved_sha) or checkout.head() != approved_sha:
        problems.append("the approved SHA is not the checked-out HEAD")
    if checkout.dirty():
        problems.append("the working tree is not clean")
    preflight, preflight_head = _preflight_digest(ledger, paths.preflight)
    if preflight is None:
        problems.append("there is no passed preflight status file for this campaign")
    elif preflight_head != approved_sha:
        problems.append("the preflight ran at another SHA")
    if problems:
        raise ApprovalRefused(problems)
    if not confirm(approval_phrase(campaign.campaign_id, approved_sha)):
        raise ApprovalRefused(["the operator did not type the approval phrase"])
    document = {
        "kind": APPROVAL_KIND,
        "campaign_id": campaign.campaign_id,
        "approved_sha": approved_sha,
        "preflight_digest": preflight,
        "issued_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "nonce": secrets.token_hex(16),
    }
    paths.approval.write_text(json.dumps(document, indent=2) + "\n", "utf-8")
    value = digest(paths.approval)
    ledger.issue_approval(value, approved_sha=approved_sha)
    return value


@dataclass(frozen=True)
class RealRequest:
    campaign_id: str | None
    approved_sha: str | None
    acknowledge: str | None
    real_provider: bool


def _approval(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def real_mode_gates(
    request: RealRequest,
    paths: CampaignPaths,
    *,
    environ: Mapping[str, str],
    checkout: Checkout,
    interactive: bool,
    registered: Callable[[Campaign], bool],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[list[Gate], str | None]:
    """Every gate, evaluated without side effects, and the approval digest to consume."""
    gates: list[Gate] = []

    def gate(name: str, passed: object, detail: str = "") -> None:
        gates.append(Gate(name, bool(passed), "" if passed else detail))

    blocker = ci_or_test(environ)
    gate("real_provider_flag", request.real_provider, "--real-provider is required")
    gate("not_ci_or_test", blocker is None, f"running under {blocker}")
    gate("interactive_operator", interactive, "an operator must be at an interactive terminal")
    campaign_ok = request.campaign_id is not None and CAMPAIGN_ID.fullmatch(request.campaign_id)
    gate("campaign_id_explicit", campaign_ok, "--campaign-id is required")
    gate(
        "acknowledgement_matches",
        campaign_ok and request.acknowledge == request.campaign_id,
        "--acknowledge must repeat the campaign ID exactly",
    )
    head = checkout.head()
    gate(
        "approved_sha_is_head",
        request.approved_sha is not None
        and SHA.fullmatch(request.approved_sha)
        and head == request.approved_sha,
        "--approved-sha must be the full SHA of the checked-out HEAD",
    )
    dirty = checkout.dirty()
    gate("tree_clean", dirty == 0, f"{dirty} changed or untracked path(s)")
    gate(
        "renewal_margin_600",
        environ.get(RENEWAL_MARGIN_ENV) == str(RENEWAL_MARGIN_S),
        f"set {RENEWAL_MARGIN_ENV}={RENEWAL_MARGIN_S}",
    )
    problems = dedicated_problems(paths.root, environ)
    gate("dedicated_campaign_dir", not problems, "; ".join(problems))

    try:
        ledger: Ledger | None = Ledger.open(paths.ledger)
    except LedgerError as exc:
        ledger = None
        gate("ledger_readable", False, str(exc))
    if ledger is None:
        return gates, None
    gate("ledger_readable", True)
    campaign = ledger.campaign()
    counts = ledger.counts()
    gate(
        "campaign_id_matches_ledger",
        campaign.campaign_id == request.campaign_id,
        "the ledger belongs to another campaign",
    )
    gate("ledger_mode_real", campaign.mode is Mode.REAL, "a DRY ledger never runs for real")
    gate(
        "ledger_waiting_for_approval",
        campaign.state is State.AWAITING_REAL_PROVIDER_APPROVAL or campaign.state in RESUMABLE,
        f"the campaign is {campaign.state}",
    )
    gate(
        "budget_not_exhausted",
        campaign.state is not State.BUDGET_EXHAUSTED
        and any(counts[endpoint] < cap for endpoint, cap in CAPS.items()),
        "the campaign budget is exhausted",
    )
    # The OS credential store is never probed under CI or tests: the gate simply fails there.
    gate(
        "campaign_registered",
        blocker is None and registered(campaign),
        "no matching registration on this machine (never probed under CI or tests)",
    )

    preflight, preflight_head = _preflight_digest(ledger, paths.preflight)
    gate(
        "preflight_passed",
        preflight is not None and preflight_head == request.approved_sha,
        "no passed preflight status file for this campaign at this SHA",
    )

    document = _approval(paths.approval)
    if document is None:
        gate("approval_material", False, "run `approve` after the user's explicit go-ahead")
        return gates, None
    issued = None
    try:
        issued = datetime.fromisoformat(str(document.get("issued_at")))
    except ValueError:
        issued = None
    age = clock() - issued if issued is not None and issued.tzinfo is not None else None
    gate(
        "approval_material",
        document.get("kind") == APPROVAL_KIND
        and document.get("campaign_id") == request.campaign_id == campaign.campaign_id
        and document.get("approved_sha") == request.approved_sha
        and preflight is not None
        and document.get("preflight_digest") == preflight,
        "the approval is for another campaign, SHA or preflight",
    )
    gate(
        "approval_fresh",
        age is not None and timedelta(0) <= age <= APPROVAL_MAX_AGE,
        f"the approval is older than {APPROVAL_MAX_AGE} or has no valid time",
    )
    value = digest(paths.approval)
    events = ledger.events()
    latest = events[-1] if events else None
    current = (
        latest is not None
        and latest["kind"] == "APPROVAL_ISSUED"
        and latest["detail"].get("digest") == value
    )
    gate("approval_current", current, "the approval was already used or has been superseded")
    # The approval is handed out only when every gate passed; under CI or pytest, never.
    return gates, value if current and all(g.passed for g in gates) else None
