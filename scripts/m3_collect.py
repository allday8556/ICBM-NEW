"""Rehearse the production one-product COLLECT path offline.

    python scripts/m3_collect.py

A synthetic shop answers and every step after it is the real application. No provider is
contacted, and none can be: the live collection transport refuses to exist under CI or pytest,
and this rehearsal installs the shop as its transport regardless.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.m3collect.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
