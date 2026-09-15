"""The acyclic extraction-identity pin (ADR-0010 §12), on synthetic packages in a temp tree."""

import hashlib
from pathlib import Path

from integrations.suppliers.extraction import (
    extractor_fingerprint,
    manifest_problems,
)

PACKAGE = "integrations/suppliers/demo"
INPUTS = (f"{PACKAGE}/collect/__init__.py", f"{PACKAGE}/collect/parser.py")


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _manifest(inputs: tuple[str, ...], fingerprint: str, extra: str = "") -> str:
    return (
        '"""Demo extraction identity."""\n'
        'EXTRACTOR_REVISION = "demo-collect-r1"\n'
        f"EXTRACTOR_INPUTS = {inputs!r}\n"
        f'EXTRACTOR_FINGERPRINT = "{fingerprint}"\n' + extra
    )


def _package(root: Path, *, inputs: tuple[str, ...] = INPUTS, extra: str = "") -> Path:
    _write(root, INPUTS[0], "")
    _write(root, INPUTS[1], "def parse(view):\n    return view.status\n")
    fingerprint = extractor_fingerprint(root, inputs)
    _write(root, f"{PACKAGE}/extraction_identity.py", _manifest(inputs, fingerprint, extra))
    return root / PACKAGE


def test_the_fingerprint_follows_the_frozen_encoding(tmp_path: Path) -> None:
    _write(tmp_path, "b.py", "x\r\n")
    _write(tmp_path, "a.py", "y\n")
    expected = hashlib.sha256()
    for path, data in (("a.py", b"y\n"), ("b.py", b"x\n")):  # sorted paths, CRLF -> LF
        expected.update(path.encode() + b"\0" + hashlib.sha256(data).hexdigest().encode() + b"\n")
    assert extractor_fingerprint(tmp_path, ["b.py", "a.py"]) == expected.hexdigest()


def test_a_current_pin_has_no_problem(tmp_path: Path) -> None:
    assert manifest_problems(tmp_path, _package(tmp_path)) == []


def test_editing_a_hashed_file_makes_the_pin_stale(tmp_path: Path) -> None:
    package = _package(tmp_path)
    _write(tmp_path, INPUTS[1], "def parse(view):\n    return view.path\n")
    assert any("stale" in p for p in manifest_problems(tmp_path, package))


def test_line_endings_alone_do_not_change_the_pin(tmp_path: Path) -> None:
    package = _package(tmp_path)
    _write(tmp_path, INPUTS[1], "def parse(view):\r\n    return view.status\r\n")
    assert manifest_problems(tmp_path, package) == []


def test_the_manifest_is_never_part_of_its_own_hash(tmp_path: Path) -> None:
    manifest = f"{PACKAGE}/extraction_identity.py"
    _write(tmp_path, manifest, "")
    package = _package(tmp_path, inputs=(*INPUTS, manifest))
    assert any("cyclic" in p for p in manifest_problems(tmp_path, package))


def test_every_collect_module_is_hashed(tmp_path: Path) -> None:
    package = _package(tmp_path)
    _write(tmp_path, f"{PACKAGE}/collect/stock.py", "RULE = 1\n")  # a new parser file escapes
    assert any("omits collect modules" in p for p in manifest_problems(tmp_path, package))


def test_the_manifest_holds_only_the_three_constants(tmp_path: Path) -> None:
    package = _package(tmp_path, extra="HELPER = 1\n")
    assert any("exactly the three" in p for p in manifest_problems(tmp_path, package))


def test_a_collect_package_needs_a_manifest(tmp_path: Path) -> None:
    _write(tmp_path, f"{PACKAGE}/collect/parser.py", "")
    assert manifest_problems(tmp_path, tmp_path / PACKAGE) != []
    assert manifest_problems(tmp_path, tmp_path / "integrations/suppliers/none") == []
