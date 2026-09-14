"""M2 SmartStore CONNECT acceptance harness (Issue #46 §2).

Without arguments it runs the DRY rehearsal: the whole campaign against a fake provider behind
the real registry-gated caller, with zero SmartStore requests. Commands and the runbook:
``scripts/m2harness/cli.py`` and ``docs/acceptance/evidence/README.md``.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.m2harness.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
