"""The M4 offline acceptance harness (Issue #80 PR-F, kickoff 5739459941).

    python scripts/m4_acceptance.py --root <fresh-dedicated-root>
    python scripts/m4_acceptance.py --verify <report.json>

``--root`` names a new or empty directory dedicated to this one run. It must lie outside the
repository and every ICBM data directory, and it must never be a preserved campaign runtime. The
code must run from a clean checkout of exactly one commit: no tracked change, no untracked file and
no ignored source. The run's application data lives under ``<root>/data``. The sanitized report is
written to ``<root>/m4-acceptance-report.json`` and printed. The exit status is 0 only when the
report has no problem, 1 when it has one, and 2 when the root or the checkout was refused before
anything was created.

The run is offline and synthetic. It has no provider capability, so no approval phrase exists or
is needed. ``--verify`` recomputes a report's digest.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.m4accept import evidence
from scripts.m4accept.checkout import CheckoutRefused
from scripts.m4accept.harness import run_acceptance
from scripts.m4accept.root import REPORT, RootRefused


def cmd_run(root: Path) -> int:
    try:
        report = run_acceptance(root, dict(os.environ))
    except (RootRefused, CheckoutRefused) as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True)
    (root.resolve() / REPORT).write_text(text + "\n", "utf-8")
    print(text)
    return 0 if not report["problems"] else 1


def cmd_verify(report_file: Path) -> int:
    report = json.loads(report_file.read_text("utf-8"))
    valid = evidence.verify_report(report)
    print("report digest verified" if valid else "report digest does NOT verify")
    return 0 if valid else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="m4_acceptance")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--root", type=Path)
    target.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    if args.verify is not None:
        return cmd_verify(args.verify)
    return cmd_run(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
