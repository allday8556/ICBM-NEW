"""Programmatic Alembic entry points used by ``icbm db`` and by readiness."""

import contextlib
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from app.core.ownership import DataDirLease

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
# Alembic config attribute through which an owning command hands its lease to env.py.
OWNERSHIP_ATTRIBUTE = "icbm_ownership"


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade_to_head(database_url: str, *, ownership: DataDirLease | None = None) -> None:
    """Migrate to head. Without ``ownership`` env.py acquires the data-directory lock itself."""
    cfg = alembic_config(database_url)
    if ownership is not None:
        cfg.attributes[OWNERSHIP_ATTRIBUTE] = ownership
    command.upgrade(cfg, "head")


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def read_only_revision(database_path: Path) -> str | None:
    """Applied revision through a read-only SQLite handle (ADR-0006 read-only command).

    Opens with ``mode=ro``: no pragma, no schema change, and a missing file is never created.
    """
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as connection:
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None
    return None if row is None else str(row[0])
