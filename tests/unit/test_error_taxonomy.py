"""ADR-0008 Stage 1: the error taxonomy cannot drift between its documents and the runtime.

One value set is written in several places:
- the runtime ``ErrorClass``;
- ``docs/ARCHITECTURE.md`` §8;
- ADR-0008's mapping and policy tables;
- SmartStore ``ERRORS.md`` §1;
- the migrations;
- through ADR-0008's spelling aliases, the frozen Canonical v3.1 §11.3 at
  ``docs/architecture/CANONICAL-V3.1.md`` (ADR-0009).

These rules fail as soon as any one of them moves alone.
"""

import ast
import re
from collections.abc import Callable
from pathlib import Path

from app.core.errors import AUTO_RETRYABLE, HTTP_STATUS, ErrorClass

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
ARCHITECTURE = DOCS / "ARCHITECTURE.md"
ADR_0008 = DOCS / "adr" / "0008-error-taxonomy-alignment.md"
ERRORS = DOCS / "platforms" / "smartstore" / "ERRORS.md"
CANONICAL_V31 = DOCS / "architecture" / "CANONICAL-V3.1.md"
MIGRATIONS = REPO_ROOT / "app" / "db" / "migrations" / "versions"

ENUM = [c.value for c in ErrorClass]
TOKEN = re.compile(r"`([A-Z_]+)`")
# A v3.1-only class spelling as a token: not part of a provider code (GW.RATE_LIMIT), a longer
# name (POLICY_BLOCKED) or a source id (…-POLICY-3529).
V31_ONLY_SPELLING = re.compile(r"(?<![\w.\-])(RATE_LIMIT|POLICY)(?![\w\-])")
ALIASES = {("RATE_LIMITED", "RATE_LIMIT"), ("POLICY_BLOCKED", "POLICY")}


def _lines(path: Path) -> list[str]:
    return path.read_text("utf-8").splitlines()


def _block_after(lines: list[str], marker: str) -> list[str]:
    """Lines of the first fenced block after the first line that starts with ``marker``."""
    start = next(i for i, line in enumerate(lines) if line.startswith(marker))
    opened = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("```"))
    closed = next(i for i in range(opened + 1, len(lines)) if lines[i].startswith("```"))
    return [line for line in lines[opened + 1 : closed] if line.strip()]


def _table_after(lines: list[str], is_anchor: Callable[[str], bool]) -> list[list[str]]:
    """Data rows of the first markdown table after the anchor line, as stripped cells."""
    start = next(i for i, line in enumerate(lines) if is_anchor(line))
    first = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("|"))
    rows: list[list[str]] = []
    for line in lines[first:]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))])
    return rows[2:]  # the header and the separator


def _token(cell: str) -> str | None:
    match = TOKEN.fullmatch(cell)
    return match.group(1) if match else None


def _mapping_rows() -> list[tuple[str | None, str | None, str | None]]:
    rows = _table_after(
        _lines(ADR_0008), lambda line: line == "### Mapping and compatibility table"
    )
    return [(_token(r[0]), _token(r[1]), _token(r[2])) for r in rows]


def _module_constant(path: Path, name: str) -> object:
    for node in ast.parse(path.read_text("utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} defines no {name}")


def _v31_section_11_3() -> list[str]:
    block = _block_after(_lines(CANONICAL_V31), "### 11.3")
    return [token.strip() for line in block for token in line.split("|") if token.strip()]


# ---------------------------------------------------------------- the documents


def test_architecture_s8_lists_exactly_the_runtime_taxonomy() -> None:
    block = _block_after(_lines(ARCHITECTURE), "Core error classes")
    assert [line.split()[0] for line in block] == ENUM


def test_adr_0008_mapping_table_aligns_the_runtime_with_v1_history_and_frozen_v3_1() -> None:
    rows = _mapping_rows()
    assert [aligned for aligned, _, _ in rows] == ENUM
    # Its v1 column is the set migration 0003 allowed, which is never edited.
    v1_literal = str(
        _module_constant(MIGRATIONS / "0003_m2_marketplace_capabilities.py", "ERROR_CLASSES")
    )
    assert {v1 for _, v1, _ in rows if v1} == set(re.findall(r"'([A-Z_]+)'", v1_literal))
    # Its v3.1 column is exactly the frozen §11.3, spelling included.
    assert {v31 for _, _, v31 in rows if v31} == set(_v31_section_11_3())
    # Only the two ADR-0008 decision-A aliases differ in spelling.
    assert {(a, v) for a, _, v in rows if a and v and a != v} == ALIASES


def test_frozen_v3_1_section_11_3_maps_onto_the_taxonomy_minus_not_found() -> None:
    to_aligned = {v31: aligned for aligned, v31 in ALIASES}
    mapped = {to_aligned.get(token, token) for token in _v31_section_11_3()}
    assert mapped == set(ENUM) - {"NOT_FOUND"}


def test_adr_0008_policy_table_matches_the_runtime_retry_and_http_rules() -> None:
    rows = _table_after(_lines(ADR_0008), lambda line: line.startswith("**Per-class policy.**"))
    assert [_token(r[0]) for r in rows] == ENUM
    assert {ErrorClass(str(_token(r[0]))): int(r[-1]) for r in rows} == HTTP_STATUS
    retried = {ErrorClass(str(_token(r[0]))) for r in rows if r[1].startswith("yes")}
    assert retried == AUTO_RETRYABLE


def test_errors_md_selects_from_the_taxonomy_minus_not_found() -> None:
    lines = _lines(ERRORS)
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("The canonical classes this document selects from are:")
    )
    listed: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            if listed:
                break
            continue
        match = re.fullmatch(r"- `([A-Z_]+)`", line.strip())
        assert match, line
        listed.append(match.group(1))
    assert listed == [c for c in ENUM if c != "NOT_FOUND"]


def test_icbm_contracts_use_a_v3_1_only_spelling_only_when_quoting_v3_1() -> None:
    contracts = [ARCHITECTURE, *sorted((DOCS / "platforms").rglob("*.md"))]
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{number}"
        for path in contracts
        for number, line in enumerate(_lines(path), 1)
        if V31_ONLY_SPELLING.search(line) and "v3.1" not in line
    ]
    assert offenders == []


# ---------------------------------------------------------------- the runtime


def test_the_migrations_record_the_v1_history_and_widen_to_exactly_the_enum() -> None:
    migration = MIGRATIONS / "0005_error_class_taxonomy.py"
    assert list(_module_constant(migration, "ALIGNED_ERROR_CLASSES")) == ENUM  # type: ignore[call-overload]
    v1_literal = str(
        _module_constant(MIGRATIONS / "0003_m2_marketplace_capabilities.py", "ERROR_CLASSES")
    )
    assert list(_module_constant(migration, "V1_ERROR_CLASSES")) == re.findall(  # type: ignore[call-overload]
        r"'([A-Z_]+)'", v1_literal
    )


def test_no_integration_raises_or_names_the_local_not_found_class() -> None:
    # ADR-0008 C: NOT_FOUND is an ICBM-local lookup failure; a provider or wire "not found" is
    # classified by its evidence (ERRORS.md §9.3, §10.4), never as NOT_FOUND.
    modules = sorted((REPO_ROOT / "integrations").rglob("*.py"))
    assert modules
    offenders = []
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            named = (isinstance(node, ast.Name) and node.id == "NotFoundError") or (
                isinstance(node, ast.alias) and node.name == "NotFoundError"
            )
            attribute = (
                isinstance(node, ast.Attribute)
                and node.attr == "NOT_FOUND"
                and isinstance(node.value, ast.Name)
                and node.value.id == "ErrorClass"
            )
            if named or attribute:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")  # type: ignore[attr-defined]
    assert offenders == []
