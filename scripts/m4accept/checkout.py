"""The exact checkout one M4 acceptance run executes (Issue #80 PR-F, review 5254796929 blocker 1).

A run is evidence for a commit only when the code it executes is that commit and nothing else. The
run is refused before its root is claimed unless every one of these holds:
- **A commit.** The code's own repository top level answers ``git rev-parse HEAD``.
- **No tracked change.** ``git status`` shows no staged or unstaged change, and no tracked file is
  hidden from it (assume-unchanged or skip-worktree).
- **No untracked file.** Nothing untracked and not ignored is in the working tree, so no new
  source can extend or shadow the commit.
- **No ignored source.** No ignored Python source, sourceless bytecode, path file or extension
  module is in the working tree outside the running interpreter's own environment.
- **Loaded from the checkout.** Every loaded ``app``, ``integrations`` and ``scripts`` module comes
  from this checkout.

A count that cannot be measured is unknown (``None``), and unknown refuses the run like any other
problem. The acceptance root lies outside the repository, so the run itself never dirties the tree.
The report keeps the exact HEAD; the architect's closeout compares it with merged main.
"""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.m4accept.root import REPO_ROOT

CODE_PACKAGES = ("app", "integrations", "scripts")
SOURCE_SUFFIXES = (".py", ".pyw", ".pyc", ".pyo", ".pth", ".pyd", ".so")


class CheckoutRefused(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Checkout:
    code_sha: str | None
    tracked_changes: int | None
    hidden_tracked_files: int | None
    untracked_files: int | None
    ignored_sources: int | None
    code_outside_checkout: int

    @property
    def problems(self) -> list[str]:
        problems = []
        if self.code_sha is None:
            problems.append("the code is not the top level of a git checkout with a commit")
        for count, what in (
            (self.tracked_changes, "tracked changes"),
            (self.hidden_tracked_files, "tracked files hidden from git status"),
            (self.untracked_files, "untracked files"),
            (self.ignored_sources, "ignored source files"),
            (self.code_outside_checkout, "loaded code modules from outside the checkout"),
        ):
            if count is None:
                problems.append(f"the checkout's {what} could not be measured")
            elif count:
                problems.append(f"the checkout is not clean: {count} {what}")
        return problems

    def as_json(self) -> dict[str, object]:
        return {
            "tracked_changes": self.tracked_changes,
            "hidden_tracked_files": self.hidden_tracked_files,
            "untracked_files": self.untracked_files,
            "ignored_sources": self.ignored_sources,
            "code_outside_checkout": self.code_outside_checkout,
        }


def _git(repo: Path, *args: str) -> list[str] | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    return done.stdout.splitlines() if done.returncode == 0 else None


def _head(repo: Path) -> str | None:
    top = _git(repo, "rev-parse", "--show-toplevel")
    if not top or Path(top[0]).resolve() != repo.resolve():
        return None
    sha = _git(repo, "rev-parse", "--verify", "HEAD")
    return sha[0] if sha else None


def _environment_prefixes(repo: Path) -> list[str]:
    """The running interpreter's environments inside ``repo``, as ``git`` names paths."""
    prefixes = []
    for prefix in {sys.prefix, sys.base_prefix}:
        path = Path(prefix).resolve()
        if path != repo and path.is_relative_to(repo):
            prefixes.append(f"{path.relative_to(repo).as_posix()}/".casefold())
    return prefixes


def _ignored_source(name: str, environments: list[str]) -> bool:
    if not name.casefold().endswith(SOURCE_SUFFIXES):
        return False
    if name.casefold().startswith(tuple(environments)):
        return False
    # Cached bytecode is never imported without its source; bytecode anywhere else can be.
    return not (name.endswith((".pyc", ".pyo")) and "__pycache__" in name.split("/"))


def code_outside(repo: Path) -> int:
    """Loaded ``app``, ``integrations`` and ``scripts`` modules whose file is not in ``repo``."""
    root = repo.resolve()
    outside = 0
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] not in CODE_PACKAGES:
            continue
        file = getattr(module, "__file__", None)
        if file is not None and not Path(file).resolve().is_relative_to(root):
            outside += 1
    return outside


def probe_checkout(repo: Path = REPO_ROOT) -> Checkout:
    """Measure the checkout at ``repo`` and the code this process loaded from it."""
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    flags = _git(repo, "ls-files", "-v")
    ignored = _git(repo, "ls-files", "--others", "--ignored", "--exclude-standard")
    environments = _environment_prefixes(repo.resolve())
    return Checkout(
        code_sha=_head(repo),
        tracked_changes=None
        if status is None
        else sum(1 for line in status if not line.startswith("??")),
        hidden_tracked_files=None
        if flags is None
        else sum(1 for line in flags if line[:1].islower() or line[:1] == "S"),
        untracked_files=None if status is None else sum(1 for x in status if x.startswith("??")),
        ignored_sources=None
        if ignored is None
        else sum(1 for name in ignored if _ignored_source(name, environments)),
        code_outside_checkout=code_outside(repo),
    )
