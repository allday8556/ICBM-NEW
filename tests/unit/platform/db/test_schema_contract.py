"""The schema contract (ADR-0018 §7, §8): one canonical manifest, whatever order SQLite stored."""

import contextlib
import sqlite3
import threading
import time

import pytest

from app.platform.db import schema_contract
from app.platform.db.schema_contract import SchemaManifest, canonical_table_sql, manifest_of

TABLE = (
    'CREATE TABLE "t" ( id VARCHAR(40) NOT NULL, state VARCHAR(20) NOT NULL, note TEXT,'
    " CONSTRAINT pk_t PRIMARY KEY (id),"
    " CONSTRAINT ck_t_state CHECK (state IN ('A', 'B, C', 'D)')),"
    " CONSTRAINT ck_t_note CHECK (note IS NULL OR length(note) > 0) )"
)
REORDERED = (
    'CREATE TABLE "t" ( id VARCHAR(40) NOT NULL, state VARCHAR(20) NOT NULL, note TEXT,'
    " CONSTRAINT ck_t_note CHECK (note IS NULL OR length(note) > 0),"
    " CONSTRAINT pk_t PRIMARY KEY (id),"
    " CONSTRAINT ck_t_state CHECK (state IN ('A', 'B, C', 'D)')) )"
)


def test_the_order_of_table_constraints_is_canonical_and_nothing_else_is() -> None:
    assert canonical_table_sql(TABLE) == canonical_table_sql(REORDERED)
    # A comma or a parenthesis inside a literal never splits a clause.
    assert "CHECK (state IN ('A', 'B, C', 'D)'))" in canonical_table_sql(TABLE)
    # Columns keep their order, and a changed clause is a changed table.
    swapped = TABLE.replace(
        "id VARCHAR(40) NOT NULL, state VARCHAR(20) NOT NULL",
        "state VARCHAR(20) NOT NULL, id VARCHAR(40) NOT NULL",
    )
    assert canonical_table_sql(swapped) != canonical_table_sql(TABLE)
    weakened = TABLE.replace("length(note) > 0", "1")
    assert canonical_table_sql(weakened) != canonical_table_sql(TABLE)


def test_a_manifest_names_every_missing_changed_and_unexpected_object() -> None:
    expected = manifest_of(
        [
            ("table", "t", "t", TABLE),
            ("index", "ix_t_state", "t", "CREATE INDEX ix_t_state ON t (state)"),
            (
                "trigger",
                "trg_t",
                "t",
                "CREATE TRIGGER trg_t BEFORE DELETE ON t BEGIN SELECT 1; END",
            ),
        ]
    )
    same = manifest_of(
        [
            (
                "trigger",
                "trg_t",
                "t",
                "CREATE TRIGGER trg_t  BEFORE DELETE ON t\n BEGIN SELECT 1; END",
            ),
            ("table", "t", "t", REORDERED),
            ("index", "ix_t_state", "t", "CREATE INDEX ix_t_state ON t (state)"),
        ]
    )
    assert expected.differences(same) == [] and expected.digest == same.digest
    damaged = manifest_of(
        [
            ("table", "t", "t", TABLE),
            (
                "trigger",
                "trg_t",
                "t",
                "CREATE TRIGGER trg_t BEFORE DELETE ON t BEGIN SELECT 2; END",
            ),
            (
                "trigger",
                "trg_x",
                "t",
                "CREATE TRIGGER trg_x BEFORE INSERT ON t BEGIN SELECT 1; END",
            ),
        ]
    )
    assert expected.differences(damaged) == [
        "missing:index:ix_t_state",
        "changed:trigger:trg_t",
        "unexpected:trigger:trg_x",
    ]
    assert expected.digest != damaged.digest
    assert SchemaManifest({}).differences(SchemaManifest({})) == []


def test_concurrent_callers_derive_the_contract_once_and_never_at_the_same_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alembic's migration context is one per process: two derivations running together corrupt
    each other. Callers that race for an uncached contract are serialized, and all but the first
    are answered from the cache."""
    running = threading.Semaphore(1)
    overlaps: list[str] = []
    calls: list[str] = []

    def one_migration_at_a_time(url: str) -> None:
        if not running.acquire(blocking=False):
            overlaps.append(url)
            return
        try:
            calls.append(url)
            time.sleep(0.05)  # long enough for every other caller to arrive
            with contextlib.closing(sqlite3.connect(url.removeprefix("sqlite:///"))) as built:
                built.execute("CREATE TABLE alembic_version (version_num VARCHAR(64))")
                built.execute("INSERT INTO alembic_version VALUES ('head-under-test')")
                built.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
                built.commit()
        finally:
            running.release()

    monkeypatch.setattr(schema_contract, "upgrade_to_head", one_migration_at_a_time)
    schema_contract._derive.cache_clear()
    results: list[object] = []
    barrier = threading.Barrier(6)

    def caller() -> None:
        barrier.wait()
        try:
            results.append(schema_contract.expected_manifest("head-under-test"))
        except BaseException as failed:
            results.append(failed)

    threads = [threading.Thread(target=caller) for _ in range(6)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        schema_contract._derive.cache_clear()
    assert overlaps == [] and len(calls) == 1
    assert len(results) == 6
    assert all(isinstance(result, SchemaManifest) for result in results)
    assert len({id(result) for result in results}) == 1
    assert "table:t" in results[0].objects  # type: ignore[union-attr]


def test_a_failed_derivation_is_not_cached_and_does_not_hold_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(_url: str) -> None:
        raise RuntimeError("the migrations could not run")

    monkeypatch.setattr(schema_contract, "upgrade_to_head", failing)
    schema_contract._derive.cache_clear()
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="could not run"):
                schema_contract.expected_manifest("head-under-test")
        assert schema_contract._DERIVATION.acquire(blocking=False)
        schema_contract._DERIVATION.release()
    finally:
        schema_contract._derive.cache_clear()
