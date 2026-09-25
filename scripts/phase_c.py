"""The Adaptive Collector Phase C campaign harness (Issue #110; see ``scripts/phasec``).

Run it only with the ICBM application stopped::

    python -m scripts.phase_c --campaign-root <dir> [--data-root <dir>] --actor <who> <command> ...
"""

import sys

from scripts.phasec.harness import run


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
