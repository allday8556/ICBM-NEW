from pathlib import Path

from alembic import context

from app import __version__
from app.config import AppConfig
from app.core.ownership import DataDirLease, acquire_data_dir
from app.db.database import create_sqlite_engine
from app.db.metadata import metadata
from app.db.migrate import OWNERSHIP_ATTRIBUTE

config = context.config
database_url = config.get_main_option("sqlalchemy.url") or AppConfig.from_env().database_url


def _data_dir() -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    database = database_url.removeprefix("sqlite:///")
    if database in ("", ":memory:"):
        return None
    return Path(database).parent


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Migrations mutate the schema, so they run only under data-directory ownership (ADR-0006):
    # `icbm db upgrade` hands in its lease; any other caller (e.g. the alembic CLI) acquires one.
    lease: DataDirLease | None = config.attributes.get(OWNERSHIP_ATTRIBUTE)
    data_dir = _data_dir()
    acquired_here = lease is None and data_dir is not None
    if acquired_here and data_dir is not None:
        lease = acquire_data_dir(data_dir, app_version=__version__)
    try:
        # Same engine factory as the application, so a fresh database is created in WAL mode.
        engine = create_sqlite_engine(database_url)
        try:
            with engine.connect() as connection:
                context.configure(
                    connection=connection,
                    target_metadata=metadata,
                    render_as_batch=True,
                    compare_type=True,
                )
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            engine.dispose()
    finally:
        if acquired_here and lease is not None:
            lease.release()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
