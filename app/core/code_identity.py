"""The running application's code identity (ADR-0018 §9, Gate 3 area 3).

A visual acceptance is bound to the **exact accepted code SHA** — the commit the application's
checkout is at — and, as an additional integrity binding, to a digest of **what the process
executes and serves**. Both must match a reviewed record for it to be current:

- **the commit** (``checkout_sha``): read once, at composition, from the checkout's own git
  metadata, read-only — no git process, no network. Any new commit, a documents-only, tests-only
  or harness-only one included, is another accepted SHA, so every earlier record is stale (§9:
  "again … when the accepted code SHA changed"). An install with no readable checkout has no SHA,
  so no record can ever be current for it.
- **the running code digest** (``code_digest``): every file of the ``app`` and ``integrations``
  packages, every file of the served UI directory, and the dependency pins. It catches what a
  commit cannot: executed or served code changed in the working tree after that commit.

Text files are hashed with ``CRLF`` normalized to ``LF`` (the repository checks out ``eol=lf``; a
local conversion must not make the same code a different identity). Binary files are hashed as they
are. Bytecode caches are never part of the identity.
"""

import functools
import hashlib
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

CODE_IDENTITY_VERSION: Final = "code-identity/v1"
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
CODE_PACKAGES: Final = ("app", "integrations")
DEPENDENCY_PINS: Final = ("pyproject.toml", "constraints.txt")
_TEXT: Final = frozenset(
    {".py", ".js", ".css", ".html", ".json", ".toml", ".txt", ".svg", ".md", ".mako", ".ini"}
)
_SKIPPED_DIRECTORIES: Final = frozenset({"__pycache__"})
_SKIPPED_SUFFIXES: Final = frozenset({".pyc", ".pyo"})
_SHA: Final = re.compile(r"[0-9a-f]{40}")
_REF: Final = re.compile(r"refs/[A-Za-z0-9._/-]+")


def _files(directory: Path) -> Iterator[Path]:
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix in _SKIPPED_SUFFIXES:
            continue
        if _SKIPPED_DIRECTORIES.intersection(path.relative_to(directory).parts):
            continue
        yield path


def file_digest(path: Path) -> str:
    content = path.read_bytes()
    if path.suffix.lower() in _TEXT:
        content = content.replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


def code_manifest(ui_dir: Path, root: Path = REPOSITORY_ROOT) -> dict[str, str]:
    """``{label: sha256}`` of every file the application executes or serves."""
    manifest: dict[str, str] = {}
    for package in CODE_PACKAGES:
        base = root / package
        for path in _files(base):
            manifest[f"{package}/{path.relative_to(base).as_posix()}"] = file_digest(path)
    for path in _files(ui_dir):
        manifest[f"ui/{path.relative_to(ui_dir).as_posix()}"] = file_digest(path)
    for name in DEPENDENCY_PINS:
        pin = root / name
        if pin.is_file():
            manifest[name] = file_digest(pin)
    return manifest


def code_digest(ui_dir: Path, root: Path = REPOSITORY_ROOT) -> str:
    manifest = code_manifest(ui_dir, root)
    lines = "\n".join(f"{label}\t{digest}" for label, digest in sorted(manifest.items()))
    return hashlib.sha256(f"{CODE_IDENTITY_VERSION}\n{lines}".encode()).hexdigest()


def _git_dir(root: Path) -> Path | None:
    dotgit = root / ".git"
    if dotgit.is_dir():
        return dotgit
    if dotgit.is_file():
        # A linked worktree or submodule: ``gitdir: <path>``.
        text = dotgit.read_text("utf-8").strip()
        if text.startswith("gitdir:"):
            return (root / text.removeprefix("gitdir:").strip()).resolve()
    return None


def checkout_sha(root: Path = REPOSITORY_ROOT) -> str | None:
    """The commit the checkout at ``root`` is at, from its git metadata, read-only; ``None`` when
    it cannot be read. A detached HEAD, a branch ref, a packed ref and a linked worktree are read
    exactly as git resolves them; anything else is unreadable, never guessed."""
    try:
        git_dir = _git_dir(root)
        if git_dir is None:
            return None
        common = git_dir
        pointer = git_dir / "commondir"
        if pointer.is_file():
            common = (git_dir / pointer.read_text("utf-8").strip()).resolve()
        head = (git_dir / "HEAD").read_text("utf-8").strip()
        if _SHA.fullmatch(head):
            return head
        ref = head.removeprefix("ref:").strip()
        if not head.startswith("ref:") or not _REF.fullmatch(ref) or ".." in ref:
            return None
        for base in (git_dir, common):
            loose = base / ref
            if loose.is_file():
                value = loose.read_text("utf-8").strip()
                return value if _SHA.fullmatch(value) else None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text("utf-8").splitlines():
                sha, _, name = line.partition(" ")
                if name == ref and _SHA.fullmatch(sha):
                    return sha
    except OSError:
        return None
    return None


@functools.cache
def running_code_digest(ui_dir: Path) -> str:
    """The identity of the code this process loaded, taken once: the process runs the code it
    started with, whatever later changes on disk."""
    return code_digest(ui_dir.resolve())


@functools.cache
def running_checkout_sha(root: Path = REPOSITORY_ROOT) -> str | None:
    """The commit this process was composed at, taken once, like its code digest."""
    return checkout_sha(root)


__all__ = [
    "CODE_IDENTITY_VERSION",
    "checkout_sha",
    "code_digest",
    "code_manifest",
    "file_digest",
    "running_checkout_sha",
    "running_code_digest",
]
