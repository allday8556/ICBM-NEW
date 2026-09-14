import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from app.db.database import create_sqlite_engine
from app.db.metadata import metadata
from app.db.migrate import alembic_config, current_revision, head_revision, upgrade_to_head

pytestmark = pytest.mark.integration

CANONICAL_TABLES = (
    "jobs",
    "job_attempts",
    "audit_events",
    "supplier_connections",
    "marketplace_capabilities",
    "marketplace_workflow_overlays",
    "marketplace_permission_attestations",
)


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_fresh_database_is_created_at_head_in_wal_mode(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    upgrade_to_head(_url(database))
    engine = create_sqlite_engine(_url(database))
    try:
        assert current_revision(engine) == head_revision() == "0004_m2_permission_attestations"
        tables = set(inspect(engine).get_table_names())
        assert tables == {"alembic_version", *CANONICAL_TABLES}
    finally:
        engine.dispose()
    with sqlite3.connect(database) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        for table in CANONICAL_TABLES:
            assert raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_orm_models_match_the_migrations(migrated_template: Path) -> None:
    engine = create_sqlite_engine(_url(migrated_template))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, metadata) == []
    finally:
        engine.dispose()


def test_audit_events_reject_update_and_delete(data_dir: Path) -> None:
    with sqlite3.connect(data_dir / "icbm.db") as raw:
        raw.execute(
            "INSERT INTO audit_events (event_id, occurred_at, correlation_id, actor, event_type, "
            "action, outcome, details_json) VALUES ('e1', '2026-09-13 00:00:00', 'cid', "
            "'test', 'PROTECTED_ACTION', 'X', 'DENIED', '{}')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE audit_events SET outcome = 'ALLOWED'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM audit_events")
        assert raw.execute("SELECT outcome FROM audit_events").fetchall() == [("DENIED",)]


def test_downgrade_and_upgrade_round_trip(tmp_path: Path) -> None:
    url = _url(tmp_path / "icbm.db")
    upgrade_to_head(url)
    command.downgrade(alembic_config(url), "base")
    command.upgrade(alembic_config(url), "head")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()
