"""The running application's code identity (ADR-0018 §9, Gate 3 area 3).

A visual acceptance is bound to the exact code it accepted. A running process has no git, so the
identity it can check for itself is a digest of **what it executes and serves**: every file of the
``app`` and ``integrations`` packages, every file of the served UI directory, and the dependency
pins. Anything else in the checkout — documents, acceptance evidence, tests, scripts — is not
code the application runs, so publishing evidence under ``docs/`` never moves it, while any change
to executed or served code does.

The git commit a harness ran at is recorded beside this digest as provenance; the recorder requires
that commit to be the checkout's HEAD, so a report from another commit is never recorded (see
``app.live.visual``).

Text files are hashed with ``CRLF`` normalized to ``LF`` (the repository checks out ``eol=lf``; a
local conversion must not make the same code a different identity). Binary files are hashed as they
are. Bytecode caches are never part of the identity.
"""

import functools
import hashlib
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


@functools.cache
def running_code_digest(ui_dir: Path) -> str:
    """The identity of the code this process loaded, taken once: the process runs the code it
    started with, whatever later changes on disk."""
    return code_digest(ui_dir.resolve())


__all__ = [
    "CODE_IDENTITY_VERSION",
    "code_digest",
    "code_manifest",
    "file_digest",
    "running_code_digest",
]
