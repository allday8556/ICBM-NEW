import contextlib
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from app.config import database_path
from app.core.errors import ErrorClass
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
    "marketplace_connections",
    "product_facts_revisions",
    "product_facts_fields",
    "product_facts_evidence",
    "source_assets",
    "product_facts_image_refs",
    "collection_runs",
    # M4 PR-B (ADR-0013): the canonical product foundation.
    "source_products",
    "current_source_revision_moves",
    "product_groups",
    "group_members",
    "group_membership_revisions",
    "group_change_events",
    "listing_compositions",
    "product_items",
    "source_bindings",
    # M4 PR-D (ADR-0013 §7): immutable pricing snapshots and their current-pointer history.
    "pricing_snapshots",
    "current_pricing_snapshot_moves",
    # M4 PR-E (ADR-0013 §9): derived image lineage, operator image selection, exact-binary QA.
    "derived_image_artifacts",
    "derived_image_derivations",
    "derived_image_derivation_inputs",
    "derived_image_derivation_roots",
    "image_selection_revisions",
    "image_selection_source_decisions",
    "image_selection_outputs",
    "current_image_selection_moves",
    "image_qa_results",
    # M4 PR-Q (ruling 5738760913): immutable product-level quantity offers.
    "quantity_offers",
    # M5 PR-B: the canonical seller and marketplace account (ACCOUNT_IDENTITY §2).
    "seller_entities",
    "marketplace_accounts",
    # M5 PR-B (ADR-0014): the registration foundation.
    "registration_drafts",
    "registration_draft_items",
    "registration_snapshots",
    "registration_item_snapshots",
    "registration_batches",
    "registration_intents",
    "registration_attempts",
    "marketplace_registrations",
    "marketplace_registration_items",
    "duplicate_overrides",
    # M5 PR-E (ADR-0014 26): the REGISTER execution-scope send brake and its resume boundary.
    "registration_execution_scopes",
    # M5 PR-F (ADR-0014 27, decision 5751540323): the operator-authored preparation, revisioned
    # and append-only, and the provenance link proving which revision froze a Snapshot.
    "registration_preparations",
    "registration_preparation_revisions",
    "registration_preparation_items",
    "registration_snapshot_preparations",
    # Gate 1 G1-A (ADR-0015 §2, authorization 5785935712): the durable registration target policy,
    # its append-only revisions and its one current revision.
    "registration_target_policies",
    "registration_target_policy_revisions",
    "registration_target_policy_current",
    # Gate 1 G1-B (ADR-0015 §3, authorization 5788082735): the durable operator-reviewed category
    # metadata, its append-only revisions and its one current revision.
    "registration_category_metadata",
    "registration_category_metadata_revisions",
    "registration_category_metadata_current",
)
HEAD = "0020_g1_registration_category_metadata"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_fresh_database_is_created_at_head_in_wal_mode(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    upgrade_to_head(_url(database))
    engine = create_sqlite_engine(_url(database))
    try:
        assert current_revision(engine) == head_revision() == HEAD
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
    with sqlite3.connect(database_path(data_dir)) as raw:
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


BEFORE_0005 = "0004_m2_permission_attestations"
AT_0005 = "0005_error_class_taxonomy"
CAPABILITY_ROW = (
    "INSERT INTO marketplace_capabilities VALUES ('{key}', 'NOT_BOUND', NULL, 'MISSING',"
    " 'OPERATOR_ATTESTED', 'BLOCKED', 'UNRECORDED', NULL, {error_class}, NULL, NULL,"
    " '2026-09-14 00:00:00', '2026-09-14 00:00:00')"
)
OVERLAY_ROW = (
    "INSERT INTO marketplace_workflow_overlays VALUES ('{key}', 'PRODUCT_REGISTRATION',"
    " 'PAUSED', 'SCOPE_INSUFFICIENT', NULL, '2026-09-14 00:00:00')"
)


def _capability(key: str, error_class: str | None) -> str:
    return CAPABILITY_ROW.format(
        key=key, error_class="NULL" if error_class is None else f"'{error_class}'"
    )


def _enforcing(database: Path) -> sqlite3.Connection:
    """A raw connection that enforces foreign keys, like every application connection."""
    raw = sqlite3.connect(database)
    raw.execute("PRAGMA foreign_keys=ON")
    return raw


def _rows(raw: sqlite3.Connection) -> tuple[list[tuple[object, ...]], ...]:
    return tuple(
        raw.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in ("marketplace_capabilities", "marketplace_workflow_overlays")
    )


def test_0005_widens_the_error_class_check_and_keeps_every_row(tmp_path: Path) -> None:
    # ADR-0008 Stage 1: an existing database with a capability row and an overlay that references
    # it is upgraded with foreign keys enforced; nothing stored changes.
    database = tmp_path / "icbm.db"
    command.upgrade(alembic_config(_url(database)), BEFORE_0005)
    with contextlib.closing(_enforcing(database)) as raw:
        raw.execute(_capability("smartstore", "AUTH"))
        raw.execute(OVERLAY_ROW.format(key="smartstore"))
        raw.commit()
        before = _rows(raw)
        with pytest.raises(sqlite3.IntegrityError):  # the 0004 schema still has the v1 list
            raw.execute(_capability("x", "FATAL"))
    upgrade_to_head(_url(database))
    with contextlib.closing(_enforcing(database)) as raw:
        assert _rows(raw) == before
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        # The migrated CHECK accepts exactly the enum: every member, and nothing else.
        for index, member in enumerate(ErrorClass):
            raw.execute(_capability(f"k{index}", member.value))
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(_capability("bad", "GW.AUTHN"))
        # The overlay's foreign key to the rebuilt table is still enforced.
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(OVERLAY_ROW.format(key="no-such-marketplace"))


def test_0005_downgrade_never_rewrites_a_stored_class(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = _url(database)
    upgrade_to_head(url)
    with contextlib.closing(_enforcing(database)) as raw:
        raw.execute(_capability("smartstore", "FATAL"))
        raw.execute(OVERLAY_ROW.format(key="smartstore"))
        raw.commit()
    command.downgrade(alembic_config(url), AT_0005)  # later revisions step down first
    with pytest.raises(RuntimeError, match="never rewritten"):
        command.downgrade(alembic_config(url), BEFORE_0005)
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == AT_0005
    finally:
        engine.dispose()
    with contextlib.closing(_enforcing(database)) as raw:
        assert raw.execute("SELECT error_class FROM marketplace_capabilities").fetchall() == [
            ("FATAL",)
        ]
        raw.execute("UPDATE marketplace_capabilities SET error_class = 'AUTH'")
        raw.commit()
    command.downgrade(alembic_config(url), BEFORE_0005)  # nothing holds an added class now
    with contextlib.closing(_enforcing(database)) as raw:
        assert raw.execute("SELECT error_class FROM marketplace_capabilities").fetchall() == [
            ("AUTH",)
        ]
        assert len(raw.execute("SELECT * FROM marketplace_workflow_overlays").fetchall()) == 1
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(_capability("x", "FATAL"))


_AT = "'2026-09-15 00:00:00'"
CONNECTION_ROWS = {
    "initialised": f"('smartstore', 0, 0, NULL, NULL, NULL, NULL, NULL, NULL, {_AT}, {_AT})",
    "generations": f"('smartstore', 2, 5, NULL, NULL, NULL, NULL, NULL, NULL, {_AT}, {_AT})",
    "bound": f"('smartstore', 1, 1, 'uid-x', 'id-x', 1, 1, {_AT}, 'op', {_AT}, {_AT})",
}


@pytest.mark.parametrize("row", CONNECTION_ROWS.values(), ids=CONNECTION_ROWS.keys())
def test_0006_downgrade_never_drops_durable_connection_state(tmp_path: Path, row: str) -> None:
    # Blocker 3 (review 5200019078): generation high-water marks and the binding are safety
    # state. A populated table refuses the downgrade and keeps revision and data intact.
    database = tmp_path / "icbm.db"
    url = _url(database)
    upgrade_to_head(url)
    with contextlib.closing(_enforcing(database)) as raw:
        raw.execute(f"INSERT INTO marketplace_connections VALUES {row}")
        raw.commit()
        before = raw.execute("SELECT * FROM marketplace_connections").fetchall()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), AT_0005)
    engine = create_sqlite_engine(url)
    try:
        # SQLite DDL is not transactional in Alembic: the later, empty 0007 steps down first, and
        # 0006 then refuses, so revision 0006 and its data stay intact.
        assert current_revision(engine) == "0006_m2_marketplace_connections"
    finally:
        engine.dispose()
    with contextlib.closing(_enforcing(database)) as raw:
        assert raw.execute("SELECT * FROM marketplace_connections").fetchall() == before
        raw.execute("DELETE FROM marketplace_connections")
        raw.commit()
    command.downgrade(alembic_config(url), AT_0005)  # only an empty table is dropped
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == AT_0005
        assert "marketplace_connections" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


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
