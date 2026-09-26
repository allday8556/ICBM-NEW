"""The two roots a Phase C command uses, kept apart (supplement `5313045448`).

``--data-root`` is the operator's live ICBM data root, protected by ADR-0006. ``--campaign-root``
is the operator-supplied evidence directory: outside the repository, outside the data root, and
outside the default and configured ICBM data directories. Neither contains the other. No location
is hard-coded; the operator names both.
"""

from collections.abc import Mapping
from pathlib import Path

from app.config import ConfigError, configured_data_dir, default_data_dir

REPO_ROOT = Path(__file__).resolve().parents[2]


class RootsRefused(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def campaign_root_problems(
    campaign_root: Path, data_root: Path | None, environ: Mapping[str, str]
) -> list[str]:
    """Why the campaign root may not hold Phase C evidence; empty when it may."""
    if not campaign_root.is_absolute():
        return ["the campaign root is an absolute path the operator names"]
    resolved = campaign_root.resolve()
    problems = []
    if _overlaps(resolved, REPO_ROOT):
        problems.append("the campaign root is inside or contains the repository")
    if data_root is not None and _overlaps(resolved, data_root.resolve()):
        problems.append("the campaign root overlaps the data root")
    for resolver, what in (
        (default_data_dir, "the default ICBM data directory"),
        (configured_data_dir, "the configured ICBM data directory"),
    ):
        try:
            directory = resolver(environ)
        except ConfigError:
            directory = None
        if directory is not None and _overlaps(resolved, directory.resolve()):
            problems.append(f"the campaign root overlaps {what}")
    return problems


def require_roots(campaign_root: Path, data_root: Path | None, environ: Mapping[str, str]) -> None:
    if data_root is not None and not data_root.is_absolute():
        raise RootsRefused(["the data root is an absolute path the operator names"])
    if problems := campaign_root_problems(campaign_root, data_root, environ):
        raise RootsRefused(problems)
