"""Extraction identity of a supplier collection definition (ADR-0010 §12).

The pin lives in ``integrations/suppliers/<key>/extraction_identity.py``, which holds exactly
three constants: ``EXTRACTOR_REVISION``, ``EXTRACTOR_INPUTS`` and ``EXTRACTOR_FINGERPRINT``. The
hashed set is exactly the files named in ``EXTRACTOR_INPUTS``. It must include every module of
the supplier's ``collect/`` package and never the manifest itself, so the computation is acyclic
and reproducible from the repository.

Encoding: files in ascending path-string order; each file's bytes with CRLF converted to LF and
nothing else; the fingerprint is SHA-256 over, per file, the path (UTF-8), a NUL byte, the
lowercase hex SHA-256 of the normalized bytes and ``\\n``.
"""

import ast
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "extraction_identity.py"
COLLECT_PACKAGE = "collect"
_CONSTANTS = ("EXTRACTOR_REVISION", "EXTRACTOR_INPUTS", "EXTRACTOR_FINGERPRINT")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def extractor_fingerprint(repo_root: Path, inputs: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(inputs):
        data = (repo_root / path).read_bytes().replace(b"\r\n", b"\n")
        file_digest = hashlib.sha256(data).hexdigest().encode("ascii")
        digest.update(path.encode("utf-8") + b"\0" + file_digest + b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class Manifest:
    revision: str
    inputs: tuple[str, ...]
    fingerprint: str


def read_manifest(path: Path) -> Manifest:
    """Parse a manifest without importing it; anything but the three literals is refused."""
    tree = ast.parse(path.read_text("utf-8"))
    values: dict[str, object] = {}
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # the module docstring
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if not (
            isinstance(target, ast.Name)
            and target.id in _CONSTANTS
            and target.id not in values
            and value is not None
        ):
            raise ValueError("the manifest holds exactly the three extraction-identity constants")
        try:
            values[target.id] = ast.literal_eval(value)
        except ValueError:
            raise ValueError("extraction-identity constants are literals") from None
    if set(values) != set(_CONSTANTS):
        raise ValueError("the manifest holds exactly the three extraction-identity constants")
    revision, inputs, fingerprint = (values[name] for name in _CONSTANTS)
    if not (
        isinstance(revision, str)
        and isinstance(fingerprint, str)
        and isinstance(inputs, tuple)
        and all(isinstance(item, str) for item in inputs)
    ):
        raise ValueError("revision and fingerprint are strings; inputs is a tuple of paths")
    return Manifest(revision, inputs, fingerprint)


def manifest_problems(repo_root: Path, package: Path) -> list[str]:
    """Why ``package``'s extraction identity is not acyclic, complete and current; empty if it
    is, or if the package has no collection definition at all."""
    manifest_path = package / MANIFEST_NAME
    collect = package / COLLECT_PACKAGE
    if not manifest_path.is_file():
        return (
            ["a collect package without an extraction-identity manifest"]
            if collect.is_dir()
            else []
        )
    try:
        manifest = read_manifest(manifest_path)
    except (ValueError, SyntaxError) as exc:
        return [str(exc)]

    def relative(path: Path) -> str:
        return path.relative_to(repo_root).as_posix()

    problems = []
    if not manifest.revision.strip():
        problems.append("EXTRACTOR_REVISION is empty")
    if relative(manifest_path) in manifest.inputs:
        problems.append("the manifest is part of its own hashed set (cyclic pin)")
    if len(set(manifest.inputs)) != len(manifest.inputs):
        problems.append("EXTRACTOR_INPUTS names a file twice")
    required = {relative(p) for p in collect.rglob("*.py")} if collect.is_dir() else set()
    if not required:
        problems.append("the collect package has no module")
    if missing := sorted(required - set(manifest.inputs)):
        problems.append(f"EXTRACTOR_INPUTS omits collect modules: {missing}")
    absent = [path for path in manifest.inputs if not (repo_root / path).is_file()]
    if absent:
        problems.append(f"EXTRACTOR_INPUTS names missing files: {absent}")
    if not _HEX64.fullmatch(manifest.fingerprint):
        problems.append("EXTRACTOR_FINGERPRINT is not a lowercase SHA-256 digest")
    elif not absent and extractor_fingerprint(repo_root, manifest.inputs) != manifest.fingerprint:
        problems.append(
            "EXTRACTOR_FINGERPRINT is stale: re-pin it, and advance EXTRACTOR_REVISION in the "
            "same change if the extraction semantics changed"
        )
    return problems
