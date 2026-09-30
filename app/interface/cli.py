"""``icbm`` command line: serve the application, manage the database schema, record a reviewed
visual acceptance (ADR-0018 §9) — the only path by which one is ever recorded — and pair, rotate
or revoke the capture extension (ADR-0019 §3), which is the only way a pairing is ever issued.

Data-directory ownership (ADR-0006) is the default: every command acquires the exclusive
data-directory lock before it does anything, unless it is listed in ``READ_ONLY_COMMANDS``.
A new command therefore owns the directory unless someone deliberately classifies it as
read-only here and in ADR-0006 (a repository test keeps the two lists equal).
"""

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from app import __version__
from app.config import AppConfig, ConfigError
from app.platform.core.logging import configure_logging
from app.platform.core.ownership import (
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
    **{
        ("extension", action): (
            "Stop the ICBM server using this data directory, then retry the pairing command."
        )
        for action in ("pair", "rotate", "revoke")
    },
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
    extension = commands.add_parser("extension", help="capture extension pairing (ADR-0019 §3)")
    extension_commands = extension.add_subparsers(dest="extension_command", required=True)
    pair = extension_commands.add_parser(
        "pair", help="pair one extension identity and print its one-time pairing code"
    )
    pair.add_argument(
        "--extension-id", required=True, help="the extension's own identity (32 letters a-p)"
    )
    extension_commands.add_parser(
        "rotate", help="issue a new pairing generation; the prior code stops working"
    )
    extension_commands.add_parser("revoke", help="remove the pairing")
    return parser


def _command_of(args: argparse.Namespace) -> Command:
    if args.command == "db":
        return ("db", args.db_command)
    if args.command == "live":
        return ("live", args.live_command)
    if args.command == "extension":
        return ("extension", args.extension_command)
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
    from app.platform.db.migrate import head_revision, upgrade_to_head

    configure_logging(config.log_level, config.log_dir)
    upgrade_to_head(config.database_url, ownership=lease)
    print(f"database at head {head_revision()}: {config.database_path}")
    return 0


def _record_visual_acceptance(
    config: AppConfig, lease: DataDirLease, args: argparse.Namespace
) -> int:
    """Record one reviewed visual acceptance report, or refuse and record nothing. The commit it
    must match is the one this process was composed at
    (``app.platform.core.code_identity``)."""
    from app.container import build_container
    from app.platform.core.errors import AppError

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


def _extension_pairing(config: AppConfig, lease: DataDirLease, args: argparse.Namespace) -> int:
    """Pair, rotate or revoke the capture extension (ADR-0019 §3; ruling 5906290729 B-2).

    The pairing lives in the OS keyring only. A pairing code is printed once, for the operator to
    paste into the extension; it is never logged and never written to a file here.
    """
    from app.platform.core.clock import SystemClock
    from app.platform.core.errors import AppError
    from app.platform.core.secrets import build_secret_store
    from app.stages.collect.extension.nonces import NonceCache
    from app.stages.collect.extension.pairing import ExtensionPairing

    clock = SystemClock()
    pairing = ExtensionPairing(build_secret_store(config.secret_backend), clock, NonceCache(clock))
    origin = f"http://{config.host}:{config.port}"
    try:
        if args.extension_command == "revoke":
            print("pairing revoked" if pairing.revoke() else "no pairing existed")
            return 0
        issued = (
            pairing.pair(args.extension_id, origin=origin)
            if args.extension_command == "pair"
            else pairing.rotate(origin=origin)
        )
    except AppError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    described = issued.record.describe()
    print(
        f"paired extension {described['extension_id']} "
        f"(pairing {described['pairing_id']}, generation {described['generation']})"
    )
    print("pairing code — paste it into the extension once, and keep it nowhere else:")
    print(issued.code)
    return 0


def _db_current(config: AppConfig) -> int:
    from app.platform.db.migrate import read_only_revision

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
    ("extension", "pair"): _extension_pairing,
    ("extension", "rotate"): _extension_pairing,
    ("extension", "revoke"): _extension_pairing,
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
