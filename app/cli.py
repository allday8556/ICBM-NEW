"""``icbm`` command line: serve the application, manage the database schema, and record a reviewed
visual acceptance (ADR-0018 §9) — the only path by which one is ever recorded.

Data-directory ownership (ADR-0006) is the default: every command acquires the exclusive
data-directory lock before it does anything, unless it is listed in ``READ_ONLY_COMMANDS``.
A new command therefore owns the directory unless someone deliberately classifies it as
read-only here and in ADR-0006 (a repository test keeps the two lists equal).
"""

import argparse
import json
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from app import __version__
from app.config import AppConfig, ConfigError
from app.core.logging import configure_logging
from app.core.ownership import (
    DataDirInUseError,
    DataDirLease,
    DataDirOwnershipError,
    acquire_data_dir,
)

Command = tuple[str, ...]

EXIT_CONFIG_ERROR = 2
EXIT_DATA_DIR_IN_USE = 3
EXIT_OWNERSHIP_UNAVAILABLE = 4

_IN_USE_HINTS: dict[Command, str] = {
    ("serve",): (
        "Another ICBM server already owns this data directory. Stop it, or set ICBM_DATA_DIR "
        "to a different directory."
    ),
    ("db", "upgrade"): (
        "Stop the ICBM server using this data directory, then retry the database upgrade."
    ),
    ("live", "record-visual-acceptance"): (
        "Stop the ICBM server using this data directory, then retry the recording."
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="icbm", description="ICBM-NEW local application")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the loopback web application")
    serve.add_argument("--port", type=int, help="override ICBM_PORT")
    db = commands.add_parser("db", help="database schema commands")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("upgrade", help="apply Alembic migrations up to head")
    db_commands.add_parser("current", help="print the applied schema revision (read-only)")
    live = commands.add_parser("live", help="Gate 3 pre-LIVE records (ADR-0018)")
    live_commands = live.add_subparsers(dest="live_command", required=True)
    visual = live_commands.add_parser(
        "record-visual-acceptance",
        help="record a reviewed populated visual acceptance report (ADR-0018 §9)",
    )
    visual.add_argument("--report", required=True, type=Path, help="the harness report JSON")
    visual.add_argument("--approved-by", required=True, help="the reviewer who accepted it")
    visual.add_argument(
        "--authorization-ref", required=True, help="the GitHub comment id that accepted it"
    )
    visual.add_argument("--actor", required=True, help="who runs this command")
    return parser


def _command_of(args: argparse.Namespace) -> Command:
    if args.command == "db":
        return ("db", args.db_command)
    if args.command == "live":
        return ("live", args.live_command)
    return (args.command,)


def _serve(config: AppConfig, lease: DataDirLease, args: argparse.Namespace) -> int:
    import uvicorn

    from app.main import create_app

    uvicorn.run(
        create_app(config, ownership=lease),
        host=config.host,
        port=config.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )
    return 0


def _db_upgrade(config: AppConfig, lease: DataDirLease, args: argparse.Namespace) -> int:
    from app.db.migrate import head_revision, upgrade_to_head

    configure_logging(config.log_level, config.log_dir)
    upgrade_to_head(config.database_url, ownership=lease)
    print(f"database at head {head_revision()}: {config.database_path}")
    return 0


def _checkout_head() -> str | None:
    """The commit the application's checkout is at, or ``None`` when it cannot be read."""
    from app.core.code_identity import REPOSITORY_ROOT

    try:
        found = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout.strip() or None


def _record_visual_acceptance(
    config: AppConfig, lease: DataDirLease, args: argparse.Namespace
) -> int:
    """Record one reviewed visual acceptance report, or refuse and record nothing."""
    from app.container import build_container
    from app.core.errors import AppError

    configure_logging(config.log_level, config.log_dir)
    try:
        report = json.loads(args.report.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        print(f"VISUAL_REPORT_UNREADABLE: {exc}", file=sys.stderr)
        return 1
    container = build_container(config, ownership=lease)
    try:
        acceptance_id = container.visual_acceptance.record(
            report,
            checkout_head=_checkout_head(),
            approved_by=args.approved_by,
            authorization_ref=args.authorization_ref,
            actor=args.actor,
            correlation_id=f"visual-acceptance-{uuid.uuid4()}",
        )
    except AppError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        for problem in (exc.details or {}).get("problems", ()):
            print(f"  {problem}", file=sys.stderr)
        return 1
    finally:
        container.db.dispose()
    print(f"visual acceptance recorded: {acceptance_id} ({report['code_sha']})")
    return 0


def _db_current(config: AppConfig) -> int:
    from app.db.migrate import read_only_revision

    if not config.database_path.is_file():
        print("<no database>")
        return 0
    print(read_only_revision(config.database_path) or "<no schema>")
    return 0


# Commands that mutate canonical state, schema, durable jobs or application-owned files.
OWNING_COMMANDS: dict[Command, Callable[[AppConfig, DataDirLease, argparse.Namespace], int]] = {
    ("serve",): _serve,
    ("db", "upgrade"): _db_upgrade,
    ("live", "record-visual-acceptance"): _record_visual_acceptance,
}
# The only commands allowed to run without the data-directory lock (ADR-0006).
READ_ONLY_COMMANDS: dict[Command, Callable[[AppConfig], int]] = {
    ("db", "current"): _db_current,
}


def _report(error: DataDirOwnershipError, hint: str | None) -> None:
    lines = [f"{error.reason_code}: {error}"]
    if error.holder:
        facts = ", ".join(
            f"{key}={error.holder[key]}"
            for key in ("pid", "started_at", "hostname", "app_version")
            if key in error.holder
        )
        lines.append(f"  current owner (diagnostic only): {facts}")
    if hint:
        lines.append(f"  {hint}")
    print("\n".join(lines), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = _command_of(args)
    try:
        overrides = {"port": args.port} if getattr(args, "port", None) else {}
        config = AppConfig.from_env(**overrides)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    read_only = READ_ONLY_COMMANDS.get(command)
    if read_only is not None:
        return read_only(config)

    try:
        lease = acquire_data_dir(config.data_dir, app_version=__version__)
    except DataDirInUseError as exc:
        _report(exc, _IN_USE_HINTS.get(command))
        return EXIT_DATA_DIR_IN_USE
    except DataDirOwnershipError as exc:
        _report(exc, None)
        return EXIT_OWNERSHIP_UNAVAILABLE
    with lease:
        return OWNING_COMMANDS[command](config, lease, args)
