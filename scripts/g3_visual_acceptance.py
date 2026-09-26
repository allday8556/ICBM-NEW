"""The Gate 3 area 3 populated visual and responsive acceptance (ADR-0018 §9).

    python scripts/g3_visual_acceptance.py run --root <fresh-dedicated-root> [--channel msedge]
    python scripts/g3_visual_acceptance.py verify <report.json>

``run`` writes the populated first-vertical scenario into ``<root>/data``, serves it, opens every
required surface at every required viewport in a real browser, and writes the sealed report to
``<root>/g3-visual-report.json`` with full-page screenshots under ``<root>/screens``. The code must
run from a clean checkout of exactly one commit; ``--allow-dirty`` lets a development run proceed,
and its report then says ``checkout_clean: false`` and can never be recorded. The exit status is 0
only for a PASSED report, 1 for a FAILED one, and 2 when the root or the checkout was refused.

``verify`` checks a report against the contract (``app.live.visual.verify_report``) and prints every
problem. A verified report is still not a proof: only ``icbm live record-visual-acceptance``, run by
the operator at the report's exact commit with the reviewer and the review reference that accepted
it, records one. Provider-zero: nothing here calls a marketplace or a supplier.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.live.visual import verify_report
from scripts.g3visual.harness import REPORT, RootRefused, run

DEFAULT_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"


def cmd_run(root: Path, channel: str, allow_dirty: bool) -> int:
    try:
        report = run(root, channel=channel, allow_dirty=allow_dirty)
    except RootRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    print(f"report: {root.resolve() / REPORT}")
    print(f"verdict: {report['verdict']}")
    for failure in report["failures"]:
        print(f"  {failure}")
    for problem in verify_report(report):
        print(f"  contract: {problem}")
    return 0 if report["verdict"] == "PASSED" else 1


def cmd_verify(path: Path) -> int:
    problems = verify_report(json.loads(path.read_text("utf-8")))
    for problem in problems:
        print(problem)
    print("report verified against the contract" if not problems else "report does NOT verify")
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    # Logs and migration titles are UTF-8; a legacy console code page must not break the run.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="g3_visual_acceptance")
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--root", type=Path, required=True)
    runner.add_argument("--channel", default=DEFAULT_CHANNEL)
    runner.add_argument("--allow-dirty", action="store_true")
    verifier = commands.add_parser("verify")
    verifier.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    if args.command == "verify":
        return cmd_verify(args.report)
    return cmd_run(args.root, args.channel, args.allow_dirty)


if __name__ == "__main__":
    sys.exit(main())
