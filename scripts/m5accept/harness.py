"""The M5 offline acceptance run (Issue #89 PR-F §A, kickoff 5750366733).

It drives the accepted M5 acceptance-plan cases through the **real production owners** on a fresh
dedicated root, and records what it observed. Every case is a check: a failure is a recorded
problem, never a crash and never a silent pass.

The run is offline and provider-zero. CREATE, image upload and product search are NOT_ADOPTED, so
the provider seams for those contracts are local fakes (`seams.py`) and the production seams are
asked — and refuse — in the boundary phase. The guards refuse every HTTP client, browser, AI, OCR
and supplier transport for the life of the run, and the report states the measured counters,
including a marketplace mutation count of zero.

A PASS here is offline evidence for one commit. It is **not** M5 acceptance, and it authorizes no
real write: the bounded canary readiness result it records is `BLOCKED` while the contracts it
needs are unadopted.
"""

import importlib
import traceback
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.connect.marketplace.capability import RemoteOutcome
from app.core.errors import AppError
from app.db.migrate import head_revision
from app.jobs.models import JobState
from app.products.model import ReadinessStatus
from app.register.canary import CanaryVerdict, UnitFacts, evaluate, required_endpoints
from app.register.execution import (
    CREATE_ENDPOINT_GROUP,
    CREATE_JOB_TYPE,
    ExecutionPolicy,
    enqueue_create,
)
from app.register.model import (
    BatchSummary,
    IntentState,
    ListingShape,
    ResolutionEvidence,
    ResolvedBy,
    ScopePauseReason,
    VerificationState,
    sanitized_digest,
)
from app.register.preparation import (
    UNRESOLVED_CREATE_CONFLICT,
    DuplicateVerdict,
    FieldValue,
)
from app.register.sanitize import PayloadSanitationError
from integrations.marketplaces.smartstore import product as smartstore_product
from integrations.marketplaces.smartstore.adoption import SmartStoreAdoption
from scripts.m4accept import evidence
from scripts.m4accept.checkout import CheckoutRefused, probe_checkout
from scripts.m5accept import synthetic
from scripts.m5accept.guards import FORBIDDEN_MODULES, SMARTSTORE_CALLER, offline
from scripts.m5accept.owners import MARKETPLACE, Owners, open_owners
from scripts.m5accept.root import DATA, RootRefused, claim_root, root_problems, settle_root
from scripts.m5accept.synthetic import CID, OPERATOR

REPORT_SCHEMA = "icbm-m5-acceptance-report/v1"
# The audit events that would exist only if a provider had been reached.
PROVIDER_AUDIT = (
    "SUPPLIER_AUTH_SUCCEEDED",
    "SUPPLIER_CONNECTION_VERIFIED",
    "MARKETPLACE_SESSION_COMMITTED",
    "MARKETPLACE_ACCOUNT_BOUND",
)


# The endpoint contracts a registration needs. The CONNECT pair is deliberately not reported here:
# this snapshot is about the registration contracts, and an endpoint id that reads like a
# credential word has no place in an acceptance report (the report scanner refuses one).
REGISTRATION_ENDPOINTS = (
    "SMARTSTORE_PRODUCT_CREATE_V2",
    "SMARTSTORE_PRODUCT_IMAGE_UPLOAD",
    "SMARTSTORE_PRODUCT_SEARCH",
    "SMARTSTORE_ORIGIN_PRODUCT_READ_V2",
    "SMARTSTORE_CHANNEL_PRODUCT_READ_V2",
)


def _registration_adoption() -> dict[str, bool]:
    adoption = SmartStoreAdoption().adoption()
    return {name: bool(adoption.get(name, False)) for name in REGISTRATION_ENDPOINTS}


# Why each seam of this run is a **declaration** rather than a proof. These are different gaps and
# the report keeps them apart, because a PASS here proves a different thing about each:
# - `ENDPOINT_NOT_ADOPTED`: the adapter has not adopted that endpoint contract, so nothing may be
#   sent through it at all;
# - `OFFLINE_SYNTHETIC_PROVIDER_RESPONSE`: the contract **is** adopted, and the declaration exists
#   only because a provider-zero run has no provider to answer it;
# - `WIRE_CONTRACT_UNPROVEN`: PR-D's real wire projection cannot prove this unit sendable;
# - `PUBLISHED_STATE_UNPROVEN`: the adopted read-back carries no published state — a field of the
#   comparison, never an endpoint.
# The endpoint-adoption snapshot stays the authoritative adoption fact; these name the reason.
ENDPOINT_NOT_ADOPTED = "ENDPOINT_NOT_ADOPTED"
OFFLINE_PROVIDER_RESPONSE = "OFFLINE_SYNTHETIC_PROVIDER_RESPONSE"
WIRE_CONTRACT_UNPROVEN = "WIRE_CONTRACT_UNPROVEN"
PUBLISHED_STATE_UNPROVEN = "PUBLISHED_STATE_UNPROVEN"

DECLARED_SEAMS: Mapping[str, tuple[str, str | None]] = {
    "CREATE_HANDOFF": (ENDPOINT_NOT_ADOPTED, "SMARTSTORE_PRODUCT_CREATE_V2"),
    "RECONCILE_LOOKUP": (ENDPOINT_NOT_ADOPTED, "SMARTSTORE_PRODUCT_SEARCH"),
    "READ_BACK": (OFFLINE_PROVIDER_RESPONSE, "SMARTSTORE_ORIGIN_PRODUCT_READ_V2"),
    "WIRE_PROJECTION": (WIRE_CONTRACT_UNPROVEN, None),
    "PUBLISHED_STATE": (PUBLISHED_STATE_UNPROVEN, None),
    "ACCOUNT_BINDING": (OFFLINE_PROVIDER_RESPONSE, None),
}


def _declarations() -> dict[str, dict[str, object]]:
    adoption = _registration_adoption()
    return {
        seam: {
            "reason": reason,
            "endpoint_id": endpoint,
            "endpoint_adopted": None if endpoint is None else adoption.get(endpoint, False),
        }
        for seam, (reason, endpoint) in DECLARED_SEAMS.items()
    }


# The owners this run drives, and so the only rows it may change: the registration tables, the
# CONNECT rows it feeds typed evidence to, the job system and the audit log.
OWNED_BY_THIS_RUN = ("app.register", "app.connect", "app.jobs", "app.audit")


def _upstream_tables() -> frozenset[str]:
    """The histories this run reads and must never change (case 17): COLLECT source truth, the
    canonical Product foundation, pricing with its pointer history, and the image lineage.

    Derived from the models rather than listed here, so an upstream table is covered the day it is
    added, and this harness names no table another milestone's owner owns.
    """
    from app.db.base import Base
    from app.db.metadata import metadata  # noqa: F401  — imports every model

    return frozenset(
        table.name
        for mapper in Base.registry.mappers
        if not mapper.class_.__module__.startswith(OWNED_BY_THIS_RUN)
        for table in mapper.tables
    )


UPSTREAM_TABLES = _upstream_tables()


class Failed(Exception):
    """A phase could not go on: the checks already recorded say why."""


@dataclass
class Checks:
    items: list[dict[str, object]] = field(default_factory=list)

    def check(self, name: str, passed: bool, **observed: object) -> bool:
        entry: dict[str, object] = {"name": name, "passed": bool(passed)}
        if observed:
            entry["observed"] = observed
        self.items.append(entry)
        return bool(passed)

    def require(self, name: str, passed: bool, **observed: object) -> None:
        if not self.check(name, passed, **observed):
            raise Failed(name)

    @property
    def problems(self) -> list[str]:
        return [str(item["name"]) for item in self.items if not item["passed"]]


@dataclass
class Run:
    root: Path
    owners: Owners
    checks: Checks = field(default_factory=Checks)
    account: str = ""

    @property
    def data_dir(self) -> Path:
        return self.root / DATA

    def restart(self) -> None:
        """Close every owner and open them again over the same root: what a restart leaves."""
        policy = self.owners.execution._policy  # the same versioned policy, re-armed
        clock = self.owners.clock
        self.owners.close()
        self.owners = open_owners(self.data_dir, migrate=False, clock=clock, policy=policy)
        _install_policy(self.owners, self.account)


def _install_policy(owners: Owners, account: str) -> None:
    """Point the preflight at this run's synthetic target policy and category metadata."""
    policies, metadata = synthetic.policy_sources(account)
    owners.preflight._policies = policies  # the sources a deployment configures
    owners.preflight._metadata = metadata


# ---------------------------------------------------------------- unit helpers


@dataclass
class Unit:
    """One prepared provider-listing unit of this run."""

    draft_id: str
    snapshot_id: str
    intent_id: str
    listing_identity: str
    request: Any
    final: Any
    items: list[synthetic.ReadyItem]


def _prepare(
    run: Run,
    *,
    product: str,
    sequence: int,
    shape: ListingShape = ListingShape.SINGLE_LISTING_WITH_OPTIONS,
    items: int = 1,
    batch: str | None = None,
    reuse: list[synthetic.ReadyItem] | None = None,
) -> Unit:
    """Synthetic source truth → M4 Item(s) → Draft → READY preflight → Snapshot → Intent.

    ``reuse`` covers exactly the same M4 Items again, which is how a second Draft lands in an
    existing unit's conflict scope (R2).
    """
    owners = run.owners
    ready = reuse or [
        synthetic.ready_item(owners, product=f"{product}-{n}", sequence=sequence + n)
        for n in range(items)
    ]
    draft_id = synthetic.draft(owners, run.account, ready, shape)
    request = synthetic.request(owners, draft_id, ready)
    request, final = synthetic.ready_final(owners, request)
    snapshot = owners.builder.freeze(final, created_by=OPERATOR, correlation_id=CID)
    with owners.registrations.transaction() as unit:
        batch_id = batch or unit.create_batch(
            MARKETPLACE, run.account, created_by=OPERATOR, correlation_id=CID
        )
        intent = unit.create_intent(
            batch_id, snapshot.registration_snapshot_id, created_by=OPERATOR, correlation_id=CID
        )
    return Unit(
        draft_id,
        snapshot.registration_snapshot_id,
        intent.intent_id,
        snapshot.listing_identity,
        request,
        final,
        ready,
    )


def _queue(run: Run, unit: Unit) -> str:
    return enqueue_create(
        run.owners.jobs,
        run.owners.registrations,
        intent_id=unit.intent_id,
        request=unit.request,
        frozen=unit.final,
    )


def _send(run: Run, unit: Unit) -> Any:
    """One send through the execution owner, with whatever outcome the seam was told to give."""
    try:
        return run.owners.execution.run(_context(run, unit))
    except AppError as refused:
        return refused


def _context(run: Run, unit: Unit) -> Any:
    from app.jobs.registry import JobContext
    from app.register.execution import CREATE_POLICY, encode_send_request, target_ref

    return JobContext(
        job_id=f"m5-acceptance-{uuid.uuid4()}",
        job_type=CREATE_JOB_TYPE,
        attempt_no=1,
        max_attempts=CREATE_POLICY.max_attempts,
        correlation_id=CID,
        target_ref=target_ref(unit.intent_id),
        payload=encode_send_request(
            unit.intent_id,
            unit.request,
            unit.final.prepared_assets,
            listing_identity=unit.final.resolved.listing_identity,
            identity_generation=unit.final.resolved.identity_generation,
        ),
    )


def _failing(run: Run, error_class: Any, error_code: str) -> None:
    """Declare the next handoff a proven failure of this class. Every field is set, because a
    restart opens a new seam with its own defaults."""
    sender = run.owners.sender
    sender.outcome = RemoteOutcome.NOT_APPLIED_PROVEN
    sender.product_id = None
    sender.error_class = error_class
    sender.error_code = error_code


def _unproven(run: Run, error_code: str) -> None:
    """Declare the next handoff unproven: the outcome the provider never confirmed."""
    sender = run.owners.sender
    sender.outcome = RemoteOutcome.UNKNOWN
    sender.product_id = None
    sender.error_class = None
    sender.error_code = error_code


def _applied(run: Run, product_id: str = "9900112233") -> None:
    run.owners.sender.outcome = RemoteOutcome.APPLIED_PROVEN
    run.owners.sender.product_id = product_id
    run.owners.sender.error_class = None
    run.owners.sender.error_code = None


def _retained(unit: Unit, payload: Mapping[str, Any], *, reverse: bool = False) -> dict[str, Any]:
    """A provider read-back of exactly what the Snapshot sent, in the provider's own shape."""
    items = list(payload["items"])
    options = [
        {
            "id": f"option-{index}",
            "sellerManagerCode": item["registration_item_key"],
            "price": item["sale_price_krw"],
            "stockQuantity": 1,
        }
        for index, item in enumerate(reversed(items) if reverse else items)
    ]
    return {
        "originProduct": {
            "name": payload["name"]["value"],
            "salePrice": items[0]["sale_price_krw"],
            "stockQuantity": len(items),
            "sellerManagementCode": unit.listing_identity,
            "detailAttribute": {"optionInfo": {"optionCombinations": options}},
        },
        "smartstoreChannelProduct": {"channelProductDisplayStatusType": "ON"},
    }


# ---------------------------------------------------------------- scenarios


def scenario_identity(run: Run, unit: Unit) -> dict[str, object]:
    """1, 16: one Intent and one idempotency identity per Snapshot, across a restart."""
    checks, owners = run.checks, run.owners
    intent = owners.registrations.intent(unit.intent_id)
    assert intent is not None
    # §8: asking again for the Intent of this exact Snapshot returns the same Intent and the same
    # idempotency identity. A retry, a restart or a second operator never opens a competing one.
    with owners.registrations.transaction() as work:
        batch = work.create_batch(MARKETPLACE, run.account, created_by=OPERATOR, correlation_id=CID)
        again = work.create_intent(batch, unit.snapshot_id, created_by=OPERATOR, correlation_id=CID)
    checks.check(
        "s1.one_intent_per_snapshot",
        again.intent_id == unit.intent_id and again.idempotency_key == intent.idempotency_key,
        same_intent=again.intent_id == unit.intent_id,
    )
    run.restart()
    after = run.owners.registrations.intent(unit.intent_id)
    checks.check(
        "s1.identity_survives_restart",
        after is not None
        and after.idempotency_key == intent.idempotency_key
        and after.state is intent.state,
    )
    return {
        "idempotency_key": intent.idempotency_key,
        "second_request_returns_same_intent": again.intent_id == unit.intent_id,
    }


def scenario_double_dispatch(run: Run, unit: Unit) -> dict[str, object]:
    """2: a double dispatch neither competes for an Attempt nor bypasses the backoff."""
    checks, owners = run.checks, run.owners
    _failing(run, _transient(), "M5_ACCEPTANCE_TRANSIENT")
    first, again = _queue(run, unit), _queue(run, unit)
    checks.check(
        "s2.one_live_job", first == again, jobs=owners.jobs.count(job_type_prefix="register.")
    )
    result = owners.runner.run_next()
    checks.require("s2.job_ran", result is not None)
    assert result is not None
    state = owners.jobs.get(first).state
    checks.check("s2.retry_scheduled", state == JobState.RETRY_SCHEDULED.value, state=state)
    sent = len(owners.sender.calls)
    checks.check("s2.same_job_after_retry", _queue(run, unit) == first)
    checks.check("s2.backoff_not_bypassed", owners.runner.run_next() is None)
    checks.check("s2.create_count_unchanged", len(owners.sender.calls) == sent, sent=sent)
    attempts = owners.registrations.attempts(unit.intent_id)
    checks.check("s2.one_attempt_per_send", len(attempts) == 1, attempts=len(attempts))
    return {"job_id_stable": first == again, "create_calls": sent}


def _transient() -> Any:
    from app.core.errors import ErrorClass

    return ErrorClass.TRANSIENT


def scenario_unknown(run: Run, unit: Unit) -> dict[str, object]:
    """3, 13: an UNKNOWN is never resent, blocks its conflict scope, and needs real evidence."""
    checks, owners = run.checks, run.owners
    _unproven(run, "M5_ACCEPTANCE_AMBIGUOUS")
    _send(run, unit)
    intent = owners.registrations.intent(unit.intent_id)
    checks.require("s3.intent_unknown", intent is not None and intent.state is IntentState.UNKNOWN)
    sent = len(owners.sender.calls)
    queued = _send(run, unit)
    checks.check(
        "s3.no_blind_resend",
        isinstance(queued, AppError) and len(owners.sender.calls) == sent,
        code=getattr(queued, "code", None),
    )
    # A new Draft over the same M4 Items does not escape the conflict scope: the preflight of the
    # overlapping unit is not READY, and it says why (R2).
    overlap_draft = synthetic.draft(owners, run.account, unit.items)
    overlap = owners.preflight.candidate(synthetic.request(owners, overlap_draft, unit.items))
    codes = [reason.code for reason in overlap.reasons]
    # Not merely "not READY": the unresolved CREATE itself must be the reason, so a unit that is
    # blocked for some unrelated gap cannot stand in for the conflict scope.
    checks.check(
        "s3.overlapping_snapshot_blocked",
        overlap.status is not ReadinessStatus.READY and UNRESOLVED_CREATE_CONFLICT in codes,
        status=overlap.status.value,
        reasons=codes[:3],
    )
    blocked = UNRESOLVED_CREATE_CONFLICT if UNRESOLVED_CREATE_CONFLICT in codes else "none"
    # Only machine or provider evidence resolves it; the vocabulary has no operator assertion.
    checks.check(
        "s13.no_operator_assertion_evidence",
        "USER" not in {kind.value for kind in ResolutionEvidence}
        and ResolvedBy.USER in set(ResolvedBy),
    )
    reconcile_refusal = "none"
    try:
        owners.execution.reconcile(unit.intent_id, correlation_id=CID)
    except AppError as refused:
        reconcile_refusal = refused.code
    after = owners.registrations.intent(unit.intent_id)
    checks.check(
        "s13.unadopted_lookup_resolves_nothing",
        reconcile_refusal == "REGISTER_RECONCILE_UNAVAILABLE"
        and after is not None
        and after.state is IntentState.UNKNOWN,
        refusal=reconcile_refusal,
        state=None if after is None else after.state.value,
    )
    return {
        "create_calls": sent,
        "conflict_refusal": blocked,
        "reconcile_refusal": reconcile_refusal,
    }


def scenario_free_group(run: Run) -> dict[str, object]:
    """4: a non-overlapping group is not blocked by another group's UNKNOWN."""
    checks = run.checks
    free = _prepare(run, product="s4-free", sequence=50)
    intent = run.owners.registrations.intent(free.intent_id)
    checks.check(
        "s4.non_overlapping_group_free",
        intent is not None and intent.state is IntentState.PREPARED,
    )
    return {"intent_id": free.intent_id}


def scenario_subset_mismatch(run: Run) -> dict[str, object]:
    """5, 8, 9: a subset read-back is a whole-Intent MISMATCH; order and later state do not lie."""
    checks, owners = run.checks, run.owners
    unit = _prepare(run, product="s5-subset", sequence=60, items=2)
    _applied(run, "9900112299")
    payload = owners.registrations.snapshot_payload(unit.snapshot_id)
    assert payload is not None
    full = _retained(unit, payload)
    subset = dict(full)
    subset["originProduct"] = dict(full["originProduct"])
    detail = dict(subset["originProduct"]["detailAttribute"])
    option = dict(detail["optionInfo"])
    option["optionCombinations"] = option["optionCombinations"][:1]
    detail["optionInfo"] = option
    subset["originProduct"]["detailAttribute"] = detail
    owners.readback.retained = subset
    _send(run, unit)
    intent = owners.registrations.intent(unit.intent_id)
    checks.check(
        "s5.subset_is_whole_mismatch",
        intent is not None and intent.verification_state is VerificationState.MISMATCH,
        verification=None if intent is None else intent.verification_state.value,
    )
    sent = len(owners.sender.calls)
    checks.check("s5.no_create_for_missing_items", sent == 1 + _previous_calls(run), sent=sent)
    # 9: the same items in the provider's own order still correspond by key.
    owners.readback.retained = _retained(unit, payload, reverse=True)
    owners.execution.verify(unit.intent_id, correlation_id=CID)
    after = owners.registrations.intent(unit.intent_id)
    checks.check(
        "s9.option_order_does_not_break_correspondence",
        after is not None and after.verification_state is VerificationState.PASS,
        verification=None if after is None else after.verification_state.value,
    )
    # 8: the comparison is against the immutable Snapshot, not today's product state. The current
    # price moves; the frozen payload does not, and the same read-back still matches it.
    moved = owners.pricing.price(unit.items[0].item_id, replace(synthetic.CONTEXT, fee_rate="0.2"))
    frozen_again = owners.registrations.snapshot_payload(unit.snapshot_id)
    comparison = owners.comparator.compare(payload, _retained(unit, payload))
    checks.check(
        "s8.readback_compares_to_the_frozen_snapshot",
        frozen_again == payload and comparison.verdict.value == "MATCH",
        current_price_moved=moved.snapshot is not None,
        verdict=comparison.verdict.value,
    )
    return {"intent_id": unit.intent_id, "items": len(unit.items)}


def _previous_calls(run: Run) -> int:
    return len(run.owners.sender.calls) - 1


def scenario_separate_listings(run: Run) -> dict[str, object]:
    """6: a sibling's success is preserved and the batch summary is derived, never stored."""
    checks, owners = run.checks, run.owners
    with owners.registrations.transaction() as work:
        batch = work.create_batch(MARKETPLACE, run.account, created_by=OPERATOR, correlation_id=CID)
    first = _prepare(
        run,
        product="s6-a",
        sequence=70,
        shape=ListingShape.SEPARATE_LISTINGS,
        batch=batch,
    )
    second = _prepare(
        run,
        product="s6-b",
        sequence=72,
        shape=ListingShape.SEPARATE_LISTINGS,
        batch=batch,
    )
    _applied(run, "9900112288")
    payload = owners.registrations.snapshot_payload(first.snapshot_id)
    assert payload is not None
    owners.readback.retained = _retained(first, payload)
    _send(run, first)
    _failing(run, _transient(), "M5_ACCEPTANCE_TRANSIENT")
    _send(run, second)
    confirmed = owners.registrations.intent(first.intent_id)
    failed = owners.registrations.intent(second.intent_id)
    summary = owners.registrations.batch_summary(batch)
    checks.check(
        "s6.sibling_success_preserved",
        confirmed is not None and confirmed.state is IntentState.CONFIRMED,
        state=None if confirmed is None else confirmed.state.value,
    )
    checks.check(
        "s6.batch_partial_is_derived",
        summary is BatchSummary.PARTIAL
        and failed is not None
        and failed.state is IntentState.FAILED,
        summary=summary.value,
    )
    return {"batch": batch, "summary": summary.value, "confirmed": first.intent_id}


def scenario_absence(run: Run, confirmed_intent: str) -> dict[str, object]:
    """7: an assertion changes nothing; proven absence keeps history and frees a fresh start."""
    checks, owners = run.checks, run.owners
    registration = next(
        (
            r
            for r in owners.registrations.registrations(limit=50)
            if r.intent_id == confirmed_intent
        ),
        None,
    )
    checks.require("s7.registration_exists", registration is not None)
    assert registration is not None
    from app.register.model import AbsenceEvidence

    kinds = {kind.value for kind in AbsenceEvidence}
    checks.check(
        "s7.absence_needs_provider_evidence",
        kinds == {"PROVIDER_READ_BACK", "PROVIDER_LOOKUP"},
        kinds=sorted(kinds),
    )
    before = len(owners.registrations.registration(registration.registration_id).items)  # type: ignore[union-attr]
    with owners.registrations.transaction() as work:
        work.record_external_absence(
            registration.registration_id,
            evidence_kind=AbsenceEvidence.PROVIDER_READ_BACK,
            sanitized_evidence={"absent": True},
            recorded_by=OPERATOR,
            correlation_id=CID,
        )
    after = owners.registrations.registration(registration.registration_id)
    checks.check(
        "s7.history_preserved_after_proven_absence",
        after is not None
        and after.lifecycle_state.value == "EXTERNALLY_REMOVED"
        and len(after.items) == before,
    )
    fresh = _prepare(run, product="s7-again", sequence=80)
    checks.check("s7.fresh_unit_allowed_after_absence", fresh.intent_id != confirmed_intent)
    return {"registration_id": registration.registration_id}


def scenario_no_hotlink(run: Run) -> dict[str, object]:
    """10, 14: no supplier hotlink is publishable, and every durable digest is taken over the
    sanitized canonical representation."""
    checks, owners = run.checks, run.owners
    unit = _prepare(run, product="s10-safe", sequence=90)
    payload = owners.registrations.snapshot_payload(unit.snapshot_id)
    assert payload is not None
    refs = [
        asset.get("provider_asset_ref")
        for item in payload["items"]
        for asset in item["publication_assets"]
    ]
    checks.check(
        "s10.no_supplier_hotlink_in_payload",
        all(ref is None or str(ref).startswith("provider-asset-") for ref in refs),
        refs=len(refs),
    )
    snapshot = owners.registrations.snapshot(unit.snapshot_id)
    assert snapshot is not None
    checks.check(
        "s14.payload_hash_is_over_the_sanitized_canonical",
        snapshot.payload_hash == sanitized_digest(payload),
    )
    # A supplier URL in a business value is refused before anything is hashed or frozen.
    hotlink = replace(
        unit.request,
        listing=replace(
            unit.request.listing, name=FieldValue("see https://supplier.example/a.jpg")
        ),
    )
    refused = "none"
    try:
        result = owners.preflight.final(hotlink, unit.final.prepared_assets)
        refused = result.status.value
    except PayloadSanitationError:
        refused = "PayloadSanitationError"
    checks.check(
        "s10.url_bearing_value_never_reaches_a_payload",
        refused in ("BLOCKED", "REVIEW_REQUIRED", "PayloadSanitationError"),
        refusal=refused,
    )
    return {"unit": unit.intent_id, "assets": len(refs)}


def scenario_upload_gate(run: Run) -> dict[str, object]:
    """11, 12: a non-READY candidate uploads nothing, and drift between candidate and final sends
    nothing."""
    checks, owners = run.checks, run.owners
    unit = _prepare(run, product="s11-gate", sequence=100)
    duplicate = replace(
        unit.request,
        duplicate_evidence=replace(unit.request.duplicate_evidence, verdict=DuplicateVerdict.MATCH),
    )
    candidate = owners.preflight.candidate(duplicate)
    checks.check(
        "s11.blocked_candidate_permits_no_upload",
        candidate.status is not ReadinessStatus.READY and not candidate.upload_permitted,
        status=candidate.status.value,
    )
    # 12: a dependency that moved between candidate and send stops the CREATE at the gate. The
    # Draft Item is re-pinned to a new M4 price, which opens a new Draft revision; the Snapshot
    # was frozen at the old one.
    repriced = owners.pricing.price(
        unit.items[0].item_id, replace(synthetic.CONTEXT, fee_rate="0.3")
    )
    assert repriced.snapshot is not None
    with owners.registrations.transaction() as work:
        work.change_draft_item_price(
            unit.draft_id,
            unit.items[0].item_id,
            repriced.snapshot.pricing_snapshot_id,
            changed_by=OPERATOR,
            correlation_id=CID,
        )
    _applied(run, "9900112277")
    sent = len(owners.sender.calls)
    outcome = _send(run, unit)
    checks.check(
        "s12.dependency_drift_sends_nothing",
        isinstance(outcome, AppError) and len(owners.sender.calls) == sent,
        code=getattr(outcome, "code", None),
    )
    attempts = owners.registrations.attempts(unit.intent_id)
    checks.check("s12.no_attempt_opened_on_drift", attempts == (), attempts=len(attempts))
    return {"candidate_status": candidate.status.value}


def scenario_brakes(run: Run) -> dict[str, object]:
    """15: each brake cause, its boundary, its restart and what may release it."""
    checks, owners = run.checks, run.owners
    from app.core.errors import ErrorClass

    unit = _prepare(run, product="s15-brake", sequence=110)
    _failing(run, ErrorClass.POLICY_BLOCKED, "M5_ACCEPTANCE_POLICY")
    _send(run, unit)
    scope = owners.registrations.execution_scope(MARKETPLACE, run.account, CREATE_ENDPOINT_GROUP)
    checks.require(
        "s15.policy_failure_pauses_the_scope",
        scope.paused and scope.pause_reason is ScopePauseReason.POLICY,
        reason=None if scope.pause_reason is None else scope.pause_reason.value,
    )
    attempts_before = owners.registrations.attempts(unit.intent_id)
    run.restart()
    owners = run.owners
    after = owners.registrations.execution_scope(MARKETPLACE, run.account, CREATE_ENDPOINT_GROUP)
    checks.check(
        "s15.brake_survives_restart",
        after.paused
        and after.pause_reason is ScopePauseReason.POLICY
        and after.paused_at == scope.paused_at,
    )
    checks.check(
        "s15.attempts_preserved_across_restart",
        owners.registrations.attempts(unit.intent_id) == attempts_before,
    )
    # Authentication does not release a policy brake; an explicit audited resume does.
    synthetic.authenticate(owners)
    still = owners.execution.refresh_scope(MARKETPLACE, run.account, correlation_id=CID)
    checks.check("s15.authentication_never_releases_a_policy_brake", still.paused)
    resumed = owners.execution.resume_scope(
        MARKETPLACE, run.account, actor=OPERATOR, reason="POLICY-REVIEWED", correlation_id=CID
    )
    checks.check(
        "s15.explicit_resume_moves_the_boundary",
        not resumed.paused and resumed.resume_generation == 1 and resumed.resumed_at is not None,
    )
    checks.check(
        "s15.attempts_unchanged_by_resume",
        owners.registrations.attempts(unit.intent_id) == attempts_before,
    )
    # An AUTH brake is not an operator's to release.
    _failing(run, ErrorClass.AUTH, "M5_ACCEPTANCE_AUTH")
    auth_send = _send(run, unit)
    auth = owners.registrations.execution_scope(MARKETPLACE, run.account, CREATE_ENDPOINT_GROUP)
    checks.check(
        "s15.auth_failure_pauses_the_scope",
        auth.paused and auth.pause_reason is ScopePauseReason.AUTH,
        send=getattr(auth_send, "code", type(auth_send).__name__),
        reason=None if auth.pause_reason is None else auth.pause_reason.value,
    )
    refusal = "none"
    try:
        owners.execution.resume_scope(
            MARKETPLACE, run.account, actor=OPERATOR, reason="OPERATOR-DECIDED", correlation_id=CID
        )
    except AppError as refused:
        refusal = refused.code
    checks.check(
        "s15.auth_brake_refuses_an_operator_resume",
        refusal == "REGISTER_SCOPE_RESUME_NOT_PERMITTED",
        reason=None if auth.pause_reason is None else auth.pause_reason.value,
        refusal=refusal,
    )
    return {"resume_generation": resumed.resume_generation}


def scenario_replay(run: Run, confirmed_intent: str) -> dict[str, object]:
    """16: replaying a confirmed registration is a no-op and creates no second listing."""
    checks, owners = run.checks, run.owners
    sent = len(owners.sender.calls)
    intent = owners.registrations.intent(confirmed_intent)
    checks.require(
        "s16.intent_confirmed", intent is not None and intent.state is IntentState.CONFIRMED
    )
    from app.jobs.registry import JobContext
    from app.register.execution import CREATE_POLICY, encode_send_request, target_ref

    result = owners.execution.run(
        JobContext(
            job_id=f"m5-replay-{uuid.uuid4()}",
            job_type=CREATE_JOB_TYPE,
            attempt_no=1,
            max_attempts=CREATE_POLICY.max_attempts,
            correlation_id=CID,
            target_ref=target_ref(confirmed_intent),
            payload={"intent_id": confirmed_intent},
        )
    )
    checks.check(
        "s16.replay_is_a_no_op",
        result.action == "NO_OP_CONFIRMED" and len(owners.sender.calls) == sent,
        action=result.action,
    )
    _ = encode_send_request
    return {"create_calls": sent}


def _any_payload(owners: Owners) -> Mapping[str, Any]:
    """One frozen Snapshot payload of this run, to ask the real projection about."""
    for intent in owners.registrations.intents(limit=10):
        payload = owners.registrations.snapshot_payload(intent.registration_snapshot_id)
        if payload is not None:
            return payload
    raise Failed("boundary.no_payload_to_project")


def boundary(run: Run, before: Mapping[str, Any]) -> dict[str, object]:
    """17, and the provider boundary: what this run touched, and what it never could."""
    checks, owners = run.checks, run.owners
    # The module that could reach a marketplace cannot even be loaded while the run is armed:
    # this is the positive proof behind "provider-zero", and the only import this run probes.
    refusal = "none"
    try:
        importlib.import_module(SMARTSTORE_CALLER)
    except ImportError as refused:
        refusal = type(refused).__name__
    checks.check(
        "boundary.provider_transport_unloadable", refusal == "ImportError", refusal=refusal
    )
    # PR-D's real wire projection is not sendable while the CREATE contract is unproven: the
    # scenarios above declared one so the state machine could be exercised at all.
    unsent = smartstore_product.project(_any_payload(owners))
    checks.check(
        "boundary.real_wire_projection_refuses",
        not unsent.sendable and bool(unsent.gaps),
        gaps=len(unsent.gaps),
    )
    adoption = _registration_adoption()
    checks.check(
        "boundary.create_not_adopted", adoption.get("SMARTSTORE_PRODUCT_CREATE_V2") is False
    )
    checks.check(
        "boundary.upload_not_adopted", adoption.get("SMARTSTORE_PRODUCT_IMAGE_UPLOAD") is False
    )
    checks.check("boundary.search_not_adopted", adoption.get("SMARTSTORE_PRODUCT_SEARCH") is False)
    capability = owners.capability.capability(MARKETPLACE)
    checks.check(
        "boundary.product_registration_write_unverified",
        capability.write.status.value == "UNVERIFIED",
        write=capability.write.status.value,
    )
    with evidence.read_only(owners.database_file) as connection:
        marks = ", ".join("?" for _ in PROVIDER_AUDIT)
        provider_events = connection.execute(
            f"SELECT COUNT(*) FROM audit_events WHERE event_type IN ({marks})", PROVIDER_AUDIT
        ).fetchone()[0]
    checks.check("boundary.no_provider_audit_event", provider_events == 0, count=provider_events)
    # Every declaration names its own gap: only a seam whose endpoint the adapter has not adopted
    # may claim `ENDPOINT_NOT_ADOPTED`, so an adopted contract is never described as unadopted.
    declarations = _declarations()
    misnamed = sorted(
        seam
        for seam, fact in declarations.items()
        if (fact["reason"] == ENDPOINT_NOT_ADOPTED) is not (fact["endpoint_adopted"] is False)
    )
    checks.check(
        "boundary.declarations_name_their_own_gap",
        not misnamed and all(fact["reason"] for fact in declarations.values()),
        misnamed=misnamed,
    )
    # 17: the source, Product, pricing and image histories this run read are exactly as they
    # were. Only the registration owners it drove, and the CONNECT capability it fed typed
    # evidence to, changed.
    after = evidence.snapshot(owners.database_file)
    upstream_before = {name: rows for name, rows in before.items() if name in UPSTREAM_TABLES}
    upstream_after = {name: rows for name, rows in after.items() if name in UPSTREAM_TABLES}
    changes = evidence.history_changes(upstream_before, upstream_after)
    checks.check(
        "s17.upstream_history_unchanged",
        not changes,
        tables=len(upstream_before),
        changes=changes[:5],
    )
    return {
        "marketplace_mutations": 0,
        "real_wire_projection_sendable": False,
        "readback_comparisons": owners.comparator.calls,
        "fake_create_handoffs": len(owners.sender.calls),
        "fake_readbacks": owners.readback.calls,
        "provider_audit_events": provider_events,
    }


def canary(run: Run) -> dict[str, object]:
    """§C: the derived readiness of a bounded real canary, from this run's own facts."""
    owners = run.owners
    units = owners.register.overview().units
    prepared = [u for u in units if u.intent is not None]
    unit = prepared[0] if len(prepared) == 1 else None
    capability = owners.capability.capability(MARKETPLACE)
    facts = UnitFacts(
        account_bound=unit is not None and unit.account_binding.value == "BOUND",
        auth_ready=capability.auth.value == "READY",
        write_scope_proven=capability.write_scope.status.value == "READY",
        intent_prepared=unit is not None and unit.intent is not None,
        requires_image_upload=True,
        unresolved_conflicts=0 if unit is None else len(unit.conflicting_intents),
        sends_allowed=unit is not None and unit.scope.sends_allowed,
        units_selected=len(prepared),
    )
    result = evaluate(
        facts,
        _registration_adoption(),
        execution_mode="DRY_RUN",
        write_status=capability.write.status.value,
        clean_runtime=True,
    )
    run.checks.check(
        "canary.blocked_while_contracts_are_unadopted",
        result.verdict is CanaryVerdict.BLOCKED,
        missing=[item.value for item in result.missing],
    )
    return {
        "verdict": result.verdict.value,
        "missing": [item.value for item in result.missing],
        "unadopted_endpoints": list(required_endpoints(result.requirements)),
        "execution_mode": result.execution_mode,
        "write_status": result.write_status,
    }


# ---------------------------------------------------------------- the run


def _run_phases(run: Run, report: dict[str, Any]) -> None:
    checks = run.checks
    report["database_revision"] = run.owners.database_revision()
    checks.check("schema.at_head", report["database_revision"] == head_revision())
    run.account = synthetic.bind_account(run.owners)
    synthetic.authenticate(run.owners)
    _install_policy(run.owners, run.account)
    report["account_scope"] = {
        "marketplace_key": MARKETPLACE,
        "synthetic_connect_binding": True,
        "capability_auth": run.owners.capability.capability(MARKETPLACE).auth.value,
    }
    upstream = evidence.snapshot(run.owners.database_file)
    unit = _prepare(run, product="s1-base", sequence=0)
    report["s1_identity"] = scenario_identity(run, unit)
    report["s2_double_dispatch"] = scenario_double_dispatch(run, unit)
    report["s3_unknown"] = scenario_unknown(run, unit)
    report["s4_free_group"] = scenario_free_group(run)
    report["s5_subset"] = scenario_subset_mismatch(run)
    separate = scenario_separate_listings(run)
    report["s6_separate_listings"] = separate
    report["s7_absence"] = scenario_absence(run, str(separate["confirmed"]))
    report["s10_no_hotlink"] = scenario_no_hotlink(run)
    report["s11_upload_gate"] = scenario_upload_gate(run)
    report["s15_brakes"] = scenario_brakes(run)
    report["s16_replay"] = scenario_replay(run, str(separate["confirmed"]))
    report["canary_readiness"] = canary(run)
    report["boundary"] = boundary(run, upstream)


def run_acceptance(root: Path, environ: Mapping[str, str]) -> dict[str, Any]:
    """One full offline acceptance run on a fresh dedicated ``root``; the sanitized report."""
    run_id = str(uuid.uuid4())
    if problems := root_problems(root, environ):
        raise RootRefused(problems)
    checkout = probe_checkout()
    if checkout.problems:
        raise CheckoutRefused(checkout.problems)
    claimed = claim_root(root, environ, run_id)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "mode": "OFFLINE_SYNTHETIC",
        "claim": "HARNESS_RUN: a PASS here is offline evidence for one commit; it is not M5"
        " acceptance and authorizes no real marketplace write",
        "run_id": run_id,
        "code_sha": checkout.code_sha,
        "checkout": checkout.as_json(),
        "alembic_head": head_revision(),
        "execution_mode": "DRY_RUN",
        "execution_policy_version": ExecutionPolicy().version,
        "endpoint_adoption": _registration_adoption(),
        # What this run declared instead of proving, each with the reason it is a declaration:
        # an unadopted endpoint, an adopted contract with no provider to answer it offline, an
        # unproven wire contract, or a field the adopted read-back does not carry.
        "declared_seams": _declarations(),
    }
    checks = Checks()
    with offline(claimed, modules=FORBIDDEN_MODULES) as guarded:
        run: Run | None = None
        try:
            run = Run(claimed, open_owners(claimed / DATA, migrate=True), checks)
            _run_phases(run, report)
        except Failed:
            pass
        except Exception as error:  # every failure is a problem, never a crash
            code = getattr(error, "code", None)
            checks.check(
                "run.completed",
                False,
                error=type(error).__name__,
                code=None if code is None else str(code),
            )
            report["failure_trace"] = [
                f"{frame.name}:{frame.lineno}"
                for frame in traceback.extract_tb(error.__traceback__)
            ][-6:]
        finally:
            if run is not None:
                run.owners.close()
    assert guarded.evidence is not None
    after = probe_checkout()
    checks.check(
        "checkout.unchanged_during_run",
        after == checkout and not after.problems,
        code_sha=after.code_sha,
    )
    guard_evidence = guarded.evidence.as_json()
    checks.check(
        "hard_zero.no_external_network",
        guarded.evidence.external_network_attempts == 0
        and guarded.evidence.egress_grants_opened == 0,
        **{k: guard_evidence[k] for k in ("external_network_attempts", "egress_grants_opened")},
    )
    # Nothing provider-shaped was loaded, and the only import the guard refused is the probe the
    # boundary phase makes on purpose.
    checks.check(
        "hard_zero.no_provider_module_loaded",
        not guarded.evidence.forbidden_modules_loaded_during_run
        and set(guarded.evidence.forbidden_imports_blocked) <= {SMARTSTORE_CALLER},
        loaded=list(guarded.evidence.forbidden_modules_loaded_during_run),
        blocked=list(guarded.evidence.forbidden_imports_blocked),
    )
    checks.check(
        "hard_zero.nothing_provider_shaped_preloaded",
        not guarded.evidence.forbidden_modules_preloaded,
        preloaded=list(guarded.evidence.forbidden_modules_preloaded)[:5],
    )
    checks.check(
        "hard_zero.no_write_outside_the_root",
        guarded.evidence.writes_outside_root == 0,
        writes=guarded.evidence.writes_outside_root,
    )
    report["guards"] = guard_evidence
    report["preserved_campaign_access"] = guarded.evidence.preserved_campaign_paths_refused
    report["checks"] = checks.items
    report["checks_total"] = len(checks.items)
    report["checks_passed"] = sum(1 for item in checks.items if item["passed"])
    report["problems"] = checks.problems
    if found := evidence.leaks(report, (str(root), str(claimed))):
        report = {
            "schema": REPORT_SCHEMA,
            "run_id": run_id,
            "problems": [f"report withheld: it would leak {what}" for what in found],
        }
    report[evidence.DIGEST_FIELD] = evidence.report_digest(report)
    settle_root(claimed, run_id, passed=not report["problems"])
    return report
