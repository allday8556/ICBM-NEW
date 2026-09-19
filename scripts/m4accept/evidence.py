"""Read-only evidence and the sanitized report (Issue #80 PR-F kickoff 5739459941 §M, §O).

Immutability is proven from durable state, not from what a write call returned:
- every table is read through a read-only SQLite connection (``mode=ro``);
- each row is digested, and later rows are compared with earlier ones;
- stored bytes are hashed again from disk.

Nothing here writes. The report is canonical JSON with a digest over everything else in it. Before
it is written it is scanned for anything that must never leave the run: URLs, absolute paths,
secret-like words, the approval-phrase keyword and non-ASCII business text.
"""

import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

REPORT_SCHEMA = "icbm-m4-acceptance-report/v1"
DIGEST_FIELD = "report_digest"
# A binding's only permitted change: its open validity window closes (ADR-0013 §6).
CLOSABLE = {"source_bindings": "valid_to"}
_LEAKS = (
    ("a URL", re.compile(r"[a-z][a-z0-9+.-]*://", re.I)),
    ("an absolute Windows path", re.compile(r"(^|[\s\"'=(])[A-Za-z]:[\\/]")),
    ("a UNC or backslash path", re.compile(r"\\\\|[A-Za-z0-9_.-]\\[A-Za-z0-9_.-]")),
    (
        "an absolute POSIX path",
        re.compile(r"(^|[\s\"'=(])/(home|users|tmp|var|etc|root|mnt)\b", re.I),
    ),
    (
        "secret-like material",
        re.compile(r"cookie|secret|password|passwd|token|bearer|api[_-]?key", re.I),
    ),
    ("an approval phrase", re.compile(r"\bAPPROVE\b")),
    ("non-ASCII text", re.compile(r"[^\x00-\x7f]")),
)


@contextlib.contextmanager
def read_only(database: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(connection: sqlite3.Connection, table: str) -> list[tuple[str, int]]:
    return [(row[1], row[5]) for row in connection.execute(f"PRAGMA table_info({table})")]


def rows(connection: sqlite3.Connection, table: str) -> dict[str, dict[str, object]]:
    """Every row of ``table`` keyed by its primary key (or rowid), as column → value."""
    columns = _columns(connection, table)
    names = [name for name, _pk in columns]
    keys = [name for name, pk in sorted(columns, key=lambda c: c[1]) if pk]
    select = ", ".join(names)
    result: dict[str, dict[str, object]] = {}
    for row in connection.execute(f"SELECT rowid, {select} FROM {table}"):
        record = dict(zip(names, row[1:], strict=True))
        key = json.dumps([record[k] for k in keys] if keys else [row[0]], default=str)
        result[key] = record
    return result


def snapshot(database: Path) -> dict[str, dict[str, dict[str, object]]]:
    with read_only(database) as connection:
        return {table: rows(connection, table) for table in tables(connection)}


def digest(structure: object) -> str:
    text = json.dumps(structure, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def database_digest(database: Path) -> str:
    """A digest over every row of every table: equal digests mean nothing was written."""
    return digest(snapshot(database))


def history_changes(
    before: Mapping[str, Mapping[str, Mapping[str, object]]],
    after: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> list[str]:
    """Every earlier row that is gone or changed, except an open binding closing its window."""
    changes = []
    for table, earlier in before.items():
        later = after.get(table, {})
        closable = CLOSABLE.get(table)
        for key, row in earlier.items():
            now = later.get(key)
            if now is None:
                changes.append(f"{table}: a row disappeared")
                continue
            if now == row:
                continue
            differing = {name for name in row if row[name] != now.get(name)}
            closed = (
                closable is not None
                and differing == {closable}
                and row[closable] is None
                and now.get(closable) is not None
            )
            if not closed:
                changes.append(f"{table}: a row changed ({', '.join(sorted(differing))})")
    return changes


def file_digests(directory: Path) -> dict[str, str]:
    """Each stored file's content digest, keyed by its content address (its own name)."""
    if not directory.is_dir():
        return {}
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def content_addressed(files: Mapping[str, str]) -> bool:
    return all(name == value for name, value in files.items())


def canonical(report: Mapping[str, object]) -> str:
    return json.dumps(
        {k: v for k, v in report.items() if k != DIGEST_FIELD},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def report_digest(report: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical(report).encode("ascii")).hexdigest()


def verify_report(report: Mapping[str, object]) -> bool:
    return report.get(DIGEST_FIELD) == report_digest(report)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def leaks(report: Mapping[str, object], forbidden: Iterable[str] = ()) -> list[str]:
    """What in ``report`` must not leave the run: kinds only, never the leaking text itself."""
    found = set()
    forbidden = [text for text in forbidden if text]
    for text in _strings(report):
        for what, pattern in _LEAKS:
            if pattern.search(text):
                found.add(what)
        if any(item in text for item in forbidden):
            found.add("a local path of this run")
    return sorted(found)
