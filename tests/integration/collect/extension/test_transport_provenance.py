"""COLLECT transport provenance (ADR-0019 §4, AC-08; rulings 5906290729 B-6 and 5906712259 N-2):
migration 0033, what the run and the revision record, and that provenance is never identity."""

import contextlib
import sqlite3
from pathlib import Path

import pytest
from alembic import command

from app.config import AppConfig
from app.platform.db.migrate import alembic_config, upgrade_to_head
from app.stages.collect.models import TransportKind
from app.stages.collect.runs import DIRECT_URL, RunProvenance
from automation.acceptance.m3.rehearsal.fake_shop import (
    PRODUCT_URL,
    SUPPLIER_KEY,
    collected_for_run,
)
from tests.support.extension_support import REPO_ROOT
from tests.support.jobs_support import FakeClock
from tests.support.shadow_support import collect_once, container, gateway, rows

pytestmark = pytest.mark.integration

BEFORE = "0032_m5_registration_authoring_revisions"
MIGRATION = "0033_collect_transport_provenance"
TABLES = ("collection_runs", "product_facts_revisions")
COLUMNS = ("transport_kind", "capture_policy_revision", "capture_policy_digest")
AT = "'2026-09-30 00:00:00'"
DIGEST = "a" * 64
EXTENSION = RunProvenance(TransportKind.EXTENSION, "fakeshop-capture-1", DIGEST)


def _url(database: Path) -> str:
    return f"sqlite:///{database.as_posix()}"


def _triggers(database: Path) -> dict[str, str]:
    with contextlib.closing(sqlite3.connect(database)) as raw:
        found = raw.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
        return dict(found.fetchall())


def _old_rows(database: Path) -> None:
    """One run and one revision, written on the schema before the provenance columns existed."""
    with contextlib.closing(sqlite3.connect(database)) as raw:
        raw.execute(
            "INSERT INTO collection_runs (collection_run_id, job_id, correlation_id, supplier_key,"
            " source_url, outcome, requested_at) VALUES ('run-old', 'job-old', 'cid', 'fakeshop',"
            f" 'https://shop.collect.invalid/product/sample/1/', 'PENDING', {AT})"
        )
        raw.execute(
            "INSERT INTO product_facts_revisions VALUES ('rev-old', 'fakeshop', '1', 1,"
            f" 'https://shop.collect.invalid/product/sample/1/', {AT}, {AT}, 'KRW', 'r1',"
            f" '{'b' * 64}', '{'c' * 64}', 'run-old', 'cid', 'CONFIRMED')"
        )
        raw.commit()


# ---------------------------------------------------------------- migration 0033


def test_0033_adds_nullable_columns_and_rewrites_nothing(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    command.upgrade(alembic_config(_url(database)), BEFORE)
    _old_rows(database)
    triggers = _triggers(database)
    upgrade_to_head(_url(database))
    with contextlib.closing(sqlite3.connect(database)) as raw:
        for table in TABLES:
            columns = {row[1]: row for row in raw.execute(f"PRAGMA table_info({table})")}
            for column in COLUMNS:
                assert column in columns and columns[column][3] == 0, (table, column)  # nullable
            # No backfill: a row that predates the columns states no transport.
            assert raw.execute(f"SELECT {', '.join(COLUMNS)} FROM {table}").fetchall() == [
                (None, None, None)
            ], table
    # Additive only: every trigger that existed is still there, unchanged, and none was added.
    assert _triggers(database) == triggers


def test_0033_keeps_the_append_only_triggers_working(tmp_path: Path) -> None:
    # Ruling N-2: the columns are added with ADD COLUMN, never by rebuilding the table, so the
    # UPDATE/DELETE protection of the revision is still enforced after the upgrade.
    database = tmp_path / "icbm.db"
    command.upgrade(alembic_config(_url(database)), BEFORE)
    _old_rows(database)
    upgrade_to_head(_url(database))
    triggers = _triggers(database)
    assert {"trg_product_facts_revisions_no_update", "trg_product_facts_revisions_no_delete"} <= (
        set(triggers)
    )
    with contextlib.closing(sqlite3.connect(database)) as raw:
        for statement in (
            "UPDATE product_facts_revisions SET transport_kind = 'EXTENSION'",
            "UPDATE product_facts_revisions SET facts_status = 'REVIEW_REQUIRED'",
            "DELETE FROM product_facts_revisions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(statement)
        assert raw.execute("SELECT COUNT(*) FROM product_facts_revisions").fetchone() == (1,)
        # The shadow and capture freeze triggers of the run are still enforced too.
        for name in ("trg_collection_runs_shadow_frozen", "trg_collection_runs_capture_frozen"):
            assert name in triggers


def test_0033_uses_add_column_only() -> None:
    source = (
        REPO_ROOT / "app" / "platform" / "db" / "migrations" / "versions" / f"{MIGRATION}.py"
    ).read_text("utf-8")
    code = source.split('"""', 2)[2]
    assert "op.add_column(" in code
    for forbidden in (
        "batch_alter_table",
        "create_table",
        "drop_table",
        "CREATE TRIGGER",
        "UPDATE ",
    ):
        assert forbidden not in code, forbidden


def test_the_columns_carry_only_their_own_checks(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    upgrade_to_head(_url(database))
    run = (
        "INSERT INTO collection_runs (collection_run_id, job_id, correlation_id, supplier_key,"
        " source_url, outcome, requested_at, transport_kind, capture_policy_revision,"
        " capture_policy_digest) VALUES ('{id}', 'j', 'c', 'fakeshop',"
        " 'https://shop.collect.invalid/product/sample/1/', 'PENDING', " + AT + ", {values})"
    )
    with contextlib.closing(sqlite3.connect(database)) as raw:
        raw.execute(run.format(id="a", values="NULL, NULL, NULL"))
        raw.execute(run.format(id="b", values="'DIRECT_URL', NULL, NULL"))
        raw.execute(run.format(id="c", values=f"'EXTENSION', 'rev-1', '{DIGEST}'"))
        for bad, constraint in (
            ("'HTTP', NULL, NULL", "transport_kind_valid"),
            ("'BROWSER', NULL, NULL", "transport_kind_valid"),
            ("'extension', NULL, NULL", "transport_kind_valid"),
            ("'EXTENSION', 'rev-1', 'short'", "capture_policy_digest_hex"),
            (f"'EXTENSION', 'rev-1', '{'A' * 64}'", "capture_policy_digest_hex"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match=constraint):
                raw.execute(run.format(id="x", values=bad))


def test_the_downgrade_never_destroys_extension_provenance(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = _url(database)
    command.upgrade(alembic_config(url), BEFORE)
    with contextlib.closing(sqlite3.connect(database)) as raw:
        before = dict(raw.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
    upgrade_to_head(url)
    insert = (
        "INSERT INTO collection_runs (collection_run_id, job_id, correlation_id, supplier_key,"
        " source_url, outcome, requested_at, transport_kind, capture_policy_revision,"
        " capture_policy_digest) VALUES ('{id}', 'j', 'c', 'fakeshop',"
        " 'https://shop.collect.invalid/product/sample/1/', 'PENDING', " + AT + ", {values})"
    )
    with contextlib.closing(sqlite3.connect(database)) as raw:
        raw.execute(insert.format(id="direct", values="'DIRECT_URL', NULL, NULL"))
        raw.execute(insert.format(id="captured", values=f"'EXTENSION', 'rev-1', '{DIGEST}'"))
        raw.commit()
    # An extension capture's provenance is evidence: the step down refuses while one exists.
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), BEFORE)
    with contextlib.closing(sqlite3.connect(database)) as raw:
        raw.execute("DELETE FROM collection_runs WHERE collection_run_id = 'captured'")
        raw.commit()
    # A direct-URL run states only the transport that existed before; the step down keeps the row
    # and restores exactly the earlier schema.
    command.downgrade(alembic_config(url), BEFORE)
    with contextlib.closing(sqlite3.connect(database)) as raw:
        assert dict(raw.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")) == (
            before
        )
        assert raw.execute("SELECT collection_run_id FROM collection_runs").fetchall() == [
            ("direct",)
        ]


# ---------------------------------------------------------------- what is recorded


def test_the_shape_of_provenance_is_enforced_by_its_only_writer() -> None:
    assert RunProvenance(TransportKind.DIRECT_URL) == DIRECT_URL
    assert EXTENSION.capture_policy_digest == DIGEST
    for kind, revision, digest in (
        # A capture policy is named if and only if the transport is EXTENSION.
        (TransportKind.EXTENSION, None, None),
        (TransportKind.EXTENSION, "rev-1", None),
        (TransportKind.EXTENSION, None, DIGEST),
        (TransportKind.EXTENSION, "", DIGEST),
        (TransportKind.EXTENSION, "rev-1", "A" * 64),
        (TransportKind.EXTENSION, "rev-1", "a" * 63),
        (TransportKind.DIRECT_URL, "rev-1", None),
        (TransportKind.DIRECT_URL, None, DIGEST),
        (TransportKind.DIRECT_URL, "rev-1", DIGEST),
    ):
        with pytest.raises(ValueError):
            RunProvenance(kind, revision, digest)
    assert {kind.value for kind in TransportKind} == {"EXTENSION", "DIRECT_URL"}


def test_a_direct_url_run_and_its_revision_record_direct_url(
    config: AppConfig, clock: FakeClock
) -> None:
    with container(config, clock, gateway()) as app:
        run_id = collect_once(app, clock, PRODUCT_URL)
        run = app.collection.run(run_id)
        assert run.provenance == DIRECT_URL
        assert run.revision_id is not None
        revision = app.revisions.get(run.revision_id)
        assert revision is not None
        assert (
            revision.transport_kind,
            revision.capture_policy_revision,
            revision.capture_policy_digest,
        ) == ("DIRECT_URL", None, None)
    assert rows(config, "SELECT transport_kind FROM collection_runs") == [("DIRECT_URL",)]
    assert rows(config, "SELECT transport_kind FROM product_facts_revisions") == [("DIRECT_URL",)]


def test_provenance_enters_no_digest_and_no_fingerprint(
    config: AppConfig, clock: FakeClock
) -> None:
    # AC-08: the same facts under two transports are the same facts. Two runs, identical
    # CollectedFacts, different provenance: every evidence digest, field fingerprint and the
    # source fingerprint are equal, and only the provenance columns differ.
    with container(config, clock, gateway()) as app:
        stored = []
        for index, provenance in enumerate((DIRECT_URL, EXTENSION)):
            with app.db.write() as session:
                run_id = app.extension_capture._runs.open(
                    session,
                    job_id=f"job-{index}",
                    correlation_id="cid",
                    supplier_key=SUPPLIER_KEY,
                    source_url=PRODUCT_URL,
                    provenance=provenance,
                )
            stored.append(app.revisions.append(collected_for_run(run_id), provenance=provenance))
        direct, captured = stored
        assert (direct.transport_kind, captured.transport_kind) == ("DIRECT_URL", "EXTENSION")
        assert (captured.capture_policy_revision, captured.capture_policy_digest) == (
            "fakeshop-capture-1",
            DIGEST,
        )
        assert direct.source_fingerprint == captured.source_fingerprint
        assert direct.facts_status == captured.facts_status
        for key, field in direct.fields.items():
            other = captured.fields[key]
            assert field.fingerprint == other.fingerprint, key
            assert [e.digest for e in field.evidence] == [e.digest for e in other.evidence], key
        assert direct.fingerprints_intact() and captured.fingerprints_intact()


def test_a_run_that_predates_the_columns_states_no_transport(
    config: AppConfig, clock: FakeClock
) -> None:
    with container(config, clock, gateway()) as app:
        with app.db.write() as session:
            run_id = app.extension_capture._runs.open(
                session,
                job_id="job-old",
                correlation_id="cid",
                supplier_key=SUPPLIER_KEY,
                source_url=PRODUCT_URL,
            )
        with contextlib.closing(
            sqlite3.connect(Path(config.data_dir) / "runtime" / "icbm.db")
        ) as raw:
            raw.execute("UPDATE collection_runs SET transport_kind = NULL")
            raw.commit()
        assert app.collection.run(run_id).provenance is None
