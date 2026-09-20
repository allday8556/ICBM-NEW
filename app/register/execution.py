"""M5 PR-E: the registration execution owner — send gate, CREATE attempt, read-back, reconcile.

This is the safety state machine around one CREATE Intent. It owns no schedule and no status of
its own: durable business truth stays `RegistrationIntent` / `RegistrationAttempt` / the immutable
`RegistrationSnapshot` (PR-B), and durable scheduling stays the M0 `Job` / `JobAttempt` owner with
its `RetryPolicy` (ADR-0005). Nothing here is a second queue, a second retry clock or a second
authoritative state.

**Intent first, every run** (kickoff §5). The durable Intent decides what a run may do::

    CONFIRMED                        → idempotent no-op success
    UNKNOWN                          → no CREATE, ever; reconcile only
    SENT + APPLIED_PROVEN            → read-back / verify only; the CREATE count stays one
    PREPARED / FAILED(NOT_APPLIED)   → a CREATE may be considered, after every gate below

**The send gate** (kickoff §3). A frozen Snapshot is not permission to send forever, so
immediately before :meth:`RegistrationStore.start_attempt` the same final preflight is
re-evaluated under current truth and must be READY *with the fingerprint the Snapshot froze*; the
canonical account must still be bound; no overlapping SENT/UNKNOWN conflict may exist; the
prepared assets must still be the ones the Snapshot names; the wire projection must be sendable
and the provider CREATE must be adopted. Otherwise **no Attempt is opened, the Intent does not
move, no provider is called**, and the run fails closed with a deterministic code.

The preflight *inputs* travel with the job, in its own payload (``registration-send-request/v1``),
because they are the operator's frozen request — the same inputs the Snapshot was built from. What
is re-derived on every run is *current truth*: the account, the pins, M4 readiness, conflicts,
metadata and policy. That is the same comparison `RegistrationSnapshotBuilder` makes before it
freezes, so a drifted dependency can never reach a provider.

**Outcomes** (kickoff §4, ADR-0014 §9–§11). ``error_class`` and ``remote_outcome`` are independent:
a transient cause never implies a safe replay. The handler exposes a *retryable* job failure only
after the Attempt is durably ``NOT_APPLIED_PROVEN``; an ``UNKNOWN`` outcome always ends the job
non-retryably and waits for reconcile.

**Crash settlement** (kickoff §6). The CREATE job is declared non-idempotent, so an interrupted
attempt dead-letters as ``INTERRUPTED_OUTCOME_UNKNOWN``. :func:`settle_terminal` then closes the
still-open `RegistrationAttempt` as ``UNKNOWN`` and moves its Intent to UNKNOWN — never FAILED,
never a replay — and :func:`unsettled_jobs` makes a settlement that never happened discoverable,
so `JobRunner.reconcile_terminal_owners()` converges after any crash.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from app.connect.marketplace.capability import RemoteOutcome
from app.core.clock import Clock
from app.core.errors import AppError, ErrorClass
from app.jobs.models import JobState
from app.jobs.policy import RetryPolicy
from app.jobs.registry import JobContext, JobDefinition, TerminalJob
from app.jobs.service import JobService
from app.products.image_model import ImageAssetKind
from app.products.model import ReadinessStatus
from app.register.model import (
    IntentState,
    ResolutionEvidence,
    ResolvedBy,
    VerificationState,
)
from app.register.policy import DuplicateKeyKind, Provenance
from app.register.preflight import CapabilityReader, RegistrationPreflightService
from app.register.preparation import (
    CategoryConfirmation,
    CategorySelection,
    DetailComposition,
    DuplicateEvidence,
    DuplicateMatch,
    DuplicateVerdict,
    FieldValue,
    ListingValues,
    PreflightRequest,
    PreflightResult,
    PreparedAsset,
    UnitRequest,
)
from app.register.provider import (
    CreateSender,
    ReadbackComparator,
    ReadbackSource,
    ReconcileLookup,
    WireProjector,
)
from app.register.sanitize import (
    SECRET_MATERIAL,
    PayloadSanitationError,
    hex_digest,
    require_clean,
    safe_label,
    safe_provider_reference,
)
from app.register.store import AttemptRecord, IntentRecord, RegistrationStore

logger = logging.getLogger("icbm.register.execution")

CREATE_JOB_TYPE: Final = "register.create"
# Application policy, never a provider fact: a bounded number of attempts of the *same* Intent
# and Snapshot, with the deterministic backoff of the shared RetryPolicy (ADR-0005). Only an
# attempt already proven NOT_APPLIED_PROVEN ever reaches it (§9).
CREATE_POLICY: Final = RetryPolicy(max_attempts=3, base_delay_s=60.0, factor=2.0, max_delay_s=900.0)
SEND_REQUEST_VERSION: Final = "registration-send-request/v1"
EXECUTION_POLICY_VERSION: Final = "registration-execution-policy/v1"
# One API group is involved in a registration CREATE and its read-back, so the failure-budget
# scope of ADR-0014 §9 / v3.1 §11.2-§11.4 is this constant per marketplace and canonical account.
CREATE_ENDPOINT_GROUP: Final = "product_registration"
# The job states in which a CREATE job is still the queued work of its Intent (ADR-0005).
ACTIVE_JOB_STATES: Final = (
    JobState.QUEUED.value,
    JobState.RUNNING.value,
    JobState.RETRY_SCHEDULED.value,
)
# The Intent states a CREATE may be queued for: everything else is reconciled or done (§8-§10).
SENDABLE_STATES: Final = (IntentState.PREPARED, IntentState.FAILED)


# ---------------------------------------------------------------- failures


class ExecutionRefused(AppError):
    """The run may not proceed. No Attempt was opened and no provider was called."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        error_class: ErrorClass = ErrorClass.POLICY_BLOCKED,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, details=dict(details or {}))
        self.error_class = error_class


class AttemptFailed(AppError):
    """A CREATE attempt was opened, closed with a proven outcome, and the run failed.

    Its ``error_class`` is what the generic runner retries on, so it is the provider's class only
    when the attempt proved ``NOT_APPLIED_PROVEN``; anything else is reported non-retryably.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        error_class: ErrorClass,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, details=dict(details or {}))
        self.error_class = error_class


# ---------------------------------------------------------------- the send request codec


def encode_send_request(
    intent_id: str,
    request: PreflightRequest,
    prepared: Sequence[PreparedAsset],
    *,
    listing_identity: str,
    identity_generation: int,
) -> dict[str, Any]:
    """The job payload of one CREATE: the Intent and the operator's frozen preflight inputs.

    These are business values PR-C already sanitized; the codec refuses anything else, so the
    durable job row can never hold secret or URL-shaped material (ADR-0014 §15).
    """
    listing = request.listing
    payload: dict[str, Any] = {
        "version": SEND_REQUEST_VERSION,
        "intent_id": intent_id,
        # The unit this request was frozen as. A unit's identity carries a generation that moves
        # as soon as an Intent names it (§7), so a send gate re-evaluates under the frozen pair
        # rather than deriving the next one. The gate still checks the identity is the Snapshot's.
        "unit_identity": {
            "listing_identity": listing_identity,
            "generation": identity_generation,
        },
        "unit": {
            "draft_id": request.unit.draft_id,
            "expected_draft_revision": request.unit.expected_draft_revision,
            "item_ids": None if request.unit.item_ids is None else list(request.unit.item_ids),
        },
        "category": _encode_category(request.category),
        "listing": {
            "name": None if listing.name is None else _encode_field(listing.name),
            "tags": sorted(listing.tags),
            "attributes": {k: _encode_field(v) for k, v in sorted(listing.attributes.items())},
            "notices": {k: _encode_field(v) for k, v in sorted(listing.notices.items())},
            "options": {
                item: dict(sorted(values.items()))
                for item, values in sorted(listing.options.items())
            },
        },
        "detail": _encode_detail(request.detail),
    }
    # The same typed boundary the Snapshot builder uses (PR-C): business values are refused when
    # they carry secret or URL-shaped material, while a provider reference is judged by the
    # provider-reference rule — opaque, or plain https with no query, fragment or userinfo.
    require_clean(payload, "send_request")
    payload["duplicate_evidence"] = _encode_evidence(request.duplicate_evidence)
    payload["prepared_assets"] = [_encode_asset(asset) for asset in prepared]
    unsafe = [
        asset["provider_asset_ref"]
        for asset in payload["prepared_assets"]
        if not safe_provider_reference(str(asset["provider_asset_ref"]))
    ]
    evidence = payload["duplicate_evidence"]
    if evidence is not None:
        unsafe += [
            match["provider_listing_ref"]
            for match in evidence["matches"]
            if not safe_provider_reference(str(match["provider_listing_ref"]))
        ]
        safe_identity = safe_label(evidence["lookup_contract_version"]) and hex_digest(
            evidence["evidence_digest"]
        )
        if not safe_identity:
            unsafe.append(evidence["lookup_contract_version"])
    if unsafe:
        raise PayloadSanitationError(((SECRET_MATERIAL, "send_request.provider_reference"),))
    return payload


def decode_send_request(
    payload: Mapping[str, Any],
) -> tuple[PreflightRequest, tuple[PreparedAsset, ...]]:
    """The preflight inputs a CREATE job carries. A payload of another version is refused."""
    if payload.get("version") != SEND_REQUEST_VERSION:
        raise ExecutionRefused(
            "REGISTER_SEND_REQUEST_VERSION",
            f"unsupported send request version {payload.get('version')!r}",
            error_class=ErrorClass.VALIDATION,
        )
    unit = payload["unit"]
    listing = payload["listing"]
    request = PreflightRequest(
        unit=UnitRequest(
            draft_id=unit["draft_id"],
            expected_draft_revision=unit["expected_draft_revision"],
            item_ids=None if unit["item_ids"] is None else tuple(unit["item_ids"]),
        ),
        category=_decode_category(payload["category"]),
        listing=ListingValues(
            name=None if listing["name"] is None else _decode_field(listing["name"]),
            tags=frozenset(listing["tags"]),
            attributes={k: _decode_field(v) for k, v in listing["attributes"].items()},
            notices={k: _decode_field(v) for k, v in listing["notices"].items()},
            options={item: dict(values) for item, values in listing["options"].items()},
        ),
        detail=_decode_detail(payload["detail"]),
        duplicate_evidence=_decode_evidence(payload["duplicate_evidence"]),
    )
    return request, tuple(_decode_asset(a) for a in payload["prepared_assets"])


def frozen_unit_identity(payload: Mapping[str, Any]) -> tuple[str, int]:
    """The listing identity and generation this send request was frozen as."""
    identity = payload["unit_identity"]
    return str(identity["listing_identity"]), int(identity["generation"])


def _encode_field(value: FieldValue) -> dict[str, Any]:
    return {
        "value": value.value,
        "provenance": value.provenance.value,
        "detail_page_reference": value.detail_page_reference,
    }


def _decode_field(value: Mapping[str, Any]) -> FieldValue:
    return FieldValue(
        value=value["value"],
        provenance=Provenance(value["provenance"]),
        detail_page_reference=value["detail_page_reference"],
    )


def _encode_category(category: CategorySelection | None) -> dict[str, Any] | None:
    if category is None:
        return None
    return {
        "category_id": category.category_id,
        "mapping_revision": category.mapping_revision,
        "taxonomy_revision": category.taxonomy_revision,
        "confirmation": category.confirmation.value,
    }


def _decode_category(category: Mapping[str, Any] | None) -> CategorySelection | None:
    if category is None:
        return None
    return CategorySelection(
        category["category_id"],
        category["mapping_revision"],
        category["taxonomy_revision"],
        CategoryConfirmation(category["confirmation"]),
    )


def _encode_detail(detail: DetailComposition | None) -> dict[str, Any] | None:
    if detail is None:
        return None
    return {
        "composition_revision": detail.composition_revision,
        "body": detail.body,
        "sections": list(detail.sections),
    }


def _decode_detail(detail: Mapping[str, Any] | None) -> DetailComposition | None:
    if detail is None:
        return None
    return DetailComposition(
        composition_revision=detail["composition_revision"],
        body=detail["body"],
        sections=tuple(detail["sections"]),
    )


def _encode_evidence(evidence: DuplicateEvidence | None) -> dict[str, Any] | None:
    if evidence is None:
        return None
    return {
        "marketplace_key": evidence.marketplace_key,
        "marketplace_account_id": evidence.marketplace_account_id,
        "listing_identity": evidence.listing_identity,
        "lookup_contract_version": evidence.lookup_contract_version,
        "evidence_digest": evidence.evidence_digest,
        "verdict": evidence.verdict.value,
        "keys_checked": sorted(k.value for k in evidence.keys_checked),
        "matches": [
            {"key_kind": m.key_kind.value, "provider_listing_ref": m.provider_listing_ref}
            for m in evidence.matches
        ],
    }


def _decode_evidence(evidence: Mapping[str, Any] | None) -> DuplicateEvidence | None:
    if evidence is None:
        return None
    return DuplicateEvidence(
        marketplace_key=evidence["marketplace_key"],
        marketplace_account_id=evidence["marketplace_account_id"],
        listing_identity=evidence["listing_identity"],
        lookup_contract_version=evidence["lookup_contract_version"],
        evidence_digest=evidence["evidence_digest"],
        verdict=DuplicateVerdict(evidence["verdict"]),
        keys_checked=frozenset(DuplicateKeyKind(k) for k in evidence["keys_checked"]),
        matches=tuple(
            DuplicateMatch(DuplicateKeyKind(m["key_kind"]), m["provider_listing_ref"])
            for m in evidence["matches"]
        ),
    )


def _encode_asset(asset: PreparedAsset) -> dict[str, Any]:
    return {
        "asset_kind": asset.asset_kind.value,
        "sha256": asset.sha256,
        "derivation_id": asset.derivation_id,
        "asset_profile": asset.asset_profile,
        "candidate_fingerprint": asset.candidate_fingerprint,
        "provider_asset_ref": asset.provider_asset_ref,
    }


def _decode_asset(asset: Mapping[str, Any]) -> PreparedAsset:
    return PreparedAsset(
        asset_kind=ImageAssetKind(asset["asset_kind"]),
        sha256=asset["sha256"],
        derivation_id=asset["derivation_id"],
        asset_profile=asset["asset_profile"],
        candidate_fingerprint=asset["candidate_fingerprint"],
        provider_asset_ref=asset["provider_asset_ref"],
    )


# ---------------------------------------------------------------- execution policy


@dataclass(frozen=True)
class ExecutionPolicy:
    """Versioned application policy for registration execution — never a provider fact.

    The failure budget is *derived* from the durable attempt history of its scope
    (marketplace × canonical account × endpoint group), so it adds no second authoritative state:
    ``max_proven_failures`` consecutive proven failures in the scope stop further sends until an
    attempt in that scope succeeds or an operator intervenes. ``pause_classes`` are the causes
    that pause a scope outright rather than being retried per Item.
    """

    version: str = EXECUTION_POLICY_VERSION
    endpoint_group: str = CREATE_ENDPOINT_GROUP
    max_proven_failures: int = 3
    pause_classes: frozenset[ErrorClass] = frozenset({ErrorClass.AUTH, ErrorClass.POLICY_BLOCKED})

    def budget(
        self, attempts: Sequence[AttemptRecord], *, since: datetime | None = None
    ) -> "BudgetState":
        """The budget of one scope, read from its attempt history newest-first.

        ``since`` is the scope's last accepted recovery event: the CONNECT capability owner's
        ``auth_verified_at``, which moves only on a fresh authentication proof for that account.
        Attempts older than it are history, not pressure — that is what makes a pause
        **resumable** without a second authoritative state and without touching, deleting or
        rewriting a single past attempt (v3.1 §11.2). A proven success in the scope releases it
        too; nothing else does, and an unrelated capability change never does.
        """
        consecutive = 0
        paused_by: ErrorClass | None = None
        for attempt in attempts:
            if not attempt.finished:
                continue
            if since is not None and attempt.started_at is not None and attempt.started_at < since:
                break
            if attempt.outcome is RemoteOutcome.APPLIED_PROVEN:
                break
            if attempt.outcome is RemoteOutcome.UNKNOWN:
                # An unproven outcome is not a proven failure: it is reconciled (§10), and its
                # Intent already blocks its own conflict scope. It never spends this budget.
                continue
            if attempt.error_class is not None and attempt.error_class in self.pause_classes:
                paused_by = attempt.error_class
                break
            consecutive += 1
            if consecutive >= self.max_proven_failures:
                break
        return BudgetState(
            policy_version=self.version,
            endpoint_group=self.endpoint_group,
            consecutive_failures=consecutive,
            exhausted=consecutive >= self.max_proven_failures,
            paused_by=paused_by,
            reset_at=since,
        )


@dataclass(frozen=True)
class BudgetState:
    policy_version: str
    endpoint_group: str
    consecutive_failures: int
    exhausted: bool
    paused_by: ErrorClass | None
    # The accepted recovery event this budget was read against — a fresh authentication proof.
    # Everything older than it is history, not pressure.
    reset_at: datetime | None = None

    @property
    def sends_allowed(self) -> bool:
        return not self.exhausted and self.paused_by is None

    def canonical(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "endpoint_group": self.endpoint_group,
            "consecutive_failures": self.consecutive_failures,
            "exhausted": self.exhausted,
            "paused_by": None if self.paused_by is None else self.paused_by.value,
            "reset_at": None if self.reset_at is None else self.reset_at.isoformat(),
        }


# ---------------------------------------------------------------- results


@dataclass(frozen=True)
class ExecutionResult:
    """What one run did, for the caller and for the evidence line."""

    intent_id: str
    action: str
    intent_state: IntentState
    remote_outcome: RemoteOutcome | None = None
    verification: VerificationState | None = None
    marketplace_product_id: str | None = None
    attempt_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


class RegistrationExecutionService:
    """Executes one registration Intent under the M0 job owner (kickoff §1-§6)."""

    def __init__(
        self,
        *,
        registrations: RegistrationStore,
        preflight: RegistrationPreflightService,
        sender: CreateSender,
        readback: ReadbackSource,
        lookup: ReconcileLookup,
        capability: CapabilityReader,
        compare: ReadbackComparator,
        projection: WireProjector,
        clock: Clock,
        policy: ExecutionPolicy | None = None,
        actor: str = "worker",
    ) -> None:
        self._registrations = registrations
        self._preflight = preflight
        self._sender = sender
        self._readback = readback
        self._lookup = lookup
        self._capability = capability
        self._compare = compare
        self._projection = projection
        self._clock = clock
        self._policy = policy or ExecutionPolicy()
        self._actor = actor

    # ------------------------------------------------------------------ the job entry point

    def run(self, context: JobContext) -> ExecutionResult:
        """One run of the CREATE job: what the durable Intent allows, and nothing else."""
        payload = dict(context.payload)
        intent_id = str(payload.get("intent_id", ""))
        intent = self._intent(intent_id)
        correlation_id = context.correlation_id
        if intent.state is IntentState.CONFIRMED:
            # §5: replaying a confirmed registration is a no-op success, never a second CREATE.
            return ExecutionResult(intent_id, "NO_OP_CONFIRMED", intent.state)
        if intent.state is IntentState.UNKNOWN:
            raise ExecutionRefused(
                "REGISTER_UNKNOWN_REQUIRES_RECONCILE",
                "an UNKNOWN CREATE is reconciled with provider evidence, never replayed",
                error_class=ErrorClass.REVIEW_REQUIRED,
                details={"intent_id": intent_id},
            )
        applied = intent.remote_outcome is RemoteOutcome.APPLIED_PROVEN
        if intent.state is IntentState.SENT and applied:
            # §5: the listing exists; this run may only read it back.
            return self.verify(intent_id, correlation_id=correlation_id)
        return self._send(intent, payload, correlation_id)

    # ------------------------------------------------------------------ CREATE

    def _send(
        self, intent: IntentRecord, payload: Mapping[str, Any], correlation_id: str
    ) -> ExecutionResult:
        request, prepared = decode_send_request(payload)
        identity, generation = frozen_unit_identity(payload)
        snapshot = self._registrations.snapshot(intent.registration_snapshot_id)
        if snapshot is None:  # pragma: no cover - a Snapshot is never deleted
            raise ExecutionRefused("REGISTER_SNAPSHOT_MISSING", "the Snapshot is gone")
        if identity != snapshot.listing_identity:
            raise ExecutionRefused(
                "REGISTER_SEND_SCOPE_MISMATCH",
                "the send request names another unit than the Snapshot",
            )
        budget = self.budget(intent.marketplace_key, intent.marketplace_account_id)
        if not budget.sends_allowed:
            raise ExecutionRefused(
                "REGISTER_FAILURE_BUDGET_EXHAUSTED"
                if budget.exhausted
                else "REGISTER_SCOPE_PAUSED",
                "further sends in this marketplace/account/endpoint-group scope are stopped",
                details=budget.canonical(),
            )
        fresh = self._gate(intent, snapshot, request, prepared, generation)
        sanitized_request = self._sanitized_request(snapshot, fresh)
        with self._registrations.transaction() as unit:
            attempt = unit.start_attempt(
                intent.intent_id,
                sanitized_request=sanitized_request,
                sanitizer_profile_version=self._sanitizer_version(fresh),
                correlation_id=correlation_id,
            )
        handoff = self._sender.send(
            payload=self._payload_of(snapshot.registration_snapshot_id),
            idempotency_key=intent.idempotency_key,
            listing_identity=snapshot.listing_identity,
        )
        with self._registrations.transaction() as unit:
            settled = unit.finish_attempt(
                attempt.attempt_id,
                remote_outcome=handoff.remote_outcome,
                correlation_id=correlation_id,
                marketplace_product_id=handoff.marketplace_product_id,
                response_status=handoff.response_status,
                sanitized_response=handoff.sanitized_response,
                error_class=handoff.error_class,
                error_code=handoff.error_code,
            )
        return self._after_handoff(settled, attempt, handoff, correlation_id)

    def _after_handoff(
        self,
        intent: IntentRecord,
        attempt: AttemptRecord,
        handoff: Any,
        correlation_id: str,
    ) -> ExecutionResult:
        outcome = handoff.remote_outcome
        if outcome is RemoteOutcome.APPLIED_PROVEN:
            # §11: a 2xx is not a confirmation. The same run continues to the read-back.
            return self.verify(intent.intent_id, correlation_id=correlation_id)
        if outcome is RemoteOutcome.UNKNOWN:
            # §10: never retried automatically, whatever the cause was.
            raise AttemptFailed(
                "REGISTER_OUTCOME_UNKNOWN",
                "the CREATE outcome is unproven; reconcile before any resend",
                error_class=ErrorClass.REVIEW_REQUIRED,
                details={"intent_id": intent.intent_id, "attempt_id": attempt.attempt_id},
            )
        # NOT_APPLIED_PROVEN: only now may the provider's own class decide a job retry (§9).
        raise AttemptFailed(
            handoff.error_code or "REGISTER_CREATE_FAILED",
            "the CREATE was proven not applied",
            error_class=handoff.error_class or ErrorClass.UNKNOWN,
            details={"intent_id": intent.intent_id, "attempt_id": attempt.attempt_id},
        )

    # ------------------------------------------------------------------ the send gate (§3)

    def _gate(
        self,
        intent: IntentRecord,
        snapshot: Any,
        request: PreflightRequest,
        prepared: Sequence[PreparedAsset],
        identity_generation: int,
    ) -> PreflightResult:
        if not self._sender.available():
            raise ExecutionRefused(
                "REGISTER_CREATE_NOT_ADOPTED",
                "the provider CREATE contract is not adopted; nothing may be sent",
                details={"intent_id": intent.intent_id},
            )
        # Directly against the durable Snapshot, before anything is re-derived: the assets that
        # would be sent are exactly the sanitized provider references it froze (§3, B2).
        frozen_refs = self._frozen_refs(snapshot.registration_snapshot_id)
        if frozen_refs != tuple(sorted(a.provider_asset_ref for a in prepared)):
            raise ExecutionRefused(
                "REGISTER_SEND_ASSET_DRIFT",
                "the prepared provider assets are not the ones the Snapshot froze",
            )
        projection = self._projection(self._payload_of(snapshot.registration_snapshot_id))
        if not projection.sendable:
            raise ExecutionRefused(
                "REGISTER_WIRE_NOT_SENDABLE",
                "the wire projection is not sendable under the adopted provider contract",
                details={"gaps": list(projection.gaps)[:4]},
            )
        # The unit is the Snapshot's, not the next one: a unit's identity carries a generation
        # that moves as soon as an Intent names it (§7), so the gate re-evaluates under the
        # frozen identity. Everything else — account, pins, M4, conflicts, metadata, policy — is
        # re-derived from current truth.
        fresh = self._preflight.final(request, prepared, identity_generation=identity_generation)
        if fresh.status is not ReadinessStatus.READY:
            raise ExecutionRefused(
                "REGISTER_SEND_PREFLIGHT_NOT_READY",
                "the preflight is no longer READY under current truth",
                details={"status": fresh.status.value, "reasons": sorted(set(fresh.codes))[:8]},
            )
        if fresh.dependency_fingerprint != snapshot.preflight_fingerprint:
            raise ExecutionRefused(
                "REGISTER_SEND_FINGERPRINT_DRIFT",
                "a dependency moved since the Snapshot was frozen",
                details={"snapshot": snapshot.preflight_fingerprint},
            )
        return fresh

    # ------------------------------------------------------------------ read-back (§11)

    def verify(self, intent_id: str, *, correlation_id: str) -> ExecutionResult:
        """Read the listing back and compare it with the immutable Snapshot. Never a CREATE."""
        intent = self._intent(intent_id)
        if intent.marketplace_product_id is None:
            raise ExecutionRefused(
                "REGISTER_NO_PROVIDER_IDENTITY",
                "a read-back needs the provider product identity",
                error_class=ErrorClass.REVIEW_REQUIRED,
            )
        if not self._readback.available():
            raise ExecutionRefused(
                "REGISTER_READBACK_NOT_ADOPTED", "no read-back contract is adopted"
            )
        retained = self._readback.read(marketplace_product_id=intent.marketplace_product_id)
        comparison = self._compare.compare(
            self._payload_of(intent.registration_snapshot_id), retained
        )
        evidence = comparison.canonical()
        if comparison.verdict.value != "MATCH":
            with self._registrations.transaction() as unit:
                unit.record_mismatch(
                    intent_id,
                    comparison_contract_version=comparison.comparison_contract_version,
                    normalizer_version=comparison.normalizer_version,
                    sanitized_comparison=evidence,
                    actor=self._actor,
                    correlation_id=correlation_id,
                )
            # §11-§12: a mismatch or unreadable proof is never CONFIRMED and never a CREATE again.
            raise AttemptFailed(
                "REGISTER_READBACK_MISMATCH",
                f"the read-back is {comparison.verdict.value}, so the Intent is not confirmed",
                error_class=ErrorClass.REVIEW_REQUIRED,
                details={"verdict": comparison.verdict.value, "reasons": list(comparison.reasons)},
            )
        published_state = _published_state(evidence)
        if published_state is None:
            # ADR-0014 §11 compares the published state exactly. Nothing in the adopted read-back
            # proves it today (PR-D), so a confirmation cannot be claimed rather than invented.
            raise ExecutionRefused(
                "REGISTER_PUBLISHED_STATE_UNPROVEN",
                "the read-back proves no published state, so the listing is not confirmed",
                error_class=ErrorClass.REVIEW_REQUIRED,
                details={"intent_id": intent_id},
            )
        snapshot = self._registrations.snapshot(intent.registration_snapshot_id)
        assert snapshot is not None
        with self._registrations.transaction() as unit:
            registration = unit.confirm_registration(
                intent_id,
                comparison_contract_version=comparison.comparison_contract_version,
                normalizer_version=comparison.normalizer_version,
                sanitized_readback=evidence,
                published_state=published_state,
                option_ids={item.registration_item_key: None for item in snapshot.items},
                created_by=self._actor,
                correlation_id=correlation_id,
            )
        return ExecutionResult(
            intent_id,
            "CONFIRMED",
            IntentState.CONFIRMED,
            remote_outcome=RemoteOutcome.APPLIED_PROVEN,
            verification=VerificationState.PASS,
            marketplace_product_id=registration.marketplace_product_id,
        )

    # ------------------------------------------------------------------ reconcile (§10, §7)

    def reconcile(self, intent_id: str, *, correlation_id: str) -> ExecutionResult:
        """Settle an UNKNOWN with provider evidence only. An operator's word is never evidence."""
        intent = self._intent(intent_id)
        if intent.state is not IntentState.UNKNOWN:
            raise ExecutionRefused(
                "REGISTER_NOT_UNKNOWN",
                "only an UNKNOWN Intent is reconciled",
                error_class=ErrorClass.VALIDATION,
            )
        snapshot = self._registrations.snapshot(intent.registration_snapshot_id)
        assert snapshot is not None
        if intent.marketplace_product_id is not None and self._readback.available():
            retained = self._readback.read(marketplace_product_id=intent.marketplace_product_id)
            return self._resolve(
                intent_id,
                RemoteOutcome.APPLIED_PROVEN,
                ResolutionEvidence.PROVIDER_READ_BACK,
                retained,
                correlation_id,
                marketplace_product_id=intent.marketplace_product_id,
            )
        if not self._lookup.available():
            # §10 and PR-D: no adopted lookup contract, so the ambiguity stays an ambiguity. The
            # conflict scope is not freed and nothing is fabricated.
            raise ExecutionRefused(
                "REGISTER_RECONCILE_UNAVAILABLE",
                "no adopted provider lookup can resolve this UNKNOWN; it stays unresolved",
                error_class=ErrorClass.REVIEW_REQUIRED,
                details={"intent_id": intent_id, "listing_identity": snapshot.listing_identity},
            )
        found = self._lookup.find(
            marketplace_account_id=intent.marketplace_account_id,
            listing_identity=snapshot.listing_identity,
        )
        product_id = found.get("marketplace_product_id")
        if product_id:
            return self._resolve(
                intent_id,
                RemoteOutcome.APPLIED_PROVEN,
                ResolutionEvidence.PROVIDER_LOOKUP,
                found,
                correlation_id,
                marketplace_product_id=str(product_id),
            )
        if found.get("absence_proven"):
            return self._resolve(
                intent_id,
                RemoteOutcome.NOT_APPLIED_PROVEN,
                ResolutionEvidence.PROVIDER_LOOKUP,
                found,
                correlation_id,
            )
        raise ExecutionRefused(
            "REGISTER_RECONCILE_INCONCLUSIVE",
            "the lookup proved neither presence nor absence; the Intent stays UNKNOWN",
            error_class=ErrorClass.REVIEW_REQUIRED,
        )

    def _resolve(
        self,
        intent_id: str,
        outcome: RemoteOutcome,
        evidence_kind: ResolutionEvidence,
        evidence: Mapping[str, Any],
        correlation_id: str,
        *,
        marketplace_product_id: str | None = None,
    ) -> ExecutionResult:
        # The resolver names how the evidence was obtained, and the store's own constraint pairs
        # them: READ_BACK with PROVIDER_READ_BACK, LOOKUP with PROVIDER_LOOKUP. USER is never
        # used here, because an operator's word is not evidence (§10, B3).
        resolver = (
            ResolvedBy.READ_BACK
            if evidence_kind is ResolutionEvidence.PROVIDER_READ_BACK
            else ResolvedBy.LOOKUP
        )
        with self._registrations.transaction() as unit:
            settled = unit.resolve_unknown(
                intent_id,
                outcome=outcome,
                resolved_by=resolver,
                evidence_kind=evidence_kind,
                sanitized_evidence=dict(evidence),
                correlation_id=correlation_id,
                actor=self._actor,
                marketplace_product_id=marketplace_product_id,
            )
        return ExecutionResult(
            intent_id,
            f"RECONCILED_{outcome.value}",
            settled.state,
            remote_outcome=outcome,
            marketplace_product_id=marketplace_product_id,
        )

    # ------------------------------------------------------------------ crash settlement (§6)

    def settle_terminal(self, terminal: TerminalJob) -> None:
        """Close whatever this job left open. Idempotent, and never a replay.

        An attempt still open when its job can no longer run proves nothing about the provider, so
        it converges on UNKNOWN — never FAILED, never applied.
        """
        intent_id = _intent_of(terminal.target_ref)
        if intent_id is None:
            return
        for attempt in self._registrations.attempts(intent_id):
            if attempt.finished:
                continue
            with self._registrations.transaction() as unit:
                unit.finish_attempt(
                    attempt.attempt_id,
                    remote_outcome=RemoteOutcome.UNKNOWN,
                    correlation_id=terminal.correlation_id,
                    error_class=ErrorClass.UNKNOWN,
                    error_code=terminal.error_code or "INTERRUPTED_OUTCOME_UNKNOWN",
                )
            logger.warning(
                "register.attempt_settled_unknown",
                extra={"job_id": terminal.job_id, "attempt_id": attempt.attempt_id},
            )

    def unsettled_jobs(self, terminal_states: Sequence[str]) -> Sequence[str]:
        """The jobs whose registration attempt is still open, so a missed settlement is found."""
        return self._registrations.jobs_with_open_attempts(CREATE_JOB_TYPE, terminal_states)

    # ------------------------------------------------------------------ helpers

    def budget(self, marketplace_key: str, marketplace_account_id: str) -> BudgetState:
        """The failure budget of one scope: durable attempts, read against the scope's own reset.

        The reset is the CONNECT capability owner's ``auth_verified_at`` — the durable proof that
        this account authenticated again, which is the recovery an AUTH pause waits for. It moves
        on nothing else: a reviewed contract-freshness recording or a permission refresh leaves it
        where it was, so neither can release a paused scope. The other release is a proven success
        in the scope. No second state is invented, and the failed attempts themselves stay exactly
        as they were recorded.
        """
        attempts = self._registrations.scope_attempts(marketplace_key, marketplace_account_id)
        return self._policy.budget(attempts, since=self._scope_reset(marketplace_key))

    def _scope_reset(self, marketplace_key: str) -> datetime | None:
        """The one accepted recovery event of this scope: a **fresh authentication proof**.

        ``auth_verified_at`` moves only when the account authenticates again and no AUTHENTICATION
        overlay stands in the way (capability A1). It is deliberately *not* the capability row's
        ``updated_at``: that moves on unrelated changes — a reviewed contract-freshness recording,
        a permission-metadata refresh — none of which prove the registration scope recovered.
        """
        try:
            return self._capability.capability(marketplace_key).auth_verified_at
        except AppError:
            # A capability that cannot be read releases nothing: the budget then reads the whole
            # history, which is the fail-closed direction.
            return None

    def _intent(self, intent_id: str) -> IntentRecord:
        intent = self._registrations.intent(intent_id)
        if intent is None:
            raise ExecutionRefused(
                "REGISTER_INTENT_UNKNOWN",
                f"no Intent {intent_id!r}",
                error_class=ErrorClass.NOT_FOUND,
            )
        return intent

    def _payload_of(self, registration_snapshot_id: str) -> Mapping[str, Any]:
        payload = self._registrations.snapshot_payload(registration_snapshot_id)
        if payload is None:  # pragma: no cover - a Snapshot always has its payload
            raise ExecutionRefused("REGISTER_SNAPSHOT_MISSING", "the Snapshot payload is gone")
        return payload

    def _frozen_refs(self, registration_snapshot_id: str) -> tuple[str, ...]:
        payload = self._payload_of(registration_snapshot_id)
        return tuple(
            sorted(
                asset["provider_asset_ref"]
                for item in payload["items"]
                for asset in item["publication_assets"]
                if asset.get("provider_asset_ref") is not None
            )
        )

    def _sanitized_request(self, snapshot: Any, fresh: PreflightResult) -> dict[str, Any]:
        """The sanitized canonical request representation an attempt digest is taken over (§15)."""
        # Identities, digests and versions only: nothing operator-supplied reaches this digest,
        # because the operator's own values were sanitized when the job payload was encoded.
        request = {
            "send_request_version": SEND_REQUEST_VERSION,
            "registration_snapshot_id": snapshot.registration_snapshot_id,
            "payload_hash": snapshot.payload_hash,
            "preflight_fingerprint": fresh.dependency_fingerprint,
            "listing_identity": snapshot.listing_identity,
            "marketplace_account_id": snapshot.marketplace_account_id,
            "endpoint_group": self._policy.endpoint_group,
            "execution_policy_version": self._policy.version,
        }
        return request

    @staticmethod
    def _sanitizer_version(fresh: PreflightResult) -> str:
        return fresh.resolved.target.sanitizer_profile_version


def _published_state(evidence: Mapping[str, Any]) -> str | None:
    """The provider's published state, if the comparison evidence proves one."""
    normalized = evidence.get("normalized")
    value = normalized.get("published_state") if isinstance(normalized, Mapping) else None
    return value if isinstance(value, str) and value.strip() else None


def _intent_of(target_ref: str | None) -> str | None:
    if target_ref is None or not target_ref.startswith("intent:"):
        return None
    return target_ref.split(":", 1)[1]


def target_ref(intent_id: str) -> str:
    """The job target of one Intent: how a job and its registration rows find each other."""
    return f"intent:{intent_id}"


def create_job_definition(
    service: RegistrationExecutionService, *, retry_policy: RetryPolicy | None = None
) -> JobDefinition:
    """The CREATE job type: **non-idempotent**, with its terminal owner settlement (§6)."""
    return JobDefinition(
        job_type=CREATE_JOB_TYPE,
        handler=lambda context: _run(service, context),
        description="Register one prepared Snapshot with the marketplace and read it back",
        # A process that stops after a possible handoff may have created a listing.
        idempotent=False,
        retry_policy=retry_policy,
        on_terminal=service.settle_terminal,
        unsettled_owned_jobs=service.unsettled_jobs,
    )


def _run(service: RegistrationExecutionService, context: JobContext) -> None:
    result = service.run(context)
    logger.info(
        "register.execution",
        extra={
            "job_id": context.job_id,
            "intent_id": result.intent_id,
            "action": result.action,
            "intent_state": result.intent_state.value,
        },
    )


def enqueue_create(
    jobs: JobService,
    registrations: RegistrationStore,
    *,
    intent_id: str,
    request: PreflightRequest,
    frozen: PreflightResult,
) -> str:
    """Queue one CREATE job for an Intent, carrying the frozen preflight inputs and unit identity.

    ``frozen`` is the final READY result the Snapshot was frozen from, so the job carries exactly
    the inputs and the unit the Snapshot names — never a later preparation's.

    **One live job per Intent.** The job system owns when the next attempt runs, so a second job
    for the same Intent would run a CREATE while the first is still waiting out its backoff. If a
    job of this type is QUEUED, RUNNING or RETRY_SCHEDULED for the Intent, that job *is* the
    queued work and its id comes back unchanged; only once it is terminal, and the Intent is
    sendable again (PREPARED, or FAILED because a CREATE was proven not applied), may a new one
    be queued. An UNKNOWN Intent is never queueable: it is reconciled with provider evidence
    first, which is what moves it to FAILED (§10).
    """
    payload = encode_send_request(
        intent_id,
        request,
        frozen.prepared_assets,
        listing_identity=frozen.resolved.listing_identity,
        identity_generation=frozen.resolved.identity_generation,
    )
    # The check and the insert are **one** unit of work: two callers that both found no live job
    # would otherwise both queue one, and the second would run a CREATE while the first is still
    # waiting out its backoff. The job row joins this transaction (``session=``), so the worker is
    # notified only after it commits.
    with registrations.transaction() as unit:
        intent = unit.intent(intent_id)
        if intent is None:
            raise ExecutionRefused(
                "REGISTER_INTENT_UNKNOWN",
                f"no Intent {intent_id!r}",
                error_class=ErrorClass.NOT_FOUND,
            )
        if intent.state not in SENDABLE_STATES:
            raise ExecutionRefused(
                "REGISTER_INTENT_NOT_SENDABLE",
                "only a PREPARED Intent, or one proven not applied, may be queued",
                details={"intent_id": intent_id, "state": intent.state.value},
            )
        live = unit.active_job(CREATE_JOB_TYPE, intent_id, ACTIVE_JOB_STATES)
        if live is not None:
            logger.info(
                "register.create_job_reused", extra={"job_id": live, "intent_id": intent_id}
            )
            return live
        record = jobs.enqueue(
            CREATE_JOB_TYPE,
            payload=payload,
            target_ref=target_ref(intent_id),
            session=unit.session,
        )
    jobs.notify_worker()
    return record.job_id


__all__ = [
    "ACTIVE_JOB_STATES",
    "CREATE_ENDPOINT_GROUP",
    "CREATE_JOB_TYPE",
    "CREATE_POLICY",
    "EXECUTION_POLICY_VERSION",
    "SEND_REQUEST_VERSION",
    "AttemptFailed",
    "BudgetState",
    "ExecutionPolicy",
    "ExecutionRefused",
    "ExecutionResult",
    "RegistrationExecutionService",
    "create_job_definition",
    "decode_send_request",
    "encode_send_request",
    "enqueue_create",
    "frozen_unit_identity",
    "target_ref",
]
