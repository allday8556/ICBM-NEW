from pathlib import Path

from alembic import context

from app.config import AppConfig
from app.db.database import create_sqlite_engine
from app.db.metadata import metadata

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
    if database_url.startswith("sqlite:///"):
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
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


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
