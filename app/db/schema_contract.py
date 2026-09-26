"""The schema contract at head (ADR-0018 §7, §8): what the shipped migrations build.

A restore drill must prove the restored root's **schema itself** — every table, index, trigger and
view with its SQL — not only its ``alembic_version`` marker, and the evidence-retention proof must
prove the guard triggers' **semantics**, not their names. Both compare against one **expected
manifest**: the schema the shipped Alembic migrations build on an empty database, derived once per
process for the code's head in a private temporary directory (its own data directory, never the
active root, read back read-only).

The live database never defines what is expected: a dropped, altered, replaced or extra schema
object — a same-name no-op trigger included — differs from the contract. A database migrated by a
SQLite build that stores different SQL for the same migration differs as well; that fails closed
and names the objects, it is never reconciled here.
"""

import contextlib
import functools
import hashlib
import json
import re
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.db.migrate import upgrade_to_head

MANIFEST_VERSION: Final = "schema-manifest/v1"
# Every schema object except SQLite's own (``sqlite_sequence``, ``sqlite_autoindex_*``, statistics).
MANIFEST_QUERY: Final = (
    "SELECT type, name, tbl_name, sql FROM sqlite_master"
    " WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
)


class SchemaContractUnavailable(RuntimeError):
    """The expected schema could not be derived; every comparison against it fails closed."""


@dataclass(frozen=True)
class SchemaObject:
    kind: str
    name: str
    table: str
    sql: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass(frozen=True)
class SchemaManifest:
    objects: Mapping[str, SchemaObject]

    @property
    def digest(self) -> str:
        return digest_of(self.objects.values())

    def on_table(self, table: str, kind: str = "trigger") -> dict[str, SchemaObject]:
        return {o.name: o for o in self.objects.values() if o.kind == kind and o.table == table}

    def differences(self, actual: "SchemaManifest") -> list[str]:
        """Every object that is missing, changed or unexpected in ``actual``, by key only."""
        found: list[str] = []
        for key in sorted(set(self.objects) | set(actual.objects)):
            expected, seen = self.objects.get(key), actual.objects.get(key)
            if seen is None:
                found.append(f"missing:{key}")
            elif expected is None:
                found.append(f"unexpected:{key}")
            elif seen != expected:
                found.append(f"changed:{key}")
        return found


def normalized_sql(sql: str | None) -> str:
    return " ".join((sql or "").split())


_TABLE_CONSTRAINTS: Final = ("CONSTRAINT ", "PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY")


def canonical_table_sql(sql: str) -> str:
    """A table's SQL with its table-level constraints in one canonical order.

    A batch migration re-creates a table from a set of constraints, so SQLite stores the same
    table with its constraint clauses in an order that varies between processes. Columns keep
    their order; each clause keeps its text; only the order of the constraint clauses is fixed.
    """
    start, end = sql.find("("), sql.rfind(")")
    if start < 0 or end < start:
        return sql
    items: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in sql[start + 1 : end]:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    items.append("".join(current).strip())
    columns = [i for i in items if not i.upper().startswith(_TABLE_CONSTRAINTS)]
    constraints = sorted(i for i in items if i.upper().startswith(_TABLE_CONSTRAINTS))
    return f"{sql[:start].rstrip()} ({', '.join(columns + constraints)}){sql[end + 1 :]}"


def manifest_of(rows: Iterable[tuple[str, str, str, str | None]]) -> SchemaManifest:
    """The manifest of ``MANIFEST_QUERY`` rows, read from any database."""
    objects = [
        SchemaObject(
            str(kind),
            str(name),
            str(table),
            canonical_table_sql(normalized_sql(sql)) if kind == "table" else normalized_sql(sql),
        )
        for kind, name, table, sql in rows
    ]
    return SchemaManifest({o.key: o for o in sorted(objects, key=lambda o: o.key)})


# A delete guard that refuses every delete: no WHEN clause, no condition but ``WHERE 1``, ABORT.
_UNCONDITIONAL_DELETE_REFUSAL: Final = re.compile(
    r"CREATE TRIGGER (\w+) BEFORE DELETE ON (\w+) BEGIN SELECT RAISE\(ABORT, '[^']*'\)"
    r"(?: WHERE 1)?; END"
)


def refuses_every_delete(trigger: SchemaObject, table: str) -> bool:
    """Whether ``trigger`` — by its normalized SQL, not its name — refuses every delete of
    ``table``."""
    match = _UNCONDITIONAL_DELETE_REFUSAL.fullmatch(trigger.sql)
    return match is not None and match.group(1) == trigger.name and match.group(2) == table


def digest_of(objects: Iterable[SchemaObject]) -> str:
    encoded = json.dumps(
        [[o.kind, o.name, o.table, o.sql] for o in sorted(objects, key=lambda o: o.key)],
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{MANIFEST_VERSION}\n{encoded}".encode()).hexdigest()


@functools.cache
def expected_manifest(head: str) -> SchemaManifest:
    """The schema the shipped migrations build at ``head``: derived, never read from a live root."""
    with tempfile.TemporaryDirectory(
        prefix="icbm-schema-contract-", ignore_cleanup_errors=True
    ) as directory:
        database = Path(directory) / "icbm.db"
        upgrade_to_head(f"sqlite:///{database.as_posix()}")
        uri = f"{database.resolve().as_uri()}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as built:
            version = built.execute("SELECT version_num FROM alembic_version").fetchone()
            if version is None or str(version[0]) != head:
                raise SchemaContractUnavailable(
                    "the shipped migrations do not build the expected head"
                )
            return manifest_of(built.execute(MANIFEST_QUERY).fetchall())


__all__ = [
    "MANIFEST_QUERY",
    "MANIFEST_VERSION",
    "SchemaContractUnavailable",
    "SchemaManifest",
    "SchemaObject",
    "digest_of",
    "expected_manifest",
    "manifest_of",
    "normalized_sql",
    "refuses_every_delete",
]
