"""``icbm`` command line: serve the application and manage the database schema."""

import argparse
import sys
from collections.abc import Sequence

from app.config import AppConfig, ConfigError
from app.core.logging import configure_logging


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="icbm", description="ICBM-NEW local application")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the loopback web application")
    serve.add_argument("--port", type=int, help="override ICBM_PORT")
    db = commands.add_parser("db", help="database schema commands")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("upgrade", help="apply Alembic migrations up to head")
    db_commands.add_parser("current", help="print the applied schema revision")
    args = parser.parse_args(argv)

    try:
        overrides = {"port": args.port} if getattr(args, "port", None) else {}
        config = AppConfig.from_env(**overrides)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "serve":
        return _serve(config)
    if args.db_command == "upgrade":
        return _db_upgrade(config)
    return _db_current(config)


def _serve(config: AppConfig) -> int:
    import uvicorn

    from app.main import create_app

    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )
    return 0


def _db_upgrade(config: AppConfig) -> int:
    from app.db.migrate import head_revision, upgrade_to_head

    configure_logging(config.log_level, config.log_dir)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(config.database_url)
    print(f"database at head {head_revision()}: {config.database_path}")
    return 0


def _db_current(config: AppConfig) -> int:
    from app.db.database import create_sqlite_engine
    from app.db.migrate import current_revision

    engine = create_sqlite_engine(config.database_url)
    try:
        print(current_revision(engine) or "<no schema>")
    finally:
        engine.dispose()
    return 0
