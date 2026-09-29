"""M3 source reconnaissance harness (ADR-0010 §4, §5; Issue #52 §3).

See scripts/m3harness/cli.py for the commands and their gates.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.m3harness.cli import main

if __name__ == "__main__":
    sys.exit(main())
