"""The Phase C harness commands (Issue #110 C0 `5826469852`; supplement `5313045448`).

Every command that touches the live ``--data-root`` runs the stopped-app sequence: validate both
roots, verify the campaign ledger, **acquire the ADR-0006 data-directory lease** (a live server or
another harness makes this fail ``DATA_DIR_IN_USE`` before any database, log or service effect),
check the schema head, compose the production owners with that lease, act through their public
APIs only, append the campaign event, dispose and release. It never opens SQLite itself and never
submits a product collection.

A command that can affect real evidence also needs its stage authorized in the campaign ledger and
the campaign's typed approval phrase. In C0 only stage C0 is authorized, so every such command is
refused against a real campaign; the C0 tests exercise them on synthetic data roots only.
"""

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO

from app.collect.adaptive.capture import OperatorExclusion, OperatorReason, OperatorScope
from app.collect.adaptive.validation import freshness_tuple, validate
from app.collect.adaptive_capture.controls import SYNTHETIC_NEGATIVES
from app.collect.adaptive_shadow.evidence import Resolution
from app.collect.adaptive_shadow.store import RAW_MAX_AGE
from app.collect.adaptive_shadow.switch import bundle_key_of
from app.config import AppConfig
from app.container import Container, build_container
from app.core.errors import AppError
from app.core.ownership import DataDirLease, DataDirOwnershipError, acquire_data_dir
from scripts.phasec.artifacts import (
    ARTIFACT_SCHEMA,
    ArtifactRefused,
    canonical,
    read_resolution,
    write_resolution,
)
from scripts.phasec.ledger import (
    Campaign,
    CampaignLedger,
    LedgerRefused,
    approval_phrase,
    now,
)
from scripts.phasec.roots import REPO_ROOT, RootsRefused, require_roots

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_DATA_DIR_IN_USE = 3
CANDIDATES = "candidates"
CLOSEOUTS = "closeouts"
HARNESS_VERSION = "phase-c-harness-1"

Compose = Callable[[AppConfig, DataDirLease], Container]
CodeSha = Callable[[], str]


def production_compose(config: AppConfig, lease: DataDirLease) -> Container:
    return build_container(config, ownership=lease)


def checkout_sha() -> str:
    """The exact, clean checkout this harness runs from (the M4 acceptance checkout rules)."""
    from scripts.m4accept.checkout import probe_checkout

    checkout = probe_checkout(REPO_ROOT)
    if checkout.problems or checkout.code_sha is None:
        raise LedgerRefused("; ".join(checkout.problems) or "no exact code SHA")
    return checkout.code_sha


class Refused(RuntimeError):
    pass


@dataclass(frozen=True)
class Command:
    stage: str | None  # the campaign stage it needs authorized, None for campaign-only reads
    data_root: bool  # it composes the owners over the live data root (stopped-app)
    evidence: bool  # it can affect real evidence: stage + approval phrase required


COMMANDS: Mapping[str, Command] = {
    "init": Command(None, False, False),
    "authorize-stage": Command(None, False, True),
    "status": Command(None, True, False),
    "candidates": Command(None, True, False),
    "export-candidate": Command(None, True, False),
    "request-capture": Command("C1", True, True),
    "finalize-sample": Command("C1", True, True),
    "validate": Command("C1", True, True),
    "enable": Command("C2", True, True),
    "disable": Command("C2", True, True),
    "declare": Command("C2", True, True),
    "resolve": Command("C3", True, True),
    "end": Command("C4", True, True),
    "close": Command("C4", True, True),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phase_c", description=__doc__)
    parser.add_argument("--campaign-root", required=True, type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--actor")
    parser.add_argument("--correlation")
    parser.add_argument("--approve", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--campaign-id", required=True)
    init.add_argument("--authorization", required=True)
    authorize = sub.add_parser("authorize-stage")
    authorize.add_argument("--stage", required=True)
    authorize.add_argument("--authorization", required=True)
    status = sub.add_parser("status")
    status.add_argument("--supplier", required=True)
    sub.add_parser("candidates")
    export = sub.add_parser("export-candidate")
    export.add_argument("--run", required=True)
    request = sub.add_parser("request-capture")
    request.add_argument("--supplier", required=True)
    request.add_argument("--target-url", required=True)
    request.add_argument("--lifetime-hours", type=float, default=4.0)
    finalize = sub.add_parser("finalize-sample")
    finalize.add_argument("--run", required=True)
    finalize.add_argument("--scope", required=True, type=Path)
    finalize.add_argument("--expected", required=True, type=Path)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--epr", required=True)
    validate_parser.add_argument("--sample", action="append", default=[], required=True)
    enable = sub.add_parser("enable")
    enable.add_argument("--epr", required=True)
    # The samples of the PASS run whose freshness the switch binds (none only for a PASS run
    # recorded over none, as synthetic tests do).
    enable.add_argument("--sample", action="append", default=[])
    disable = sub.add_parser("disable")
    disable.add_argument("--supplier", required=True)
    declare = sub.add_parser("declare")
    declare.add_argument("--epr", required=True)
    declare.add_argument("--min-size", type=int, default=3)
    declare.add_argument("--supersedes")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--run", required=True)
    resolve.add_argument("--resolution", required=True, choices=[r.value for r in Resolution])
    resolve.add_argument("--adaptive-failed-closed", required=True, choices=["yes", "no"])
    resolve.add_argument("--dimension", action="append", default=[], required=True)
    resolve.add_argument("--source-evidence", action="append", default=[], required=True)
    for name in ("end", "close"):
        command = sub.add_parser(name)
        command.add_argument("--window", required=True)
    return parser


def run(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    compose: Compose = production_compose,
    code_sha: CodeSha = checkout_sha,
    out: TextIO = sys.stdout,
) -> int:
    env = os.environ if environ is None else environ
    args = build_parser().parse_args(list(argv))
    spec = COMMANDS[args.command]
    campaign_root: Path = args.campaign_root
    data_root: Path | None = args.data_root
    try:
        if spec.data_root and data_root is None:
            raise Refused("this command acts on the live data root: --data-root is required")
        require_roots(campaign_root, data_root, env)
        ledger = CampaignLedger(campaign_root.resolve())
        if args.command == "init":
            ledger.create(
                campaign_id=args.campaign_id,
                code_sha=code_sha(),
                authorization=args.authorization,
                actor=_actor(args),
            )
            _emit(out, {"campaign": args.campaign_id, "ledger": str(ledger.path)})
            return EXIT_OK
        campaign = ledger.campaign()
        _gate(campaign, args, spec)
        if not spec.data_root:
            return _campaign_only(ledger, campaign, args, out)
        assert data_root is not None
        return _with_data_root(ledger, campaign, args, data_root, env, compose, out)
    except (Refused, RootsRefused, LedgerRefused, ArtifactRefused, AppError) as refused:
        _emit(out, {"refused": str(refused), "code": getattr(refused, "code", None)})
        return EXIT_REFUSED


def _actor(args: argparse.Namespace) -> str:
    if not args.actor or not str(args.actor).strip():
        raise Refused("every campaign action names its --actor")
    return str(args.actor)


def _gate(campaign: Campaign, args: argparse.Namespace, spec: Command) -> None:
    if spec.stage is not None and spec.stage not in campaign.authorized:
        raise Refused(f"stage {spec.stage} is not authorized in this campaign")
    if spec.evidence:
        _actor(args)
        if args.approve != approval_phrase(campaign, args.command):
            raise Refused("the typed approval phrase does not match this command")


def _campaign_only(
    ledger: CampaignLedger, campaign: Campaign, args: argparse.Namespace, out: TextIO
) -> int:
    assert args.command == "authorize-stage"
    event = ledger.append(
        "STAGE_AUTHORIZED",
        _actor(args),
        args.correlation or f"{campaign.campaign_id}:stage:{args.stage}",
        {"stage": args.stage, "authorization": args.authorization},
    )
    _emit(out, {"authorized": args.stage, "seq": event["seq"]})
    return EXIT_OK


def _with_data_root(
    ledger: CampaignLedger,
    campaign: Campaign,
    args: argparse.Namespace,
    data_root: Path,
    env: Mapping[str, str],
    compose: Compose,
    out: TextIO,
) -> int:
    """The stopped-app sequence: lease first, then everything else, all under it."""
    try:
        lease = acquire_data_dir(data_root.resolve(), app_version=HARNESS_VERSION)
    except DataDirOwnershipError as refused:
        _emit(out, {"refused": str(refused), "code": refused.reason_code})
        return EXIT_DATA_DIR_IN_USE
    with lease:
        config = AppConfig.from_env(env, data_dir=data_root.resolve())
        app = compose(config, lease)
        try:
            if not app.readiness.schema_at_head():
                raise Refused("the data root's schema is not at head: run `icbm db upgrade` first")
            result = HANDLERS[args.command](app, ledger, campaign, args)
        finally:
            app.db.dispose()
    _emit(out, result)
    return EXIT_OK


def _emit(out: TextIO, value: Any) -> None:
    out.write(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def _correlation(campaign: Campaign, args: argparse.Namespace) -> str:
    return str(args.correlation or f"{campaign.campaign_id}:{args.command}:{uuid.uuid4().hex[:12]}")


def _record(
    ledger: CampaignLedger,
    campaign: Campaign,
    args: argparse.Namespace,
    correlation: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return ledger.append(args.command.upper().replace("-", "_"), _actor(args), correlation, payload)


# ================================================================ handlers


def _status(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    supplier = args.supplier
    evidence = app.shadow_evidence
    unresolved = []
    windows = []
    for window in evidence.windows(supplier):
        states = evidence.window_evidence(window.window_id).states
        windows.append(
            {
                "window_id": window.window_id,
                "bundle_key": window.bundle_key,
                "events": list(window.events),
                "eligible_runs": len(states),
            }
        )
        for run_id, state in states.items():
            if not state.terminal:
                recorded_at = evidence.record(run_id).recorded_at
                unresolved.append(
                    {
                        "collection_run_id": run_id,
                        "window_id": window.window_id,
                        "resolve_before": (recorded_at + RAW_MAX_AGE).isoformat(),
                        "note": "a mandatory pre-close item: resolve from source evidence before"
                        " either raw bound (90 days or 5,000 per supplier) prunes it; a pruned"
                        " unresolved run becomes PRUNED_BEFORE_RESOLUTION and blocks the bundle",
                    }
                )
    switch = app.shadow_switch.current(supplier)
    retention = evidence.retention_status(supplier)
    return {
        "campaign_id": campaign.campaign_id,
        "code_sha": campaign.code_sha,
        "stages_authorized": dict(campaign.authorized),
        "ceilings": campaign.ceilings,
        "switch": None
        if switch is None
        else {
            "entry_id": switch.entry_id,
            "action": switch.action,
            "bundle_key": switch.bundle_key,
        },
        "windows": windows,
        "unresolved_mismatches": unresolved,
        "retention": {
            "records": retention.records,
            "count_headroom": retention.count_headroom,
            "age_deadline": retention.age_deadline,
            "estimated_count_deadline": retention.estimated_count_deadline,
            "nearer_bound": retention.nearer_bound,
        },
        "capture_requests": [
            {
                "request_id": r.request_id,
                "target_digest": r.target_digest,
                "expires_at": r.expires_at,
                "consumed_by": r.consumed_by,
            }
            for r in app.capture_store.requests(campaign.campaign_id)
        ],
        "ledger_events": len(campaign.events),
    }


def _candidates(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    return [
        {
            "collection_run_id": view.collection_run_id,
            "status": view.status,
            "candidate_digest": None if view.candidate is None else view.candidate.digest,
            "regions": [] if view.candidate is None else view.candidate.regions(),
            "refusal": view.refusal,
        }
        for view in app.capture_store.candidates(campaign.campaign_id)
    ]


def _export_candidate(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    """Write a sanitized candidate into the campaign root, for the operator to author a scope and
    the expected facts from. It holds only what the capture's final scan already passed."""
    view = app.capture_store.candidate(args.run)
    if view.candidate is None:
        raise Refused("this run has no captured candidate")
    directory = ledger.root / CANDIDATES
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{view.candidate.digest}.json"
    content = canonical(
        {
            "candidate_digest": view.candidate.digest,
            "collection_run_id": view.collection_run_id,
            "structure": view.candidate.structure,
            "regions": view.candidate.regions(),
        }
    )
    if not path.exists():
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    return {"exported": path.name}


def _request_capture(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    target = app.collection.target_of(args.supplier, args.target_url)
    correlation = _correlation(campaign, args)
    request_id = app.capture_store.request(
        campaign_id=campaign.campaign_id,
        supplier_key=args.supplier,
        target=target,
        lifetime=timedelta(hours=args.lifetime_hours),
        requested_by=_actor(args),
        correlation_id=correlation,
    )
    (record,) = [
        r for r in app.capture_store.requests(campaign.campaign_id) if r.request_id == request_id
    ]
    _record(
        ledger,
        campaign,
        args,
        correlation,
        {"request_id": request_id, "target_digest": record.target_digest},
    )
    return {"request_id": request_id, "target_digest": record.target_digest}


def _scope(path: Path, actor: str) -> OperatorScope:
    raw = json.loads(path.read_text("utf-8"))
    return OperatorScope(
        decided_by=actor,
        decided_at=now(),
        product_boundary=str(raw["product_boundary"]),
        confirmed_regions=tuple(str(r) for r in raw.get("confirmed_regions", ())),
        exclusions=tuple(
            OperatorExclusion(str(token), OperatorReason(str(reason)))
            for token, reason in raw.get("exclusions", ())
        ),
    )


def _finalize_sample(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    correlation = _correlation(campaign, args)
    expected = json.loads(args.expected.read_text("utf-8"))
    if not isinstance(expected, dict):
        raise Refused("the expected facts are one JSON object the operator authored")
    sample = app.capture_store.finalize(
        args.run,
        scope=_scope(args.scope, _actor(args)),
        expected=expected,
        stored_by=_actor(args),
        correlation_id=correlation,
    )
    payload = {
        "collection_run_id": args.run,
        "sample_digest": sample.digest,
        "truncated": sample.truncated,
    }
    _record(ledger, campaign, args, correlation, payload)
    return payload


def _validate(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    bundle = app.adaptive_profiles.load_bundle(args.epr)
    samples = [app.adaptive_validation.sample(d) for d in args.sample]
    result = validate(bundle, samples, negatives=SYNTHETIC_NEGATIVES)
    correlation = _correlation(campaign, args)
    run_id = app.adaptive_validation.record_run(
        bundle, result, samples, recorded_by=_actor(args), correlation_id=correlation
    )
    payload = {"epr": args.epr, "run_id": run_id, "verdict": result.verdict.value}
    _record(ledger, campaign, args, correlation, payload)
    return payload


def _enable(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    bundle = app.adaptive_profiles.load_bundle(args.epr)
    samples = [app.adaptive_validation.sample(d) for d in args.sample]
    freshness = freshness_tuple(bundle, samples, None)
    correlation = _correlation(campaign, args)
    entry = app.shadow_switch.enable(
        args.epr, freshness, actor=_actor(args), reason="PHASE_C", correlation_id=correlation
    )
    payload = {"entry_id": entry, "bundle_key": bundle_key_of(args.epr)}
    _record(ledger, campaign, args, correlation, payload)
    return payload


def _disable(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    correlation = _correlation(campaign, args)
    entry = app.shadow_switch.disable(
        args.supplier, actor=_actor(args), reason="PHASE_C", correlation_id=correlation
    )
    _record(ledger, campaign, args, correlation, {"entry_id": entry})
    return {"entry_id": entry}


def _declare(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    record = app.adaptive_profiles.record(args.epr)
    correlation = _correlation(campaign, args)
    window = app.shadow_evidence.declare(
        record.supplier_key,
        bundle_key_of(args.epr),
        actor=_actor(args),
        reason="PHASE_C",
        correlation_id=correlation,
        min_size=args.min_size,
        supersedes=args.supersedes,
    )
    payload = {"window_id": window, "bundle_key": bundle_key_of(args.epr)}
    _record(ledger, campaign, args, correlation, payload)
    return payload


def _resolve(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    """The human operator's resolution: its artifact first, then the ledger event naming it."""
    run = app.collection.run(args.run)
    correlation = _correlation(campaign, args)
    reference = write_resolution(
        ledger.root,
        {
            "schema": ARTIFACT_SCHEMA,
            "campaign_id": campaign.campaign_id,
            "collection_run_id": args.run,
            "revision_id": run.revision_id or "NO_REVISION",
            "mismatch_dimensions": sorted(args.dimension),
            "source_evidence": sorted(args.source_evidence),
            "resolution": args.resolution,
            "adaptive_failed_closed": args.adaptive_failed_closed == "yes",
            "actor": _actor(args),
            "correlation_id": correlation,
            "at": now(),
        },
    )
    read_resolution(ledger.root, reference)  # it exists and hashes, before the event
    state = app.shadow_evidence.resolve(
        args.run,
        Resolution(args.resolution),
        evidence_ref=reference,
        adaptive_failed_closed=args.adaptive_failed_closed == "yes",
        actor=_actor(args),
        correlation_id=correlation,
    )
    payload = {
        "collection_run_id": args.run,
        "evidence_ref": reference,
        "count_as": state.count_as.value,
    }
    _record(ledger, campaign, args, correlation, payload)
    return payload


def _end(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    correlation = _correlation(campaign, args)
    app.shadow_evidence.end(args.window, actor=_actor(args), reason="PHASE_C")
    _record(ledger, campaign, args, correlation, {"window_id": args.window})
    return {"ended": args.window}


def _close(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    correlation = _correlation(campaign, args)
    closeout = app.shadow_evidence.close(args.window, actor=_actor(args), reason="PHASE_C")
    directory = ledger.root / CLOSEOUTS
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{args.window}.json").open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical(closeout))
    _record(
        ledger,
        campaign,
        args,
        correlation,
        {"window_id": args.window, "verdict": closeout["verdict"]},
    )
    return closeout


HANDLERS: Mapping[str, Callable[[Container, CampaignLedger, Campaign, Any], Any]] = {
    "status": _status,
    "candidates": _candidates,
    "export-candidate": _export_candidate,
    "request-capture": _request_capture,
    "finalize-sample": _finalize_sample,
    "validate": _validate,
    "enable": _enable,
    "disable": _disable,
    "declare": _declare,
    "resolve": _resolve,
    "end": _end,
    "close": _close,
}
