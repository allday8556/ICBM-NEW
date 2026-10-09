"""Migration 0057 rebuilds ``live_grants`` while other rows still refer to its grants.

On the operating database (2026-10-09) the first upgrade to 0057 failed at ``DROP TABLE
live_grants``: asset upload attempts named existing grants, and the SQLite driver opened no
transaction before the DDL, so ``defer_foreign_keys`` did not hold. The rebuild now runs inside a
savepoint; this proves an upgrade and a downgrade keep every grant and every referring row.
"""

import sqlite3

import pytest
from alembic import command

from app.container import Container
from app.platform.db.migrate import OWNERSHIP_ATTRIBUTE, alembic_config
from tests.integration.register.test_registration_deletion import (  # noqa: F401 - fixtures
    _grant,
    account,
    registration,
    sources,
    store,
)

pytestmark = pytest.mark.integration

BEFORE = "0056_ai_provider_profile"


def test_the_grants_are_rebuilt_while_rows_refer_to_them(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    grant_id = _grant(container, registration)
    database = container.config.database_path
    with sqlite3.connect(database) as raw:
        raw.execute("PRAGMA foreign_keys = ON")
        # Any referrer will do: the operating database's were asset upload attempts.
        raw.execute(
            "CREATE TABLE grant_referrer_probe ("
            " grant_id VARCHAR(36) NOT NULL REFERENCES live_grants (grant_id))"
        )
        raw.execute("INSERT INTO grant_referrer_probe VALUES (?)", (grant_id,))
    container.db.dispose()
    cfg = alembic_config(container.config.database_url)
    cfg.attributes[OWNERSHIP_ATTRIBUTE] = container.ownership
    command.downgrade(cfg, BEFORE)
    command.upgrade(cfg, "head")
    with sqlite3.connect(database) as raw:
        raw.execute("PRAGMA foreign_keys = ON")
        assert raw.execute(
            "SELECT stage, state FROM live_grants WHERE grant_id = ?", (grant_id,)
        ).fetchone() == ("DELETE", "ACTIVE")
        assert raw.execute("SELECT grant_id FROM grant_referrer_probe").fetchall() == [(grant_id,)]
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        sql = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'live_grants'"
        ).fetchone()[0]
    assert "stage IN ('ASSET', 'CREATE', 'DELETE', 'DISPATCH')" in sql
