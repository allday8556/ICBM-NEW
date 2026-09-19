"""The fresh, dedicated M4 acceptance root (Issue #80 PR-F kickoff 5739459941 §B).

A root is accepted only when every one of these holds:
- **No preserved campaign.** No component of the path names a preserved campaign runtime
  (``m3-recon-*`` or ``m3-accept-*``, in any spelling). This is judged on the path as given before
  anything else is looked at, and again once resolved, so a link cannot lead into one.
- **Dedicated.** The root lies outside the repository and outside the default and configured ICBM
  data directories.
- **Fresh.** It does not exist yet, is an empty directory, or holds only this harness's marker in
  state ``INITIALIZED``. A root that has already run is never reused.

Only the root itself is ever listed; no sibling directory is enumerated.
"""

import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePath

from app.config import ConfigError, configured_data_dir, default_data_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKER = "m4-acceptance-root.json"
MARKER_SCHEMA = "icbm-m4-acceptance-root/v1"
DATA = "data"
REPORT = "m4-acceptance-report.json"
PRESERVED = re.compile(r"m3[-_]?(accept|recon)", re.I)

INITIALIZED = "INITIALIZED"
RUNNING = "RUNNING"
PASSED = "PASSED"
FAILED = "FAILED"


class RootRefused(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def names_preserved_campaign(path: PurePath) -> bool:
    return any(PRESERVED.search(part) for part in path.parts)


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _marker_state(path: Path) -> str | None:
    try:
        marker = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("schema") != MARKER_SCHEMA:
        return None
    state = marker.get("state")
    return state if isinstance(state, str) else None


def root_problems(root: Path, environ: Mapping[str, str]) -> list[str]:
    """Why ``root`` is not a fresh dedicated M4 acceptance root; empty when it is. The messages
    never repeat the path."""
    if names_preserved_campaign(PurePath(str(root))):
        return ["the root names a preserved campaign runtime"]
    resolved = root.resolve()
    if names_preserved_campaign(resolved):
        return ["the root resolves into a preserved campaign runtime"]
    problems = []
    if _overlaps(resolved, REPO_ROOT):
        problems.append("the root is inside or contains the repository")
    for resolver, what in (
        (default_data_dir, "the default ICBM data directory"),
        (configured_data_dir, "the configured ICBM data directory"),
    ):
        try:
            directory = resolver(environ)
        except ConfigError:
            directory = None
        if directory is not None and _overlaps(resolved, directory.resolve()):
            problems.append(f"the root overlaps {what}")
    if problems or not resolved.exists():
        return problems
    if not resolved.is_dir():
        return ["the root is not a directory"]
    entries = sorted(entry.name for entry in resolved.iterdir())
    if not entries:
        return []
    if entries == [MARKER] and _marker_state(resolved / MARKER) == INITIALIZED:
        return []
    if MARKER in entries:
        return ["this acceptance root has already been used; every run needs a fresh root"]
    return ["the root is not empty and is not a fresh M4 acceptance root"]


def _write_marker(root: Path, state: str, run_id: str) -> None:
    marker = {
        "schema": MARKER_SCHEMA,
        "state": state,
        "run_id": run_id,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    (root / MARKER).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", "utf-8")


def initialize_root(root: Path, environ: Mapping[str, str]) -> Path:
    """Create a fresh root holding only the marker, for an operator who prepares it ahead."""
    if problems := root_problems(root, environ):
        raise RootRefused(problems)
    resolved = root.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    _write_marker(resolved, INITIALIZED, str(uuid.uuid4()))
    return resolved


def claim_root(root: Path, environ: Mapping[str, str], run_id: str) -> Path:
    """Refuse anything but a fresh dedicated root, then mark it as this run's before any data is
    written."""
    if problems := root_problems(root, environ):
        raise RootRefused(problems)
    resolved = root.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    _write_marker(resolved, RUNNING, run_id)
    return resolved


def settle_root(root: Path, run_id: str, *, passed: bool) -> None:
    _write_marker(root, PASSED if passed else FAILED, run_id)
