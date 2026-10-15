"""``icbm`` command line: serve the application, manage the database schema, record a reviewed
visual acceptance (ADR-0018 §9) — the only path by which one is ever recorded — run the
protected pre-LIVE operator commands of the first canary (Issue #89 architect resolution
``5915900049`` D3), and pair, rotate or revoke the capture extension (ADR-0019 §3), which is the
only way a pairing is ever issued.

**The protected operator commands own no truth.** Each protected action —
``eligibility-packet``, ``record-eligibility``, ``issue-asset-grant``, ``issue-create-grant``,
``issue-delete-grant``, ``release-brake`` and ``engage-brake`` — calls exactly one existing owner
method, of the canary-eligibility owner (ADR-0018 §5.1) or the LIVE authority (§3, §3.5, §4).
``icbm live inspect`` writes nothing and reads two existing read-only projections, the live status
and the canary readiness. Each prints what the owners returned. Every rule, identity and refusal
stays with the owner: a value given here is only an expectation the owner checks, never a fact it
records as given. Nothing here reaches a provider, changes the execution mode or makes a mutation
permitted: a grant and a released brake are two layers of the send-time stack, which still refuses
outside a bounded LIVE window (``M0_DRY_RUN_ONLY``).

**The two local proofs** (ADR-0018 §7, §8) run the same way: ``restore-drill-asset`` and
``restore-drill-create`` call ``RestoreDrillService.drill_asset`` / ``drill_create`` for one
exact ASSET grant or Intent into a fresh restore root the operator names, and
``prove-retention`` calls ``RetentionProofService.prove``. Each owner records its own PASSED or
FAILED proof of the state as it is; nothing here decides a verdict, and a proof proves only
itself — the stack still refuses under ``M0_DRY_RUN_ONLY``.

``icbm coupang save-credentials`` is the no-echo local input path into the OS credential store.
``icbm coupang read-canary`` composes the one C-LIVE-R1 identity-bound KR read and
``icbm coupang read-category-metadata`` reads and immediately stores the exact recommended
category contracts without exposing provider bodies or account values. Category reads consume a
durable pre-transport barrier; ``mark-category-metadata-ambiguous`` records the one known legacy
gap without transport, and ``recover-category-metadata`` performs only one explicitly approved
read for the unique ambiguous category.
``icbm coupang mutate-test-products`` composes the C-LIVE-MUT-1 exact test 1..11 workflow.
``icbm coupang recover-test-product-create`` performs only the positive-only identity lookup for
one ambiguous C-LIVE-MUT-1 CREATE and never treats an empty answer as absence.
None of these commands accepts Access Key or Secret Key as an argument, so secret material does
not enter a process list.

**The residual-risk acceptance** (ADR-0018 §6.1, G3-30) is given by the user and the architect in
GitHub. ``record-residual-risk-acceptance`` is the only path that records its durable proof: it
calls ``ResidualRiskAcceptanceService.record`` with the account, the current risk contract and
the two GitHub comment identities (``github_issue_comment:<id>@<sha256>``). It records a pointer
to that decision, never the decision, and it authorizes nothing: every other layer still decides.

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
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

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
# The protected operator commands (5915900049 D3) all hold the data directory, like the recording.
LIVE_OPERATOR_COMMANDS: tuple[str, ...] = (
    "inspect",
    "eligibility-packet",
    "record-eligibility",
    "issue-asset-grant",
    "issue-create-grant",
    "issue-delete-grant",
    "issue-dispatch-grant",
    "release-brake",
    "engage-brake",
    "restore-drill-asset",
    "restore-drill-create",
    "prove-retention",
    "record-residual-risk-acceptance",
    "upload-assets",
)
COUPANG_OPERATOR_COMMANDS: tuple[str, ...] = (
    "save-credentials",
    "read-canary",
    "read-category-metadata",
    "mark-category-metadata-ambiguous",
    "recover-category-metadata",
    "mutate-test-products",
    "complete-rejected-test-products",
    "recover-test-product-create",
)
for _name in LIVE_OPERATOR_COMMANDS:
    _IN_USE_HINTS[("live", _name)] = (
        "Stop the ICBM server using this data directory, then retry the command."
    )
for _name in COUPANG_OPERATOR_COMMANDS:
    _IN_USE_HINTS[("coupang", _name)] = (
        "Stop the ICBM server using this data directory, then retry the Coupang command."
    )


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
        "--authorization-ref", required=True, help="the GitHub comment id recording the acceptance"
    )
    visual.add_argument("--actor", required=True, help="who runs this command")
    _add_operator_commands(live_commands)
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
    coupang = commands.add_parser("coupang", help="bounded Coupang credential and canary actions")
    coupang_commands = coupang.add_subparsers(dest="coupang_command", required=True)
    coupang_commands.add_parser(
        "save-credentials", help="prompt without echo and save one credential bundle"
    )
    coupang_commands.add_parser(
        "bind-saved-delivery",
        help="bind the one current saved delivery policy to the Coupang account inputs",
    )
    canary = coupang_commands.add_parser(
        "read-canary", help="run the approved one-shot KR read-only canary"
    )
    canary.add_argument(
        "--clock-skew-seconds", required=True, type=float, help="observed absolute clock offset"
    )
    canary.add_argument("--scope-id", required=True, help="the bounded owner-approval reference")
    canary.add_argument("--approved-at", required=True, help="approval time with explicit offset")
    canary.add_argument("--expires-at", required=True, help="expiry time with explicit offset")
    metadata = coupang_commands.add_parser(
        "read-category-metadata",
        help="read each recommended category contract once and save each safe result immediately",
    )
    metadata.add_argument(
        "--clock-skew-seconds", required=True, type=float, help="observed absolute clock offset"
    )
    mark_metadata = coupang_commands.add_parser(
        "mark-category-metadata-ambiguous",
        help="record the known pre-journal first-category ambiguity without provider transport",
    )
    mark_metadata.add_argument("--scope-id", required=True, help="must be C-LIVE-CATEGORY-LEGACY-1")
    recover_metadata = coupang_commands.add_parser(
        "recover-category-metadata",
        help="perform one explicitly approved recovery read for the unique ambiguous category",
    )
    recover_metadata.add_argument(
        "--clock-skew-seconds", required=True, type=float, help="observed absolute clock offset"
    )
    recover_metadata.add_argument(
        "--scope-id", required=True, help="must be C-LIVE-CATEGORY-RECOVERY-1"
    )
    recover_metadata.add_argument(
        "--approved-at", required=True, help="approval time with explicit offset"
    )
    recover_metadata.add_argument(
        "--expires-at", required=True, help="expiry time with explicit offset"
    )
    mutations = coupang_commands.add_parser(
        "mutate-test-products",
        help="run the approved C-LIVE-MUT-1 test 1..11 create/update/read/delete workflow",
    )
    mutations.add_argument(
        "--bundle", required=True, type=Path, help="closed test 1..11 payload bundle JSON"
    )
    mutations.add_argument(
        "--clock-skew-seconds", required=True, type=float, help="observed absolute clock offset"
    )
    mutations.add_argument("--scope-id", required=True, help="must be C-LIVE-MUT-1")
    mutations.add_argument(
        "--payload-digest", required=True, help="reviewed lowercase SHA-256 payload digest"
    )
    mutations.add_argument(
        "--approved-at", required=True, help="approval time with explicit offset"
    )
    mutations.add_argument("--expires-at", required=True, help="expiry time with explicit offset")
    corrected = coupang_commands.add_parser(
        "complete-rejected-test-products",
        help="rebuild corrected test 1..11 payloads in memory and complete rejected tests 2..11",
    )
    corrected.add_argument(
        "--clock-skew-seconds", required=True, type=float, help="observed absolute clock offset"
    )
    corrected.add_argument("--scope-id", required=True, help="must be C-LIVE-MUT-1")
    corrected.add_argument(
        "--payload-digest", required=True, help="reviewed lowercase SHA-256 payload digest"
    )
    corrected.add_argument(
        "--approved-at", required=True, help="approval time with explicit offset"
    )
    corrected.add_argument("--expires-at", required=True, help="expiry time with explicit offset")
    recovery = coupang_commands.add_parser(
        "recover-test-product-create",
        help="recover one ambiguous C-LIVE-MUT-1 CREATE by positive provider evidence only",
    )
    recovery.add_argument(
        "--bundle", required=True, type=Path, help="closed test 1..11 payload bundle JSON"
    )
    recovery.add_argument(
        "--clock-skew-seconds", required=True, type=float, help="observed absolute clock offset"
    )
    recovery.add_argument(
        "--payload-digest", required=True, help="reviewed lowercase SHA-256 payload digest"
    )
    return parser


def _add_operator_commands(live_commands: Any) -> None:
    """The protected operator commands of 5915900049 D3: existing owners only."""
    inspect = live_commands.add_parser(
        "inspect", help="print the brake, every grant and the canary readiness (read-only)"
    )
    inspect.add_argument("--unit-ref", help="the one provider-listing unit a canary would name")
    packet = live_commands.add_parser(
        "eligibility-packet",
        help="print the server-built eligibility review packet (ADR-0018 §5.1)",
    )
    packet.add_argument("--preparation-id", required=True)
    record = live_commands.add_parser(
        "record-eligibility", help="record one eligibility review of the current packet"
    )
    record.add_argument("--preparation-id", required=True)
    record.add_argument(
        "--packet-digest", required=True, help="the digest of the packet that was reviewed"
    )
    record.add_argument(
        "--checks", required=True, type=Path, help="the closed checklist, as a JSON object"
    )
    record.add_argument("--actor", required=True, help="the reviewer who records it")
    accept = live_commands.add_parser(
        "record-residual-risk-acceptance",
        help="record the proof of the user and architect residual-risk acceptance (ADR-0018 §6.1)",
    )
    accept.add_argument("--marketplace-key", required=True)
    accept.add_argument("--account", required=True, help="the canonical marketplace account id")
    accept.add_argument(
        "--risk-contract", required=True, help="the exact residual-risk contract version accepted"
    )
    accept.add_argument(
        "--user-acceptance",
        required=True,
        help="the GitHub comment recording the user's acceptance, written by the agent:"
        " github_issue_comment:<id>@<sha256 of body>",
    )
    accept.add_argument(
        "--architect-acceptance",
        required=True,
        help="the GitHub comment recording the architect's acceptance:"
        " github_issue_comment:<id>@<sha256>",
    )
    accept.add_argument("--actor", required=True, help="the operator who records it")
    asset = live_commands.add_parser(
        "issue-asset-grant", help="issue the exact ASSET-stage grant (ADR-0018 §3.2)"
    )
    asset.add_argument("--marketplace", required=True)
    asset.add_argument("--account", required=True, help="the canonical marketplace account id")
    asset.add_argument("--preparation-revision-id", required=True)
    asset.add_argument("--candidate-fingerprint", required=True)
    asset.add_argument(
        "--artifacts",
        required=True,
        type=Path,
        help="a JSON list of {asset_kind, sha256, derivation_id}",
    )
    asset.add_argument("--asset-profile", required=True)
    asset.add_argument("--budget", required=True, type=int)
    _add_window(asset)
    create = live_commands.add_parser(
        "issue-create-grant", help="issue the exact CREATE-stage grant (ADR-0018 §3.2)"
    )
    create.add_argument("--intent-id", required=True)
    _add_window(create)
    delete = live_commands.add_parser(
        "issue-delete-grant",
        help="issue the exact DELETE grant of one confirmed registration (ADR-0018 §3.5)",
    )
    delete.add_argument("--registration-id", required=True)
    _add_window(delete)
    dispatch = live_commands.add_parser(
        "issue-dispatch-grant",
        help="issue the exact DISPATCH grant of one product order (ADR-0025 §5)",
    )
    dispatch.add_argument("--product-order-id", required=True)
    _add_window(dispatch)
    release = live_commands.add_parser(
        "release-brake", help="release the protected-write brake (ADR-0018 §4.1)"
    )
    release.add_argument("--actor", required=True)
    release.add_argument("--reason-code", required=True)
    release.add_argument(
        "--authorization-ref",
        required=True,
        help="the GitHub comment id recording the user's approval (agent bookkeeping)",
    )
    engage = live_commands.add_parser(
        "engage-brake", help="re-engage the protected-write brake (ADR-0018 §4.1)"
    )
    engage.add_argument("--actor", required=True)
    engage.add_argument("--reason-code", required=True)
    drill_asset = live_commands.add_parser(
        "restore-drill-asset", help="run the ASSET restore drill of one grant (ADR-0018 §7)"
    )
    drill_asset.add_argument("--grant-id", required=True)
    drill_create = live_commands.add_parser(
        "restore-drill-create", help="run the CREATE restore drill of one Intent (ADR-0018 §7)"
    )
    drill_create.add_argument("--intent-id", required=True)
    for drill in (drill_asset, drill_create):
        drill.add_argument(
            "--restore-root",
            required=True,
            type=Path,
            help="a new or empty absolute directory outside the data directory",
        )
        drill.add_argument("--actor", required=True)
    retention = live_commands.add_parser(
        "prove-retention", help="record the evidence-retention proof as it is now (ADR-0018 §8)"
    )
    retention.add_argument("--actor", required=True)
    upload = live_commands.add_parser(
        "upload-assets",
        help=(
            "upload one ASSET grant's artifacts in a bounded LIVE window of this process"
            " (ADR-0018 §4.1 amendment note)"
        ),
    )
    upload.add_argument("--grant-id", required=True)
    upload.add_argument(
        "--window-s",
        required=True,
        type=int,
        help="the LIVE window of this run in seconds, at most 14400; it closes when the run ends",
    )
    upload.add_argument("--actor", required=True)


def _add_window(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--not-before", required=True, help="ISO-8601 with an explicit offset, e.g. ...+09:00"
    )
    command.add_argument("--expires-at", required=True, help="ISO-8601 with an explicit offset")
    command.add_argument("--approved-by", required=True)
    command.add_argument(
        "--authorization-ref",
        required=True,
        help="the GitHub comment id recording the user's approval (agent bookkeeping)",
    )


def _command_of(args: argparse.Namespace) -> Command:
    if args.command == "db":
        return ("db", args.db_command)
    if args.command == "live":
        return ("live", args.live_command)
    if args.command == "extension":
        return ("extension", args.extension_command)
    if args.command == "coupang":
        return ("coupang", args.coupang_command)
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


def _coupang_credentials(config: AppConfig, lease: DataDirLease, args: argparse.Namespace) -> int:
    """Prompt locally with no echo and atomically replace the OS-held credential bundle."""
    from getpass import getpass

    from app.platform.core.secrets import build_secret_store
    from app.stages.connect.coupang.credentials import CoupangCredentialStore
    from integrations.marketplaces.coupang.auth import Credentials

    secrets = build_secret_store(config.secret_backend)
    status = secrets.status()
    if not status.available or status.backend == "memory":
        print("COUPANG_OS_SECRET_STORE_UNAVAILABLE", file=sys.stderr)
        return 1
    try:
        vendor_id = input("Coupang Vendor ID: ")
        generation = int(input("Credential generation (positive integer): "))
        access_key = getpass("Coupang Access Key (hidden): ")
        secret_key = getpass("Coupang Secret Key (hidden): ")
        credentials = Credentials(access_key, secret_key, vendor_id, generation)
        CoupangCredentialStore(secrets).save(credentials)
    except (EOFError, KeyboardInterrupt, OSError, ValueError):
        print("COUPANG_CREDENTIAL_INPUT_REFUSED", file=sys.stderr)
        return 1
    print(f"Coupang credentials stored in the OS credential manager (generation {generation})")
    return 0


def _coupang_bind_saved_delivery(
    config: AppConfig, lease: DataDirLease, args: argparse.Namespace
) -> int:
    """Use one exact current saved courier; never ask for or print the value again."""
    from app.container import build_container
    from app.stages.connect.coupang.credentials import CoupangCredentialStore
    from app.stages.connect.coupang.live_preflight import CoupangLivePreflightStore

    container = build_container(config, ownership=lease)
    try:
        status = container.secrets.status()
        if not status.available or status.backend == "memory":
            print("COUPANG_OS_SECRET_STORE_UNAVAILABLE", file=sys.stderr)
            return 1
        credentials = CoupangCredentialStore(container.secrets).load()
        if credentials is None:
            print("COUPANG_CREDENTIALS_NOT_CONFIGURED", file=sys.stderr)
            return 1
        candidates = [
            policy
            for policy in container.target_policies.accounts("smartstore").accounts
            if policy.current is not None
            and policy.inputs is not None
            and policy.inputs.delivery_policy is not None
            and policy.inputs.delivery_policy.delivery_company is not None
        ]
        if len(candidates) != 1:
            print("COUPANG_SAVED_DELIVERY_SELECTION_AMBIGUOUS", file=sys.stderr)
            return 1
        candidate = candidates[0]
        candidate_revision = candidate.current
        assert candidate_revision is not None
        current = container.target_policies.policy("smartstore", candidate.marketplace_account_id)
        if (
            current.current is None
            or current.inputs is None
            or current.inputs.delivery_policy is None
            or current.inputs.delivery_policy.delivery_company is None
            or current.current.policy_revision != candidate_revision.policy_revision
        ):
            print("COUPANG_SAVED_DELIVERY_REVISION_MOVED", file=sys.stderr)
            return 1
        CoupangLivePreflightStore(container.secrets).bind_legacy_account_values_from_saved_courier(
            credentials=credentials,
            delivery_company_code=current.inputs.delivery_policy.delivery_company,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        container.db.dispose()
    print("Coupang account inputs bound to the current saved delivery policy")
    return 0


def _coupang_read_canary(config: AppConfig, lease: DataDirLease, args: argparse.Namespace) -> int:
    """Run C-LIVE-R1 once; every caller-selectable value is a preflight binding, not a target."""
    from app.platform.core.errors import AppError
    from app.platform.core.secrets import build_secret_store
    from app.stages.connect.coupang.canary_proof import CoupangCanaryProofStore
    from app.stages.connect.coupang.credentials import CoupangCredentialStore
    from integrations.marketplaces.coupang.auth import (
        AuthContext,
        Market,
        SystemUtcClock,
    )
    from integrations.marketplaces.coupang.canary import (
        ClockPreflight,
        CoupangReadCanary,
        LiveTransportRefused,
        ReadOnlyCanaryApproval,
    )

    secrets = build_secret_store(config.secret_backend)
    status = secrets.status()
    if not status.available or status.backend == "memory":
        print("COUPANG_OS_SECRET_STORE_UNAVAILABLE", file=sys.stderr)
        return 1
    try:
        credentials = CoupangCredentialStore(secrets).load()
    except OSError:
        credentials = None
    if credentials is None:
        print("COUPANG_CREDENTIALS_NOT_CONFIGURED", file=sys.stderr)
        return 1
    clock = SystemUtcClock()
    now = clock.now()
    try:
        approval = ReadOnlyCanaryApproval.create(
            scope_id=args.scope_id,
            vendor_id=credentials.vendor_id,
            credential_generation=credentials.credential_generation,
            approved_at=_instant(args.approved_at, "--approved-at"),
            expires_at=_instant(args.expires_at, "--expires-at"),
        )
        result = CoupangReadCanary(
            context=AuthContext(credentials, Market.KR),
            approval=approval,
            clock=clock,
            clock_preflight=ClockPreflight.create(
                args.clock_skew_seconds,
                checked_at=now,
            ),
        ).run()
        CoupangCanaryProofStore(secrets).save_success(credentials=credentials, verified_at=now)
    except (AppError, LiveTransportRefused, ValueError) as exc:
        code = exc.code if isinstance(exc, AppError) else "COUPANG_CANARY_LOCAL_REFUSAL"
        print(code, file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


def _coupang_mutate_test_products(
    config: AppConfig, lease: DataDirLease, args: argparse.Namespace
) -> int:
    """Run C-LIVE-MUT-1 from one reviewed closed bundle; secrets stay in the OS store."""
    from app.platform.core.errors import AppError
    from app.platform.core.ownership import require_ownership
    from app.platform.core.secrets import build_secret_store
    from app.stages.connect.coupang.canary_proof import CoupangCanaryProofStore
    from app.stages.connect.coupang.credentials import CoupangCredentialStore
    from integrations.marketplaces.coupang.auth import (
        AuthContext,
        Market,
        SystemUtcClock,
    )
    from integrations.marketplaces.coupang.canary import ClockPreflight
    from integrations.marketplaces.coupang.live_mutations import (
        CoupangTestProductMutationRun,
        LiveMutationTransportRefused,
        MutationJournal,
        TestProductBundle,
        TestProductMutationApproval,
    )

    require_ownership(lease, config.data_dir)
    secrets = build_secret_store(config.secret_backend)
    status = secrets.status()
    if not status.available or status.backend == "memory":
        print("COUPANG_OS_SECRET_STORE_UNAVAILABLE", file=sys.stderr)
        return 1
    try:
        credentials = CoupangCredentialStore(secrets).load()
    except OSError:
        credentials = None
    if credentials is None:
        print("COUPANG_CREDENTIALS_NOT_CONFIGURED", file=sys.stderr)
        return 1
    try:
        raw = json.loads(args.bundle.read_text("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("bundle must be an object")
        bundle = TestProductBundle.from_mapping(raw)
        if bundle.payload_digest != args.payload_digest:
            raise ValueError("payload digest does not match the reviewed bundle")
        clock = SystemUtcClock()
        now = clock.now()
        proof = CoupangCanaryProofStore(secrets).load_current(credentials=credentials, now=now)
        approval = TestProductMutationApproval.create(
            scope_id=args.scope_id,
            vendor_id=credentials.vendor_id,
            credential_generation=credentials.credential_generation,
            payload_digest=bundle.payload_digest,
            approved_at=_instant(args.approved_at, "--approved-at"),
            expires_at=_instant(args.expires_at, "--expires-at"),
        )
        result = CoupangTestProductMutationRun(
            context=AuthContext(credentials, Market.KR, proof),
            approval=approval,
            bundle=bundle,
            clock=clock,
            clock_preflight=ClockPreflight.create(args.clock_skew_seconds, checked_at=now),
            journal=MutationJournal(config.runtime_dir / "coupang-live-mutations.json"),
        ).run()
    except (AppError, LiveMutationTransportRefused, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, AppError) else "COUPANG_MUTATION_LOCAL_REFUSAL"
        print(code, file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


def _coupang_read_category_metadata(
    config: AppConfig, lease: DataDirLease, args: argparse.Namespace
) -> int:
    """Read exact recommended category contracts once; persist each parsed attempt immediately."""
    from app.platform.core.ownership import require_ownership
    from app.platform.core.secrets import build_secret_store
    from app.stages.connect.coupang.canary_proof import CoupangCanaryProofStore
    from app.stages.connect.coupang.credentials import CoupangCredentialStore
    from app.stages.connect.coupang.live_preflight import CoupangLivePreflightStore
    from integrations.marketplaces.coupang.auth import (
        AuthContext,
        Market,
        SystemUtcClock,
    )
    from integrations.marketplaces.coupang.canary import ClockPreflight
    from integrations.marketplaces.coupang.live_category_metadata import (
        CoupangCategoryMetadataPreflight,
        LiveCategoryMetadataTransportRefused,
    )

    require_ownership(lease, config.data_dir)
    secrets = build_secret_store(config.secret_backend)
    status = secrets.status()
    if not status.available or status.backend == "memory":
        print("COUPANG_OS_SECRET_STORE_UNAVAILABLE", file=sys.stderr)
        return 1
    try:
        credentials = CoupangCredentialStore(secrets).load()
    except OSError:
        credentials = None
    if credentials is None:
        print("COUPANG_CREDENTIALS_NOT_CONFIGURED", file=sys.stderr)
        return 1
    store = CoupangLivePreflightStore(secrets)
    try:
        recommendations = store.category_recommendations()
        clock = SystemUtcClock()
        now = clock.now()
        proof = CoupangCanaryProofStore(secrets).load_current(credentials=credentials, now=now)
        category_codes = store.pending_category_metadata_codes(
            recommendations.unique_category_codes,
            generation=credentials.credential_generation,
        )
        if not category_codes:
            print(
                json.dumps(
                    {"attempted": 0, "failed": 0, "failure_codes": [], "succeeded": 0},
                    sort_keys=True,
                )
            )
            return 0
        owner = CoupangCategoryMetadataPreflight(
            context=AuthContext(credentials, Market.KR, proof),
            category_codes=category_codes,
            clock=clock,
            clock_preflight=ClockPreflight.create(args.clock_skew_seconds, checked_at=now),
        )

        def persist(attempt: Any) -> None:
            store.save_category_metadata_attempt(
                attempt, generation=credentials.credential_generation
            )
            if not attempt.succeeded:
                raise ValueError("COUPANG_CATEGORY_METADATA_PROVIDER_FAILURE")

        result = owner.run(
            on_before_attempt=lambda code: store.begin_category_metadata_attempt(
                code, generation=credentials.credential_generation
            ),
            on_attempt=persist,
        )
    except (LiveCategoryMetadataTransportRefused, OSError, ValueError):
        print("COUPANG_CATEGORY_METADATA_LOCAL_REFUSAL", file=sys.stderr)
        return 1
    failed = [attempt for attempt in result.attempts if not attempt.succeeded]
    summary = {
        "attempted": len(result.attempts),
        "failed": len(failed),
        "failure_codes": [attempt.error_code for attempt in failed],
        "succeeded": len(result.attempts) - len(failed),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if failed else 0


def _coupang_mark_category_metadata_ambiguous(
    config: AppConfig, lease: DataDirLease, args: argparse.Namespace
) -> int:
    """Persist the one known legacy ambiguity without making any provider request."""
    from app.platform.core.ownership import require_ownership
    from app.platform.core.secrets import build_secret_store
    from app.stages.connect.coupang.credentials import CoupangCredentialStore
    from app.stages.connect.coupang.live_preflight import CoupangLivePreflightStore

    require_ownership(lease, config.data_dir)
    if args.scope_id != "C-LIVE-CATEGORY-LEGACY-1":
        print("COUPANG_CATEGORY_METADATA_LEGACY_SCOPE_INVALID", file=sys.stderr)
        return 1
    secrets = build_secret_store(config.secret_backend)
    status = secrets.status()
    if not status.available or status.backend == "memory":
        print("COUPANG_OS_SECRET_STORE_UNAVAILABLE", file=sys.stderr)
        return 1
    try:
        credentials = CoupangCredentialStore(secrets).load()
    except OSError:
        credentials = None
    if credentials is None:
        print("COUPANG_CREDENTIALS_NOT_CONFIGURED", file=sys.stderr)
        return 1
    try:
        store = CoupangLivePreflightStore(secrets)
        first = store.category_recommendations().unique_category_codes[0]
        store.mark_legacy_category_metadata_ambiguous(
            first, generation=credentials.credential_generation
        )
    except (OSError, ValueError):
        print("COUPANG_CATEGORY_METADATA_LEGACY_MARK_LOCAL_REFUSAL", file=sys.stderr)
        return 1
    print("Coupang category metadata ambiguity recorded")
    return 0


def _coupang_recover_category_metadata(
    config: AppConfig, lease: DataDirLease, args: argparse.Namespace
) -> int:
    """Use one bounded approval for one recovery GET of the unique ambiguous category."""
    from app.platform.core.ownership import require_ownership
    from app.platform.core.secrets import build_secret_store
    from app.stages.connect.coupang.canary_proof import CoupangCanaryProofStore
    from app.stages.connect.coupang.credentials import CoupangCredentialStore
    from app.stages.connect.coupang.live_preflight import CoupangLivePreflightStore
    from integrations.marketplaces.coupang.auth import AuthContext, Market, SystemUtcClock
    from integrations.marketplaces.coupang.canary import ClockPreflight
    from integrations.marketplaces.coupang.live_category_metadata import (
        CoupangCategoryMetadataPreflight,
        LiveCategoryMetadataTransportRefused,
    )

    require_ownership(lease, config.data_dir)
    secrets = build_secret_store(config.secret_backend)
    status = secrets.status()
    if not status.available or status.backend == "memory":
        print("COUPANG_OS_SECRET_STORE_UNAVAILABLE", file=sys.stderr)
        return 1
    try:
        credentials = CoupangCredentialStore(secrets).load()
    except OSError:
        credentials = None
    if credentials is None:
        print("COUPANG_CREDENTIALS_NOT_CONFIGURED", file=sys.stderr)
        return 1
    store = CoupangLivePreflightStore(secrets)
    try:
        recommendations = store.category_recommendations()
        target = store.category_metadata_recovery_target(
            recommendations.unique_category_codes,
            generation=credentials.credential_generation,
        )
        clock = SystemUtcClock()
        now = clock.now()
        proof = CoupangCanaryProofStore(secrets).load_current(credentials=credentials, now=now)
        approved_at = _instant(args.approved_at, "--approved-at")
        expires_at = _instant(args.expires_at, "--expires-at")
        owner = CoupangCategoryMetadataPreflight(
            context=AuthContext(credentials, Market.KR, proof),
            category_codes=(target,),
            clock=clock,
            clock_preflight=ClockPreflight.create(args.clock_skew_seconds, checked_at=now),
        )
        result = owner.run(
            on_before_attempt=lambda code: store.begin_category_metadata_recovery(
                code,
                generation=credentials.credential_generation,
                scope_id=args.scope_id,
                approved_at=approved_at,
                expires_at=expires_at,
                now=now,
            ),
            on_attempt=lambda attempt: store.save_category_metadata_attempt(
                attempt, generation=credentials.credential_generation
            ),
        )
    except (LiveCategoryMetadataTransportRefused, OSError, ValueError):
        print("COUPANG_CATEGORY_METADATA_RECOVERY_LOCAL_REFUSAL", file=sys.stderr)
        return 1
    attempt = result.attempts[0]
    summary = {
        "attempted": 1,
        "failed": 0 if attempt.succeeded else 1,
        "succeeded": 1 if attempt.succeeded else 0,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if attempt.succeeded else 1


def _coupang_recover_test_product_create(
    config: AppConfig, lease: DataDirLease, args: argparse.Namespace
) -> int:
    """Recover one ambiguous CREATE identity; zero or multiple matches remain unresolved."""
    from app.platform.core.errors import AppError
    from app.platform.core.ownership import require_ownership
    from app.platform.core.secrets import build_secret_store
    from app.stages.connect.coupang.canary_proof import CoupangCanaryProofStore
    from app.stages.connect.coupang.credentials import CoupangCredentialStore
    from integrations.marketplaces.coupang.auth import (
        AuthContext,
        Market,
        SystemUtcClock,
    )
    from integrations.marketplaces.coupang.canary import ClockPreflight
    from integrations.marketplaces.coupang.live_mutations import (
        CoupangAmbiguousCreateRecovery,
        LiveMutationTransportRefused,
        MutationJournal,
        TestProductBundle,
    )

    require_ownership(lease, config.data_dir)
    secrets = build_secret_store(config.secret_backend)
    status = secrets.status()
    if not status.available or status.backend == "memory":
        print("COUPANG_OS_SECRET_STORE_UNAVAILABLE", file=sys.stderr)
        return 1
    try:
        credentials = CoupangCredentialStore(secrets).load()
    except OSError:
        credentials = None
    if credentials is None:
        print("COUPANG_CREDENTIALS_NOT_CONFIGURED", file=sys.stderr)
        return 1
    try:
        raw = json.loads(args.bundle.read_text("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("bundle must be an object")
        bundle = TestProductBundle.from_mapping(raw)
        if bundle.payload_digest != args.payload_digest:
            raise ValueError("payload digest does not match the reviewed bundle")
        clock = SystemUtcClock()
        now = clock.now()
        proof = CoupangCanaryProofStore(secrets).load_current(credentials=credentials, now=now)
        result = CoupangAmbiguousCreateRecovery(
            context=AuthContext(credentials, Market.KR, proof),
            bundle=bundle,
            clock=clock,
            clock_preflight=ClockPreflight.create(args.clock_skew_seconds, checked_at=now),
            journal=MutationJournal(config.runtime_dir / "coupang-live-mutations.json"),
        ).recover()
    except (AppError, LiveMutationTransportRefused, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, AppError) else "COUPANG_MUTATION_RECOVERY_LOCAL_REFUSAL"
        print(code, file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


def _coupang_complete_rejected_test_products(
    config: AppConfig, lease: DataDirLease, args: argparse.Namespace
) -> int:
    """Build corrected payloads only in memory and run tests 2..11 once."""
    from app.container import build_container
    from app.platform.core.errors import AppError
    from app.stages.connect.coupang.canary_proof import CoupangCanaryProofStore
    from app.stages.connect.coupang.credentials import CoupangCredentialStore
    from app.stages.connect.coupang.live_bundle import (
        PRIOR_REJECTED_PAYLOAD_DIGEST,
        build_live_test_product_bundle,
    )
    from app.stages.connect.coupang.live_preflight import CoupangLivePreflightStore
    from integrations.marketplaces.coupang.auth import (
        AuthContext,
        Market,
        SystemUtcClock,
    )
    from integrations.marketplaces.coupang.canary import ClockPreflight
    from integrations.marketplaces.coupang.live_mutations import (
        CoupangCorrectedAmbiguousCreateCompletion,
        CoupangCorrectedRejectedTestProductMutationRun,
        LiveMutationTransportRefused,
        MutationJournal,
        TestProductMutationApproval,
    )

    container = build_container(config, ownership=lease)
    try:
        status = container.secrets.status()
        if not status.available or status.backend == "memory":
            print("COUPANG_OS_SECRET_STORE_UNAVAILABLE", file=sys.stderr)
            return 1
        credentials = CoupangCredentialStore(container.secrets).load()
        if credentials is None:
            print("COUPANG_CREDENTIALS_NOT_CONFIGURED", file=sys.stderr)
            return 1
        preflight = CoupangLivePreflightStore(container.secrets)
        recommendations = preflight.category_recommendations()
        metadata = preflight.require_category_metadata(
            recommendations.unique_category_codes,
            generation=credentials.credential_generation,
        )
        bundle = build_live_test_product_bundle(
            credentials=credentials,
            vendor_user_id=preflight.vendor_user_id(credentials=credentials),
            logistics=preflight.logistics(credentials=credentials),
            category_codes_by_test=recommendations.category_codes_by_test,
            category_metadata=metadata,
            revision_of=container.revisions.latest,
            asset_of=container.source_assets.get,
        )
        if args.scope_id != "C-LIVE-MUT-1":
            raise ValueError("scope id must be C-LIVE-MUT-1")
        if bundle.payload_digest != args.payload_digest:
            raise ValueError("payload digest does not match the reviewed bundle")
        clock = SystemUtcClock()
        now = clock.now()
        proof = CoupangCanaryProofStore(container.secrets).load_current(
            credentials=credentials, now=now
        )
        approval = TestProductMutationApproval.create(
            scope_id=args.scope_id,
            vendor_id=credentials.vendor_id,
            credential_generation=credentials.credential_generation,
            payload_digest=bundle.payload_digest,
            approved_at=_instant(args.approved_at, "--approved-at"),
            expires_at=_instant(args.expires_at, "--expires-at"),
        )
        corrected = CoupangCorrectedRejectedTestProductMutationRun(
            context=AuthContext(credentials, Market.KR, proof),
            approval=approval,
            bundle=bundle,
            clock=clock,
            clock_preflight=ClockPreflight.create(args.clock_skew_seconds, checked_at=now),
            journal=MutationJournal(config.runtime_dir / "coupang-live-mutations.json"),
            prior_payload_digest=PRIOR_REJECTED_PAYLOAD_DIGEST,
        ).run()
        if corrected.completed != 10:
            result: object = corrected
        else:
            recovered = CoupangCorrectedAmbiguousCreateCompletion(
                context=AuthContext(credentials, Market.KR, proof),
                approval=approval,
                bundle=bundle,
                clock=clock,
                clock_preflight=ClockPreflight.create(args.clock_skew_seconds, checked_at=now),
                journal=MutationJournal(config.runtime_dir / "coupang-live-mutations.json"),
                prior_payload_digest=PRIOR_REJECTED_PAYLOAD_DIGEST,
            ).run()
            result = {
                "attempted": corrected.attempted,
                "completed": corrected.completed + recovered.completed,
                "payload_digest": bundle.payload_digest,
                "scope_id": corrected.scope_id,
                "total": 11,
            }
    except (AppError, LiveMutationTransportRefused, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, AppError) else "COUPANG_CORRECTED_MUTATION_LOCAL_REFUSAL"
        print(code, file=sys.stderr)
        return 1
    finally:
        container.db.dispose()
    safe_result = (
        asdict(result) if is_dataclass(result) and not isinstance(result, type) else result
    )
    print(json.dumps(safe_result, ensure_ascii=False, sort_keys=True))
    return 0


class _OperatorInputError(ValueError):
    """An operator input that is not even well-formed: refused before any owner is asked."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"not JSON-encodable: {type(value).__name__}")


def _instant(text: str, name: str) -> datetime:
    """An ISO-8601 instant with an explicit offset; a naive time is never guessed into a zone."""
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _OperatorInputError("LIVE_CLI_TIME_INVALID", f"{name} is not ISO-8601") from exc
    if value.tzinfo is None:
        raise _OperatorInputError("LIVE_CLI_TIME_INVALID", f"{name} has no UTC offset")
    return value


def _json_file(path: Path, name: str) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise _OperatorInputError("LIVE_CLI_FILE_UNREADABLE", f"{name}: {exc}") from exc


def _artifacts(path: Path) -> list[Any]:
    from app.capabilities.live_safety.store import ArtifactRef
    from app.stages.register.model import publication_asset_kind

    entries = _json_file(path, "--artifacts")
    if not isinstance(entries, list):
        raise _OperatorInputError("LIVE_CLI_ARTIFACTS_INVALID", "--artifacts is not a JSON list")
    artifacts = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"asset_kind", "sha256", "derivation_id"}:
            raise _OperatorInputError(
                "LIVE_CLI_ARTIFACTS_INVALID",
                "each artifact is exactly {asset_kind, sha256, derivation_id}",
            )
        try:
            # An M4 image, or a Detail Guidance image (ADR-0033 §8).
            kind = publication_asset_kind(entry["asset_kind"])
        except ValueError as exc:
            raise _OperatorInputError("LIVE_CLI_ARTIFACTS_INVALID", "unknown asset_kind") from exc
        artifacts.append(ArtifactRef(kind, entry["sha256"], entry["derivation_id"]))
    return artifacts


def _operate(config: AppConfig, lease: DataDirLease, args: argparse.Namespace) -> int:
    """Run one protected operator command against the existing owners it names (5915900049 D3).

    The owner decides; this prints its answer as JSON, or its refusal code on stderr and exits 1.
    """
    from app.container import build_container
    from app.platform.core.errors import AppError

    configure_logging(config.log_level, config.log_dir)
    correlation_id = f"live-{args.live_command}-{uuid.uuid4()}"
    container = build_container(config, ownership=lease)
    try:
        result = _OPERATIONS[args.live_command](container, args, correlation_id)
    except _OperatorInputError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    except AppError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    finally:
        container.db.dispose()
    # The owner's answer is the last stdout line, as one JSON document: the application log also
    # writes to stdout, and never after it.
    print(json.dumps(result, default=_json_default, ensure_ascii=False, sort_keys=True))
    return 0


def _inspect(container: Any, args: argparse.Namespace, correlation_id: str) -> dict[str, Any]:
    return {
        "live": container.live_status.status(),
        "canary": container.register.canary_readiness(args.unit_ref).model_dump(mode="json"),
    }


def _eligibility_packet(
    container: Any, args: argparse.Namespace, correlation_id: str
) -> dict[str, Any]:
    packet = container.canary_eligibility.review_packet(args.preparation_id)
    return {"digest": packet.digest, "binding": packet.binding, "packet": dict(packet.packet)}


def _record_eligibility(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    checks = _json_file(args.checks, "--checks")
    if not isinstance(checks, dict):
        raise _OperatorInputError("LIVE_CLI_CHECKS_INVALID", "--checks is not a JSON object")
    return container.canary_eligibility.record(
        preparation_id=args.preparation_id,
        expected_packet_digest=args.packet_digest,
        checks=checks,
        actor=args.actor,
        correlation_id=correlation_id,
    )


def _record_residual_risk_acceptance(
    container: Any, args: argparse.Namespace, correlation_id: str
) -> Any:
    return container.residual_risk.record(
        marketplace_key=args.marketplace_key,
        marketplace_account_id=args.account,
        risk_contract=args.risk_contract,
        user_acceptance=args.user_acceptance,
        architect_acceptance=args.architect_acceptance,
        actor=args.actor,
        correlation_id=correlation_id,
    )


def _issue_asset_grant(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    return container.live_authority.issue_asset_grant(
        marketplace_key=args.marketplace,
        marketplace_account_id=args.account,
        preparation_revision_id=args.preparation_revision_id,
        candidate_fingerprint=args.candidate_fingerprint,
        artifacts=_artifacts(args.artifacts),
        asset_profile=args.asset_profile,
        budget=args.budget,
        not_before=_instant(args.not_before, "--not-before"),
        expires_at=_instant(args.expires_at, "--expires-at"),
        approved_by=args.approved_by,
        authorization_ref=args.authorization_ref,
        correlation_id=correlation_id,
    )


def _issue_delete_grant(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    return container.live_authority.issue_delete_grant(
        registration_id=args.registration_id,
        not_before=_instant(args.not_before, "--not-before"),
        expires_at=_instant(args.expires_at, "--expires-at"),
        approved_by=args.approved_by,
        authorization_ref=args.authorization_ref,
        correlation_id=correlation_id,
    )


def _issue_dispatch_grant(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    return container.live_authority.issue_dispatch_grant(
        product_order_id=args.product_order_id,
        not_before=_instant(args.not_before, "--not-before"),
        expires_at=_instant(args.expires_at, "--expires-at"),
        approved_by=args.approved_by,
        authorization_ref=args.authorization_ref,
        correlation_id=correlation_id,
    )


def _issue_create_grant(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    return container.live_authority.issue_create_grant(
        intent_id=args.intent_id,
        not_before=_instant(args.not_before, "--not-before"),
        expires_at=_instant(args.expires_at, "--expires-at"),
        approved_by=args.approved_by,
        authorization_ref=args.authorization_ref,
        correlation_id=correlation_id,
    )


def _release_brake(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    return container.live_authority.release_brake(
        actor=args.actor,
        reason_code=args.reason_code,
        authorization_ref=args.authorization_ref,
        correlation_id=correlation_id,
    )


def _engage_brake(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    return container.live_authority.engage_brake(
        actor=args.actor, reason_code=args.reason_code, correlation_id=correlation_id
    )


def _restore_drill_asset(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    return container.restore_drills.drill_asset(
        args.grant_id,
        restore_root=args.restore_root,
        actor=args.actor,
        correlation_id=correlation_id,
    )


def _restore_drill_create(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    return container.restore_drills.drill_create(
        args.intent_id,
        restore_root=args.restore_root,
        actor=args.actor,
        correlation_id=correlation_id,
    )


def _upload_assets(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    """The one production upload entry point (owner decision 5975217061): the run composes the
    grant, CONNECT, this process's bounded LIVE window and the M4 lineage stores; every layer of
    the send-time stack still decides each upload."""
    return container.asset_upload_run.run(
        args.grant_id, window_s=args.window_s, actor=args.actor, correlation_id=correlation_id
    )


def _prove_retention(container: Any, args: argparse.Namespace, correlation_id: str) -> Any:
    proof_id, verdict = container.retention.prove(actor=args.actor, correlation_id=correlation_id)
    return {"proof_id": proof_id, "verdict": verdict}


_OPERATIONS: dict[str, Callable[[Any, argparse.Namespace, str], Any]] = {
    "inspect": _inspect,
    "eligibility-packet": _eligibility_packet,
    "record-eligibility": _record_eligibility,
    "issue-asset-grant": _issue_asset_grant,
    "issue-create-grant": _issue_create_grant,
    "issue-delete-grant": _issue_delete_grant,
    "issue-dispatch-grant": _issue_dispatch_grant,
    "release-brake": _release_brake,
    "engage-brake": _engage_brake,
    "restore-drill-asset": _restore_drill_asset,
    "restore-drill-create": _restore_drill_create,
    "prove-retention": _prove_retention,
    "record-residual-risk-acceptance": _record_residual_risk_acceptance,
    "upload-assets": _upload_assets,
}


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
    **{("live", name): _operate for name in LIVE_OPERATOR_COMMANDS},
    ("extension", "pair"): _extension_pairing,
    ("extension", "rotate"): _extension_pairing,
    ("extension", "revoke"): _extension_pairing,
    ("coupang", "save-credentials"): _coupang_credentials,
    ("coupang", "bind-saved-delivery"): _coupang_bind_saved_delivery,
    ("coupang", "read-canary"): _coupang_read_canary,
    ("coupang", "read-category-metadata"): _coupang_read_category_metadata,
    (
        "coupang",
        "mark-category-metadata-ambiguous",
    ): _coupang_mark_category_metadata_ambiguous,
    ("coupang", "recover-category-metadata"): _coupang_recover_category_metadata,
    ("coupang", "mutate-test-products"): _coupang_mutate_test_products,
    (
        "coupang",
        "complete-rejected-test-products",
    ): _coupang_complete_rejected_test_products,
    ("coupang", "recover-test-product-create"): _coupang_recover_test_product_create,
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
