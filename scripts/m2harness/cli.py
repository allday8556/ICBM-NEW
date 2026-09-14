"""The M2 acceptance harness command line (Issue #46 §2). Runbook:
docs/acceptance/evidence/README.md.

    m2_acceptance.py                  DRY rehearsal of the whole campaign (the default)
    m2_acceptance.py dry [--out DIR] [--scenario FILE] [--with-regression] [--no-visual]
    m2_acceptance.py init      --campaign-dir DIR --campaign-id ID
    m2_acceptance.py serve     --campaign-dir DIR [--port N]
    m2_acceptance.py preflight --campaign-dir DIR --campaign-id ID [--no-visual]
    m2_acceptance.py approve   --campaign-dir DIR --campaign-id ID --approved-sha SHA
    m2_acceptance.py real      --campaign-dir DIR --campaign-id ID --approved-sha SHA
                               --acknowledge ID --real-provider [--no-visual]
    m2_acceptance.py status    --campaign-dir DIR

Only ``real`` can make a SmartStore request, and only past every gate in ``gates.py``.
"""

import argparse
import contextlib
import functools
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from app import __version__
from app.config import AppConfig
from app.core.egress import EGRESS
from app.core.ownership import DataDirLease, DataDirOwnershipError, acquire_data_dir
from scripts.m2harness import campaign as runner
from scripts.m2harness import crash
from scripts.m2harness.fake_provider import FakeSmartStore, Scenario
from scripts.m2harness.gates import (
    RENEWAL_MARGIN_ENV,
    RENEWAL_MARGIN_S,
    ApprovalRefused,
    GitCheckout,
    RealRequest,
    dedicated_problems,
    issue_approval,
    real_mode_gates,
)
from scripts.m2harness.ledger import (
    PHASE_ROLES,
    Ledger,
    LedgerError,
    Mode,
    Phase,
    State,
)
from scripts.m2harness.paths import CampaignPaths
from scripts.m2harness.transport import BudgetedTransport, InnerFactory, ci_or_test, live_inner

REFUSED_ZERO = "Real SmartStore requests sent: 0."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m2_acceptance.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def campaign(command: argparse.ArgumentParser, *, with_id: bool = True) -> None:
        command.add_argument("--campaign-dir", type=Path, required=True)
        if with_id:
            command.add_argument("--campaign-id", required=True)

    dry = sub.add_parser("dry", help="rehearse the campaign against the fake provider (default)")
    dry.add_argument("--out", type=Path)
    dry.add_argument("--scenario", type=Path, help="a DRY fixture scenario (JSON)")
    dry.add_argument("--with-regression", action="store_true")
    dry.add_argument("--no-visual", action="store_true")
    campaign(sub.add_parser("init", help="create a REAL campaign directory and ledger"))
    serve = sub.add_parser("serve", help="the baseline app for operator preparation")
    campaign(serve, with_id=False)
    serve.add_argument("--port", type=int, default=8791)
    preflight = sub.add_parser("preflight", help="the zero-provider preflight; ends at the STOP")
    campaign(preflight)
    preflight.add_argument("--no-visual", action="store_true")
    approve = sub.add_parser("approve", help="type the approval after the user's go-ahead")
    campaign(approve)
    approve.add_argument("--approved-sha", required=True)
    real = sub.add_parser("real", help="the REAL campaign: only past every gate")
    campaign(real)
    real.add_argument("--approved-sha", required=True)
    real.add_argument("--acknowledge", required=True, help="the campaign ID, typed again")
    real.add_argument("--real-provider", action="store_true")
    real.add_argument("--no-visual", action="store_true")
    campaign(sub.add_parser("status", help="print the ledger (read-only)"), with_id=False)
    child = sub.add_parser("_serve", help=argparse.SUPPRESS)
    child.add_argument("--campaign-dir", type=Path, required=True)
    child.add_argument("--role", choices=("baseline", "crash"), required=True)
    child.add_argument("--port", type=int, required=True)
    child.add_argument("--phases", default="")
    child.add_argument("--crash-attempt", type=int)
    return parser


@contextlib.contextmanager
def _campaign(paths: CampaignPaths) -> Iterator[DataDirLease]:
    """The campaign directory's lease: one harness process per campaign at a time."""
    if not paths.ledger.is_file():
        raise SystemExit(f"REFUSED: no campaign ledger in {paths.root}. {REFUSED_ZERO}")
    try:
        lease = acquire_data_dir(paths.root, app_version=__version__)
    except DataDirOwnershipError as exc:
        raise SystemExit(f"REFUSED: {exc}. {REFUSED_ZERO}") from exc
    with lease:
        yield lease


def _dry(args: argparse.Namespace) -> int:
    EGRESS.install()
    out = args.out or Path(tempfile.mkdtemp(prefix="icbm-m2-dry-")) / "campaign"
    scenario = Scenario.load(args.scenario) if args.scenario else None
    floor: runner.Floor = (
        runner.CommandFloor(visual=not args.no_visual)
        if args.with_regression
        else runner.SkippedFloor()
    )
    state, paths = runner.run_dry(out, scenario=scenario, floor=floor)
    print(f"\nM2 DRY rehearsal: {state} (evidence: {paths.evidence})")
    print("SmartStore requests sent: 0. A fake provider stood behind the real caller.")
    return 0 if state is State.COMPLETED else 1


def _init(args: argparse.Namespace) -> int:
    root = args.campaign_dir.resolve()
    problems = dedicated_problems(root, os.environ)
    if problems:
        print(f"REFUSED: {root} is not a dedicated acceptance directory: {'; '.join(problems)}")
        return 2
    if root.exists() and any(root.iterdir()):
        print("REFUSED: the campaign directory must be new or empty.")
        return 2
    root.mkdir(parents=True, exist_ok=True)
    with acquire_data_dir(root, app_version=__version__):
        try:
            runner.init_campaign(CampaignPaths(root), campaign_id=args.campaign_id, mode=Mode.REAL)
        except (runner.HarnessError, LedgerError) as exc:
            print(f"REFUSED: {exc}")
            return 2
    print(f"Campaign {args.campaign_id} created in {root} (budget token 0/8, seller 0/6).")
    print("Next: `serve` to save the application, record freshness and A0; then `preflight`.")
    return 0


def _serve_operator(args: argparse.Namespace) -> int:
    paths = CampaignPaths(args.campaign_dir.resolve())
    with _campaign(paths):
        campaign = Ledger.open(paths.ledger).campaign()
        if campaign.mode is not Mode.REAL or campaign.state is State.RUNNING:
            print("REFUSED: `serve` prepares an idle REAL campaign only.")
            return 2
        app = runner.AppProcess(
            paths, campaign, runner.BASELINE, name="operator-serve", port=args.port
        )
        try:
            app.wait_ready()
            print(f"Baseline acceptance app: http://127.0.0.1:{app.port}/  (Ctrl+C stops it)")
            print("No phase is open: any SmartStore request is refused before send and recorded.")
            while app.proc.poll() is None:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            app.close()
    return 0


def _preflight(args: argparse.Namespace) -> int:
    paths = CampaignPaths(args.campaign_dir.resolve())
    with _campaign(paths):
        ledger = Ledger.open(paths.ledger)
        campaign = ledger.campaign()
        if campaign.mode is not Mode.REAL or campaign.campaign_id != args.campaign_id:
            print("REFUSED: not this REAL campaign's ledger.")
            return 2
        EGRESS.install()
        plan = runner.PreflightPlan(
            floor=runner.CommandFloor(visual=not args.no_visual),
            ci_checks=runner.github_checks_gate,
            rehearsal=runner.dry_rehearsal_gate,
        )
        passed = runner.run_preflight(
            paths, ledger, checkout=GitCheckout(), plan=plan, environ=os.environ
        )
    return 0 if passed else 1


def _approve(args: argparse.Namespace) -> int:
    blocker = ci_or_test()
    if blocker is not None or not runner.TerminalOperator.interactive():
        print("REFUSED: approval is typed by the operator at an interactive terminal, never in CI.")
        return 2
    paths = CampaignPaths(args.campaign_dir.resolve())
    with _campaign(paths):
        ledger = Ledger.open(paths.ledger)
        campaign = ledger.campaign()
        if campaign.campaign_id != args.campaign_id:
            print("REFUSED: not this campaign's ledger.")
            return 2

        def confirm(phrase: str) -> bool:
            counts = ledger.counts()
            print(f"Campaign {campaign.campaign_id} at {args.approved_sha}: {campaign.state}")
            print(f"Budget used: {counts}. The run spends real SmartStore requests within 8/6.")
            return input(f"Type '{phrase}' to approve: ").strip() == phrase

        try:
            issue_approval(
                paths,
                ledger,
                approved_sha=args.approved_sha,
                checkout=GitCheckout(),
                environ=os.environ,
                confirm=confirm,
            )
        except (ApprovalRefused, LedgerError) as exc:
            print(f"REFUSED: {exc}")
            return 2
    print("Approval recorded. The next `real` invocation consumes it; it is valid for 2 hours.")
    return 0


def _real(args: argparse.Namespace) -> int:
    paths = CampaignPaths(args.campaign_dir.resolve())
    with _campaign(paths):
        with contextlib.suppress(LedgerError):
            if Ledger.open(paths.ledger).interrupt_if_abandoned():
                print("A previous run ended without closing; it is recorded STOPPED_INTERRUPTED.")
        checkout = GitCheckout()
        request = RealRequest(
            args.campaign_id, args.approved_sha, args.acknowledge, args.real_provider
        )
        gates, approval = real_mode_gates(
            request,
            paths,
            environ=os.environ,
            checkout=checkout,
            interactive=runner.TerminalOperator.interactive(),
            registered=functools.partial(runner.registered, paths),
        )
        for gate in gates:
            print(f"[{'PASS' if gate.passed else 'FAIL'}] gate.{gate.name} {gate.detail}".rstrip())
        if approval is None or not all(gate.passed for gate in gates):
            print(f"\nREFUSED before any budget reservation. {REFUSED_ZERO}")
            return 2
        ledger = Ledger.open(paths.ledger)
        ledger.begin_real_run(approval)
        EGRESS.install()  # the harness process itself never needs the provider
        run = runner.Run(
            paths,
            ledger,
            operator=runner.TerminalOperator(),
            floor=runner.CommandFloor(visual=not args.no_visual),
            head=checkout.head(),
            tree_clean=True,
            approved_sha=args.approved_sha,
        )
        state = run.execute()
        counts = ledger.counts()
    print(f"\nM2 campaign {args.campaign_id}: {state}; budget used {counts}")
    print(f"Evidence: {paths.evidence}")
    return 0 if state is State.COMPLETED else 1


def _status(args: argparse.Namespace) -> int:
    paths = CampaignPaths(args.campaign_dir.resolve())
    try:
        ledger = Ledger.open(paths.ledger)
    except LedgerError as exc:
        print(f"REFUSED: {exc}")
        return 2
    campaign = ledger.campaign()
    print(f"campaign {campaign.campaign_id} ({campaign.mode}): {campaign.state}")
    print(f"budget used {ledger.counts()} of token 8 / seller 6 / other 0")
    print(f"crash attempts {[(r['label'], r['verdict']) for r in ledger.rows('crash_attempts')]}")
    print(f"refused before send: {len(ledger.rows('refusals'))}")
    for row in ledger.rows("phases"):
        print(f"  phase {row['phase']}#{row['attempt']}: {row['result'] or 'OPEN'}")
    return 0


def _serve_child(args: argparse.Namespace) -> int:
    """One application process of the campaign (started by the harness, never by hand)."""
    import uvicorn

    from app.main import create_app
    from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller

    paths = CampaignPaths(args.campaign_dir)
    ledger = Ledger.open(paths.ledger)
    campaign = ledger.campaign()
    phases = frozenset(Phase(value) for value in args.phases.split(",") if value)
    if any(PHASE_ROLES[phase] != args.role for phase in phases):
        print("REFUSED: a phase of the other data directory")
        return 2
    margin = os.environ.get(RENEWAL_MARGIN_ENV, "")
    config = AppConfig.from_env(
        {RENEWAL_MARGIN_ENV: margin},
        data_dir=paths.data_dir(args.role),
        host="127.0.0.1",
        port=args.port,
        secret_backend="os",
        log_to_file=True,
    )
    if config.smartstore_renewal_margin_s != RENEWAL_MARGIN_S:
        print(f"REFUSED: the campaign runs with {RENEWAL_MARGIN_ENV}={RENEWAL_MARGIN_S}")
        return 2
    nonce: bytes | None = None
    if args.crash_attempt is not None:
        try:
            nonce = bytes.fromhex(sys.stdin.readline().strip())
        except ValueError:
            nonce = None
        if (
            nonce is None
            or len(nonce) != 32
            or phases != {crash.PHASE_OF_ATTEMPT.get(args.crash_attempt)}
        ):
            print("REFUSED: a crash attempt needs its nonce and its own phase")
            return 2
    inner: InnerFactory
    if campaign.mode is Mode.REAL:
        inner = live_inner()  # refuses under CI and pytest
    else:
        inner = functools.partial(FakeSmartStore, Scenario.load(paths.scenario))
    gate = BudgetedTransport(ledger, phases=phases, inner=inner)
    lease = acquire_data_dir(config.data_dir, app_version=__version__)
    try:
        app = create_app(
            config, ownership=lease, smartstore_caller=SmartStoreEndpointCaller(transport=gate)
        )
        if nonce is not None:
            crash.install_boundary(
                app.state.container.smartstore,
                ledger=ledger,
                campaign_id=campaign.campaign_id,
                crash_attempt=args.crash_attempt,
                marker_path=paths.marker_file(args.crash_attempt),
                nonce=nonce,
                scan_paths=(config.data_dir, paths.run_dir, paths.evidence_dir, paths.ledger),
            )
        uvicorn.run(
            app,
            host=config.host,
            port=config.port,
            log_config=None,
            access_log=False,
            server_header=False,
        )
    finally:
        lease.release()
    return 0


HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "dry": _dry,
    "init": _init,
    "serve": _serve_operator,
    "preflight": _preflight,
    "approve": _approve,
    "real": _real,
    "status": _status,
    "_serve": _serve_child,
}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv) or ["dry"]
    args = build_parser().parse_args(arguments)
    return HANDLERS[args.command](args)
