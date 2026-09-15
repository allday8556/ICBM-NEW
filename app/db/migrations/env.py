from alembic import context

from app import __version__
from app.config import AppConfig
from app.core.ownership import DataDirLease, acquire_database_dir, require_ownership
from app.db.database import create_sqlite_engine, sqlite_database_dir
from app.db.metadata import metadata
from app.db.migrate import OWNERSHIP_ATTRIBUTE

config = context.config
database_url = config.get_main_option("sqlalchemy.url") or AppConfig.from_env().database_url


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
    # Migrations mutate the schema, so they run only while the migrated database's own directory
    # is owned (ADR-0006). `icbm db upgrade` hands in its lease, which is checked and never
    # replaced by another lock; any other caller (e.g. the alembic CLI) acquires one here.
    target = sqlite_database_dir(database_url)
    supplied: DataDirLease | None = config.attributes.get(OWNERSHIP_ATTRIBUTE)
    lease = supplied or acquire_database_dir(target, app_version=__version__)
    try:
        require_ownership(lease, target)
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
        if supplied is None:
            lease.release()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
