"""The fresh, dedicated M5 acceptance root (Issue #89 PR-F §A).

The rules are the accepted M4 ones, applied with this run kind's own marker so a used M4 root is
never a fresh M5 one and the other way round: no component of the path may name a preserved
campaign runtime, the root lies outside the repository and every ICBM data directory, and it does
not exist, is empty, or holds only this harness's `INITIALIZED` marker.
"""

from collections.abc import Mapping
from pathlib import Path

from scripts.m4accept.root import (
    FAILED,
    INITIALIZED,
    PASSED,
    RUNNING,
    RootRefused,
    names_preserved_campaign,
)
from scripts.m4accept.root import (
    claim_root as _claim_root,
)
from scripts.m4accept.root import (
    initialize_root as _initialize_root,
)
from scripts.m4accept.root import (
    root_problems as _root_problems,
)
from scripts.m4accept.root import (
    settle_root as _settle_root,
)

MARKER = "m5-acceptance-root.json"
MARKER_SCHEMA = "icbm-m5-acceptance-root/v1"
REPORT = "m5-acceptance-report.json"
DATA = "data"

__all__ = [
    "DATA",
    "FAILED",
    "INITIALIZED",
    "MARKER",
    "MARKER_SCHEMA",
    "PASSED",
    "REPORT",
    "RUNNING",
    "RootRefused",
    "claim_root",
    "initialize_root",
    "names_preserved_campaign",
    "root_problems",
    "settle_root",
]


def root_problems(root: Path, environ: Mapping[str, str]) -> list[str]:
    return _root_problems(root, environ, marker=MARKER)


def initialize_root(root: Path, environ: Mapping[str, str]) -> Path:
    return _initialize_root(root, environ, marker=MARKER, schema=MARKER_SCHEMA)


def claim_root(root: Path, environ: Mapping[str, str], run_id: str) -> Path:
    return _claim_root(root, environ, run_id, marker=MARKER, schema=MARKER_SCHEMA)


def settle_root(root: Path, run_id: str, *, passed: bool) -> None:
    _settle_root(root, run_id, passed=passed, marker=MARKER, schema=MARKER_SCHEMA)
