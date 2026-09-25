"""The Phase C harness commands (Issue #110 C0 `5826469852`; supplement `5313045448`; review
`5313663701`).

Every command after ``init`` runs this sequence, and each step refuses before the next one has any
effect:

1. validate both roots;
2. read and verify the campaign ledger, read-only;
3. **require the exact clean checkout the campaign was created at** (B1): another SHA or an
   unclean checkout is refused before any lock, lease, database, artifact or ledger effect;
4. take the campaign's exclusive writer lock (``CAMPAIGN_IN_USE`` otherwise) and re-read it;
5. require the command's stage to be the campaign's **current** stage (stages advance only by
   typed grants, C0 → C4, once each), its typed approval phrase, and no unfinished action;
6. for a data-root command, **acquire the ADR-0006 data-directory lease** (a live server or
   another harness fails ``DATA_DIR_IN_USE`` before any database, log or service effect), check
   the schema head and compose the production owners with that lease;
7. check that every object the command names belongs to this campaign (B3), commit the intent
   and its ceiling reservations to the campaign ledger, act through the owners' public APIs, and
   commit the outcome.

It never opens the live database itself and never submits a product collection. In C0 only
stage C0 is granted, so every evidence command is refused against a real campaign; the C0 tests
exercise them on synthetic data roots only.
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
from app.collect.adaptive_capture.store import target_digest
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
from scripts.phasec.grants import c0_grant, check_grant, grant_digest, supplier_of
from scripts.phasec.ledger import (
    ACTION_REFUSED,
    Campaign,
    CampaignInUse,
    CampaignLedger,
    LedgerRefused,
    approval_phrase,
    now,
)
from scripts.phasec.roots import REPO_ROOT, RootsRefused, require_roots

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_DATA_DIR_IN_USE = 3
EXIT_CAMPAIGN_IN_USE = 4
CANDIDATES = "candidates"
CLOSEOUTS = "closeouts"
HARNESS_VERSION = "phase-c-harness-2"

Compose = Callable[[AppConfig, DataDirLease], Container]
CodeSha = Callable[[], str]


def production_compose(config: AppConfig, lease: DataDirLease) -> Container:
    return build_container(config, ownership=lease)


def checkout_sha() -> str:
    """The exact, clean checkout this harness runs from (the M4 acceptance checkout rules)."""
    from scripts.m4accept.checkout import probe_checkout

    checkout = probe_checkout(REPO_ROOT)
    if checkout.problems or checkout.code_sha is None:
        raise LedgerRefused(
            "; ".join(checkout.problems) or "no exact code SHA", code="PHASE_C_CHECKOUT_UNCLEAN"
        )
    return checkout.code_sha


class Refused(RuntimeError):
    code = "PHASE_C_REFUSED"


@dataclass(frozen=True)
class Command:
    stages: frozenset[str] | None  # the current stages it runs in; None for reads
    data_root: bool  # it composes the owners over the live data root (stopped-app)
    evidence: bool  # it can affect real evidence: approval phrase, no unfinished action


def _in(*stages: str) -> frozenset[str]:
    return frozenset(stages)


COMMANDS: Mapping[str, Command] = {
    "init": Command(None, False, False),
    "authorize-stage": Command(None, False, True),
    "status": Command(None, True, False),
    "candidates": Command(None, True, False),
    "export-candidate": Command(None, True, False),
    "target-digest": Command(None, True, False),
    "request-capture": Command(_in("C1"), True, True),
    "finalize-sample": Command(_in("C1"), True, True),
    "validate": Command(_in("C1"), True, True),
    "enable": Command(_in("C2"), True, True),
    # Turning the shadow off is the brake: it stays available in every later stage.
    "disable": Command(_in("C2", "C3", "C4"), True, True),
    "declare": Command(_in("C2"), True, True),
    "resolve": Command(_in("C3"), True, True),
    "end": Command(_in("C4"), True, True),
    "close": Command(_in("C4"), True, True),
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
    authorize.add_argument("--grant", required=True, type=Path)
    status = sub.add_parser("status")
    status.add_argument("--supplier", required=True)
    sub.add_parser("candidates")
    export = sub.add_parser("export-candidate")
    export.add_argument("--run", required=True)
    digest = sub.add_parser("target-digest")
    digest.add_argument("--supplier", required=True)
    digest.add_argument("--target-url", required=True)
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
    disable = sub.add_parser("disable")
    disable.add_argument("--supplier", required=True)
    declare = sub.add_parser("declare")
    declare.add_argument("--epr", required=True)
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
            return _init(ledger, args, code_sha, out)
        # B1: the exact code the campaign was created at, before anything else happens.
        _require_exact_code(ledger.campaign(), code_sha)
        with ledger.writer():
            campaign = ledger.campaign()
            if args.command == "authorize-stage":
                # Read once: the phrase is checked against, and the ledger records, this object.
                args.grant_document = _load_grant(args.grant)
            _gate(campaign, args, spec)
            if not spec.data_root:
                return _authorize_stage(ledger, campaign, args, out)
            assert data_root is not None
            return _with_data_root(ledger, campaign, args, data_root, env, compose, out)
    except CampaignInUse as refused:
        _emit(out, {"refused": str(refused), "code": refused.code})
        return EXIT_CAMPAIGN_IN_USE
    except (Refused, RootsRefused, LedgerRefused, ArtifactRefused, AppError) as refused:
        _emit(out, {"refused": str(refused), "code": getattr(refused, "code", None)})
        return EXIT_REFUSED


def _init(ledger: CampaignLedger, args: argparse.Namespace, code_sha: CodeSha, out: TextIO) -> int:
    actor = _actor(args)
    sha = code_sha()
    grant = c0_grant(args.campaign_id, sha, args.authorization)
    if ledger.path.exists():
        raise Refused("this campaign root already holds a campaign")
    ledger.root.mkdir(parents=True, exist_ok=True)
    with ledger.writer():
        ledger.create(campaign_id=args.campaign_id, code_sha=sha, c0_grant=grant, actor=actor)
    _emit(out, {"campaign": args.campaign_id, "ledger": str(ledger.path), "code_sha": sha})
    return EXIT_OK


def _require_exact_code(campaign: Campaign, code_sha: CodeSha) -> None:
    running = code_sha()
    if running != campaign.code_sha:
        raise Refused(
            f"this campaign runs only at its exact code SHA {campaign.code_sha[:12]};"
            f" this checkout is {running[:12]}"
        )


def _actor(args: argparse.Namespace) -> str:
    if not args.actor or not str(args.actor).strip():
        raise Refused("every campaign action names its --actor")
    return str(args.actor)


def _load_grant(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        raise Refused("the grant is one JSON object copied from its authorization") from None


def phrase_for(campaign: Campaign, args: argparse.Namespace) -> str:
    """The exact approval phrase this command needs."""
    if args.command == "authorize-stage":
        grant = args.grant_document
        stage = grant.get("stage") if isinstance(grant, dict) else None
        return approval_phrase(campaign, args.command, f"{stage} GRANT {grant_digest(grant)[:16]}")
    return approval_phrase(campaign, args.command)


def _gate(campaign: Campaign, args: argparse.Namespace, spec: Command) -> None:
    current = campaign.current_stage
    if spec.stages is not None and current not in spec.stages:
        raise Refused(
            f"{args.command} runs in stage {'/'.join(sorted(spec.stages))};"
            f" this campaign's current stage is {current}"
        )
    if spec.evidence:
        _actor(args)
        if args.approve != phrase_for(campaign, args):
            raise Refused("the typed approval phrase does not match this command")
        if unfinished := campaign.unfinished():
            raise Refused(
                f"this campaign has an unfinished action ({unfinished[0]}): its evidence is"
                " incomplete, so it accepts no further evidence command"
            )


def _authorize_stage(
    ledger: CampaignLedger, campaign: Campaign, args: argparse.Namespace, out: TextIO
) -> int:
    assert args.command == "authorize-stage"
    grant = check_grant(campaign, args.grant_document)
    event = ledger.authorize(
        grant,
        actor=_actor(args),
        correlation_id=args.correlation or f"{campaign.campaign_id}:stage:{grant['stage']}",
    )
    _emit(
        out,
        {"authorized": grant["stage"], "grant_digest": grant_digest(grant), "seq": event["seq"]},
    )
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


def _kind(command: str) -> str:
    return command.upper().replace("-", "_")


def _act(
    ledger: CampaignLedger,
    campaign: Campaign,
    args: argparse.Namespace,
    effect: Callable[[str], dict[str, Any]],
    *,
    intent: Mapping[str, Any] | None = None,
    reservations: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Intent and reservations first, then the live-data effect, then its outcome."""
    actor = _actor(args)
    correlation = str(
        args.correlation or f"{campaign.campaign_id}:{args.command}:{uuid.uuid4().hex[:12]}"
    )
    ledger.intend(
        args.command,
        actor=actor,
        correlation_id=correlation,
        payload=dict(intent or {}),
        reservations=reservations,
    )
    try:
        payload = effect(correlation)
    except (AppError, Refused, ArtifactRefused) as refused:
        # The owner refused before it wrote anything: record that, so the intent is answered.
        ledger.record(
            ACTION_REFUSED,
            actor=actor,
            correlation_id=correlation,
            payload={"command": args.command, "code": getattr(refused, "code", None)},
        )
        raise
    ledger.record(_kind(args.command), actor=actor, correlation_id=correlation, payload=payload)
    return payload


def _one(campaign: Campaign, kind: str, **match: Any) -> Mapping[str, Any] | None:
    found = [
        payload
        for payload in campaign.outcomes(kind)
        if all(payload.get(key) == value for key, value in match.items())
    ]
    return found[-1] if found else None


def _scope(campaign: Campaign, stage: str) -> Mapping[str, Any]:
    return campaign.grants[stage].scope


# ================================================================ reads


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
        "current_stage": campaign.current_stage,
        "grants": {s: g.digest for s, g in campaign.grants.items()},
        "stages_authorized": campaign.authorized,
        "ceilings": campaign.ceilings,
        "reserved": {stage: ledger.counts(stage) for stage in campaign.grants},
        "refusals": len(ledger.refusals()),
        "unfinished_actions": campaign.unfinished(),
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


def _own_candidate(app: Container, campaign: Campaign, run_id: str) -> Any:
    view = app.capture_store.candidate(run_id)
    requested = {p["request_id"] for p in campaign.outcomes("REQUEST_CAPTURE")}
    if view.campaign_id != campaign.campaign_id or view.request_id not in requested:
        raise Refused("a campaign uses only the capture candidates of its own requests")
    return view


def _export_candidate(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    """Write a sanitized candidate of this campaign into the campaign root, for the operator to
    author a scope and the expected facts from. It holds only what the capture's final scan
    already passed."""
    view = _own_candidate(app, campaign, args.run)
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


def _target_digest(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    """A target's digest, for the C1 grant to name; the URL itself is never kept."""
    target = app.collection.target_of(args.supplier, args.target_url)
    return {"supplier_key": args.supplier, "target_digest": target_digest(args.supplier, target)}


# ================================================================ C1


def _request_capture(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    scope = _scope(campaign, "C1")
    if args.supplier != scope["supplier_key"]:
        raise Refused("a capture is requested only for the C1 grant's supplier")
    target = app.collection.target_of(args.supplier, args.target_url)
    digest = target_digest(args.supplier, target)
    if digest not in scope["target_digests"]:
        raise Refused("a capture is requested only for a target the C1 grant names")
    if any(
        r["class"] == "target_identities" and r["subject_digest"] == digest
        for r in ledger.reservations("C1")
    ):
        raise Refused("each C1 target is requested once; a spent reservation stays spent")

    def effect(correlation: str) -> dict[str, Any]:
        request_id = app.capture_store.request(
            campaign_id=campaign.campaign_id,
            supplier_key=args.supplier,
            target=target,
            lifetime=timedelta(hours=args.lifetime_hours),
            requested_by=_actor(args),
            correlation_id=correlation,
        )
        return {"request_id": request_id, "target_digest": digest}

    # One unit of the C1 submission ceiling and of the target ceiling: the ordinary operator
    # collection that consumes this request is the submission.
    return _act(
        ledger,
        campaign,
        args,
        effect,
        intent={"target_digest": digest},
        reservations=[("collection_submissions", digest), ("target_identities", digest)],
    )


def _operator_scope(path: Path, actor: str) -> OperatorScope:
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
    view = _own_candidate(app, campaign, args.run)
    if _one(campaign, "FINALIZE_SAMPLE", collection_run_id=args.run) is not None:
        raise Refused("one approved sample per target run (the frozen C1 ceiling)")
    expected = json.loads(args.expected.read_text("utf-8"))
    if not isinstance(expected, dict):
        raise Refused("the expected facts are one JSON object the operator authored")
    scope = _operator_scope(args.scope, _actor(args))

    def effect(correlation: str) -> dict[str, Any]:
        sample = app.capture_store.finalize(
            args.run,
            campaign_id=campaign.campaign_id,
            scope=scope,
            expected=expected,
            stored_by=_actor(args),
            correlation_id=correlation,
        )
        return {
            "collection_run_id": args.run,
            "request_id": view.request_id,
            "sample_digest": sample.digest,
            "truncated": sample.truncated,
        }

    return _act(ledger, campaign, args, effect, intent={"collection_run_id": args.run})


def _validate(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    supplier = supplier_of(campaign)
    mine = {p["sample_digest"] for p in campaign.outcomes("FINALIZE_SAMPLE")}
    digests = sorted(set(args.sample))
    if not digests or not set(digests) <= mine:
        raise Refused("a campaign validates only the samples it finalized itself")
    record = app.adaptive_profiles.record(args.epr)
    if record.kind != "EXTRACTION_PROFILE" or record.supplier_key != supplier:
        raise Refused("a campaign validates only an EPR of its C1 grant's supplier")
    bundle = app.adaptive_profiles.load_bundle(args.epr)
    samples = [app.adaptive_validation.sample(d) for d in digests]
    result = validate(bundle, samples, negatives=SYNTHETIC_NEGATIVES)

    def effect(correlation: str) -> dict[str, Any]:
        run_id = app.adaptive_validation.record_run(
            bundle, result, samples, recorded_by=_actor(args), correlation_id=correlation
        )
        return {
            "epr": args.epr,
            "samples": digests,
            "run_id": run_id,
            "verdict": result.verdict.value,
        }

    return _act(ledger, campaign, args, effect, intent={"epr": args.epr, "samples": digests})


# ================================================================ C2


def _enable(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    scope = _scope(campaign, "C2")
    if args.epr != scope["epr_digest"]:
        raise Refused("enable names exactly the EPR of this campaign's C2 grant")
    bundle = app.adaptive_profiles.load_bundle(args.epr)
    samples = [app.adaptive_validation.sample(d) for d in scope["sample_digests"]]
    freshness = freshness_tuple(bundle, samples, None)

    def effect(correlation: str) -> dict[str, Any]:
        entry = app.shadow_switch.enable(
            args.epr, freshness, actor=_actor(args), reason="PHASE_C", correlation_id=correlation
        )
        return {"entry_id": entry, "epr": args.epr, "bundle_key": bundle_key_of(args.epr)}

    return _act(ledger, campaign, args, effect, intent={"epr": args.epr})


def _disable(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    if args.supplier != supplier_of(campaign):
        raise Refused("a campaign turns off only its own supplier's shadow")

    def effect(correlation: str) -> dict[str, Any]:
        entry = app.shadow_switch.disable(
            args.supplier, actor=_actor(args), reason="PHASE_C", correlation_id=correlation
        )
        return {"entry_id": entry}

    return _act(ledger, campaign, args, effect, intent={"supplier_key": args.supplier})


def _declare(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    scope = _scope(campaign, "C2")
    if args.epr != scope["epr_digest"]:
        raise Refused("declare names exactly the EPR of this campaign's C2 grant")
    if _one(campaign, "ENABLE", epr=args.epr) is None:
        raise Refused("a campaign declares a window only for the bundle it enabled itself")
    if args.supersedes is not None and _one(campaign, "DECLARE", window_id=args.supersedes) is None:
        raise Refused("a campaign supersedes only a window it declared itself")
    record = app.adaptive_profiles.record(args.epr)

    def effect(correlation: str) -> dict[str, Any]:
        window = app.shadow_evidence.declare(
            record.supplier_key,
            bundle_key_of(args.epr),
            actor=_actor(args),
            reason="PHASE_C",
            correlation_id=correlation,
            min_size=scope["window_min_size"],
            supersedes=args.supersedes,
        )
        return {"window_id": window, "epr": args.epr, "bundle_key": bundle_key_of(args.epr)}

    return _act(ledger, campaign, args, effect, intent={"epr": args.epr})


# ================================================================ C3 and C4


def _resolve(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    """The human operator's resolution: its artifact first, then the owner's event naming it."""
    window = _scope(campaign, "C3")["window_id"]
    if args.run not in app.shadow_evidence.window_evidence(window).states:
        raise Refused("a campaign resolves only a run eligible in its own C3 window")
    run = app.collection.run(args.run)

    def effect(correlation: str) -> dict[str, Any]:
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
        return {
            "collection_run_id": args.run,
            "window_id": window,
            "evidence_ref": reference,
            "count_as": state.count_as.value,
        }

    return _act(ledger, campaign, args, effect, intent={"collection_run_id": args.run})


def _own_window(campaign: Campaign, window: str) -> None:
    if window != _scope(campaign, "C4")["window_id"]:
        raise Refused("a campaign ends and closes only the window of its own C4 grant")


def _end(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    _own_window(campaign, args.window)

    def effect(correlation: str) -> dict[str, Any]:
        app.shadow_evidence.end(args.window, actor=_actor(args), reason="PHASE_C")
        return {"window_id": args.window}

    return _act(ledger, campaign, args, effect, intent={"window_id": args.window})


def _close(app: Container, ledger: CampaignLedger, campaign: Campaign, args: Any) -> Any:
    _own_window(campaign, args.window)
    closeouts: list[Any] = []

    def effect(correlation: str) -> dict[str, Any]:
        closeout = app.shadow_evidence.close(args.window, actor=_actor(args), reason="PHASE_C")
        directory = ledger.root / CLOSEOUTS
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{args.window}.json").open("x", encoding="utf-8", newline="\n") as f:
            f.write(canonical(closeout))
        closeouts.append(closeout)
        return {"window_id": args.window, "verdict": closeout["verdict"]}

    _act(ledger, campaign, args, effect, intent={"window_id": args.window})
    return closeouts[0]


HANDLERS: Mapping[str, Callable[[Container, CampaignLedger, Campaign, Any], Any]] = {
    "status": _status,
    "candidates": _candidates,
    "export-candidate": _export_candidate,
    "target-digest": _target_digest,
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
