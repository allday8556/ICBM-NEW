"""The M3 REAL acceptance campaign ``m3-accept-01`` (Issue #52 rulings 5711123764, 5711187191).

    python scripts/m3_accept.py init      --root <dir>
    python scripts/m3_accept.py arm       --root <dir> --product-url <url> --phase-b-findings <json>
    python scripts/m3_accept.py approve   --root <dir>
    python scripts/m3_accept.py status    --root <dir>
    python scripts/m3_accept.py run-pass  --root <dir> --pass A|B --product-url <url>
    python scripts/m3_accept.py closeout  --root <dir>

``<dir>`` is dedicated to this campaign: outside the repository and every ordinary ICBM data
directory. It holds the ledger, the campaign's own ICBM data directory under ``data/`` — where the
accepted M1 connection must already be established before arming — and the sanitized closeout.

Nothing here is authorized to run against a provider until the prep PR is merged, the exact SHA is
frozen, and the operator types the approval phrase ``approve`` shows them. ``approve`` and
``run-pass`` refuse CI, pytest and non-interactive terminals, and the REAL environment cannot even
be built under them.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collect.collection import RegisteredCollection
from app.config import AppConfig
from integrations.suppliers.extraction import supplier_manifest
from integrations.suppliers.kmretail.collection import COLLECTION
from integrations.suppliers.transport.collection import ci_or_test
from scripts.m2harness.gates import GitCheckout, dedicated_problems
from scripts.m3accept.campaign import (
    Environment,
    closeout,
    run_pass,
    write_report,
)
from scripts.m3accept.ledger import CampaignLedger
from scripts.m3accept.manifest import approval_phrase
from scripts.m3accept.prep import (
    arm,
    local_environment,
    prep_gates,
    real_environment,
    typed_approval_matches,
)

LEDGER = "campaign.sqlite3"
DATA = "data"
REPORT = "closeout.json"


def _ledger(root: Path) -> CampaignLedger:
    return CampaignLedger(root / LEDGER)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _refuse_unattended() -> None:
    if (blocker := ci_or_test()) is not None:
        raise SystemExit(f"refused: a campaign command never runs under {blocker}")
    if not _interactive():
        raise SystemExit("refused: a campaign command runs only in an interactive terminal")


def _registered() -> RegisteredCollection:
    manifest = supplier_manifest(COLLECTION.supplier_key)
    return RegisteredCollection(
        collection=COLLECTION,
        extractor_revision=manifest.revision,
        extractor_fingerprint=manifest.fingerprint,
    )


def cmd_init(root: Path) -> int:
    if problems := dedicated_problems(root, dict(os.environ)):
        raise SystemExit("refused: " + "; ".join(problems))
    CampaignLedger.create(root / LEDGER)
    (root / DATA).mkdir(parents=True, exist_ok=True)
    print(f"initialized m3-accept-01 at {root}")
    return 0


def cmd_arm(root: Path, product_url: str, findings: Path, dry: bool) -> int:
    checkout = GitCheckout()
    if not dry and checkout.dirty():
        raise SystemExit("refused: arming binds an exact commit, and the working tree is not clean")
    env = local_environment(
        AppConfig(data_dir=root / DATA), collection=COLLECTION, registered=_registered()
    )
    manifest = arm(
        _ledger(root),
        env=env,
        product_url=product_url,
        phase_b_findings=json.loads(findings.read_text("utf-8")),
        mode="DRY" if dry else "REAL",
        code_sha=checkout.head(),
    )
    print(json.dumps(manifest.as_json(), indent=2, sort_keys=True))
    print(f"manifest digest {manifest.digest()}")
    return 0


def cmd_approve(root: Path) -> int:
    _refuse_unattended()
    ledger = _ledger(root)
    manifest = ledger.manifest() or {}
    sha = str(manifest.get("code_sha", ""))
    phrase = approval_phrase(sha)
    print("Type this exact phrase to approve two REAL passes of m3-accept-01 at this SHA:")
    print(f"    {phrase}")
    typed = input("> ")
    if not typed_approval_matches(typed, sha):
        print("not approved: the phrase did not match")
        return 1
    ledger.approve(sha)
    print("approved")
    return 0


def cmd_status(root: Path) -> int:
    ledger = _ledger(root)
    print(
        json.dumps(
            {
                "state": ledger.state().value,
                "requests": ledger.counts(),
                "refusals": ledger.refusals(),
                "results": {p: ledger.result(p) for p in ("A", "B")},
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


def _real_env(root: Path) -> tuple[CampaignLedger, Environment]:
    ledger = _ledger(root)
    manifest = ledger.manifest() or {}
    gates = prep_gates(
        root=root,
        manifest=manifest,
        checkout=GitCheckout(),
        environ=dict(os.environ),
    )
    failed = [gate for gate in gates if not gate.passed]
    if failed:
        raise SystemExit(
            "refused by PREP gates: " + "; ".join(f"{g.name} ({g.detail})" for g in failed)
        )
    config = AppConfig(data_dir=root / DATA)
    return ledger, real_environment(config, collection=COLLECTION, registered=_registered())


def cmd_run_pass(root: Path, pass_id: str, product_url: str) -> int:
    _refuse_unattended()
    ledger, env = _real_env(root)
    outcome = run_pass(ledger, env, pass_id, product_url)
    report = {
        "status": outcome.status,
        "pass": outcome.pass_id,
        "seconds_remaining": outcome.seconds_remaining,
        "detail": outcome.detail,
    }
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if outcome.status in ("ACCEPTED", "WAITING_FOR_PACING", "IN_PROGRESS") else 1


def cmd_closeout(root: Path) -> int:
    _refuse_unattended()
    ledger, env = _real_env(root)
    report = closeout(ledger, env)
    write_report(root / REPORT, report)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if not report["problems"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="m3_accept")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "arm", "approve", "status", "run-pass", "closeout"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name in ("arm", "run-pass"):
            command.add_argument("--product-url", required=True)
        if name == "arm":
            command.add_argument("--phase-b-findings", type=Path, required=True)
            command.add_argument("--dry", action="store_true")
        if name == "run-pass":
            command.add_argument("--pass", dest="pass_id", choices=("A", "B"), required=True)
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.root)
    if args.command == "arm":
        return cmd_arm(args.root, args.product_url, args.phase_b_findings, args.dry)
    if args.command == "approve":
        return cmd_approve(args.root)
    if args.command == "status":
        return cmd_status(args.root)
    if args.command == "run-pass":
        return cmd_run_pass(args.root, args.pass_id, args.product_url)
    return cmd_closeout(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
