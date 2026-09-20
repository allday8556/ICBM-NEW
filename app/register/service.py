"""REGISTER's read and action owner for the Registration Management surface (PR-F §B).

It owns no truth. Every value it returns is read from an owner that already holds it — the
registration store (Drafts, Snapshots, Intents, Attempts, registrations, execution scopes), the M4
pricing pin, the canonical account owner and the job system — and every action it accepts is
executed by the owner of that action: the execution service for a CREATE, a reconcile, a
read-back, and the store for a scope resume. Nothing here re-decides a price, a readiness, a
duplicate verdict, a budget or a capability, and nothing here writes a registration row.

**Action eligibility is a server verdict** (§22, PR-F §B). The screen receives each action with
`enabled` and a reason code, and the route calls straight into the same owner, which re-checks the
rule: an UNKNOWN Intent offers reconcile and never a CREATE retry; an applied-but-unverified one
offers the read-back only; an `AUTH` pause is never offered an operator resume. A client that
ignored every verdict could still not make the server act.

A re-send carries the **frozen** request the durable job already holds
(`registration-send-request/v1`), never rebuilt inputs: this owner has no authoring surface, and a
first send is prepared by the owner that evaluated the preflight (PR-C/PR-E).
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from app.connect.accounts import AccountBinding, MarketplaceAccountStore
from app.connect.marketplace.capability import (
    AuthStatus,
    RemoteOutcome,
    WriteScopeStatus,
    WriteStatus,
)
from app.core.errors import AppError, NotFoundError
from app.jobs.service import JobService
from app.register.canary import (
    AdoptionFacts,
    CanaryReadinessView,
    UnitFacts,
    evaluate,
)
from app.register.contracts import (
    ActionResult,
    ActionView,
    AttemptView,
    IntentView,
    ItemView,
    PreparationState,
    RegisterAction,
    RegisterOverview,
    ScopeBrakeView,
    SnapshotView,
    UnitView,
)
from app.register.execution import (
    ACTIVE_JOB_STATES,
    CREATE_JOB_TYPE,
    BudgetState,
    RegistrationExecutionService,
    queue_send_request,
    target_ref,
)
from app.register.model import (
    OPERATOR_RESUMABLE,
    IntentState,
    ScopePauseReason,
    VerificationState,
    registration_item_key,
)
from app.register.preflight import CapabilityReader
from app.register.store import (
    AttemptRecord,
    DraftRecord,
    IntentRecord,
    RegistrationStore,
    ScopeRecord,
    SnapshotRecord,
)

logger = logging.getLogger("icbm.register.service")

# Why an action is not available. Each is a server verdict the screen only renders.
NO_INTENT = "REGISTER_INTENT_ABSENT"
NOT_SENDABLE = "REGISTER_INTENT_NOT_SENDABLE"
JOB_QUEUED = "REGISTER_JOB_ALREADY_QUEUED"
SCOPE_PAUSED = "REGISTER_SCOPE_PAUSED"
BUDGET_EXHAUSTED = "REGISTER_FAILURE_BUDGET_EXHAUSTED"
NO_SEND_REQUEST = "REGISTER_SEND_REQUEST_ABSENT"
NOT_UNKNOWN = "REGISTER_NOT_UNKNOWN"
NOT_APPLIED = "REGISTER_NOT_APPLIED"
ALREADY_VERIFIED = "REGISTER_ALREADY_VERIFIED"
SCOPE_ACTIVE = "REGISTER_SCOPE_NOT_PAUSED"
RESUME_NOT_PERMITTED = "REGISTER_SCOPE_RESUME_NOT_PERMITTED"
ACCOUNT_NOT_BOUND = "REGISTER_ACCOUNT_NOT_BOUND"

# The execution scope this surface acts in, the one marketplace M5 registers to, and the default
# external-write mode (ADR-0014 §24). None of them is a decision this owner makes.
_CREATE_GROUP = "product_registration"
_MARKETPLACE = "smartstore"
DRY_RUN = "DRY_RUN"


class RegisterService:
    """The Registration Management read model and the operator actions it offers."""

    def __init__(
        self,
        *,
        registrations: RegistrationStore | None = None,
        execution: RegistrationExecutionService | None = None,
        accounts: MarketplaceAccountStore | None = None,
        jobs: JobService | None = None,
        capability: CapabilityReader | None = None,
        adoption: AdoptionFacts | None = None,
        marketplace_key: str = _MARKETPLACE,
        execution_mode: str = DRY_RUN,
    ) -> None:
        self._registrations = registrations
        self._execution = execution
        self._accounts = accounts
        self._jobs = jobs
        self._capability = capability
        self._adoption_source = adoption
        self._marketplace_key = marketplace_key
        self._execution_mode = execution_mode

    # ------------------------------------------------------------------ counts (screens)

    def registration_candidate_count(self) -> int:
        """Prepared units, never Products: a Draft that still holds an open Item (§3)."""
        return 0 if self._registrations is None else self._registrations.open_draft_count()

    def registration_count(self) -> int:
        """Verified registrations whose listing is not proven absent (§11, §14)."""
        return 0 if self._registrations is None else self._registrations.active_registration_count()

    # ------------------------------------------------------------------ the screen (§22)

    def overview(self, *, limit: int = 50) -> RegisterOverview:
        store = self._require_store()
        drafts = store.drafts(limit=limit)
        snapshots = {s.draft_id: s for s in self._snapshots_of(drafts)}
        intents = {i.registration_snapshot_id: i for i in store.intents(limit=limit * 2)}
        units = tuple(self._unit(draft, snapshots.get(draft.draft_id), intents) for draft in drafts)
        return RegisterOverview(
            registration_candidates_total=self.registration_candidate_count(),
            registrations_total=self.registration_count(),
            units=units,
            paused_scopes=tuple(self._scope_view(scope) for scope in store.paused_scopes()),
        )

    def canary_readiness(self, draft_id: str | None = None) -> CanaryReadinessView:
        """The derived, read-only plan result for a bounded real canary (§C, ADR-0014 §24).

        It authorizes nothing and writes nothing. Every missing proof is reported as missing —
        an unadopted endpoint as **not adopted**, never as absent or unnecessary — and a running
        application cannot prove its own checkout, so that requirement stays unproven here.
        """
        units = self.overview().units if draft_id is None else (self.unit(draft_id),)
        selected = [u for u in units if u.intent is not None] or list(units)
        unit = selected[0] if len(selected) == 1 else None
        capability = self._capability_facts()
        facts = UnitFacts(
            account_bound=unit is not None and unit.account_binding is AccountBinding.BOUND,
            auth_ready=capability[0],
            write_scope_proven=capability[1],
            intent_prepared=(
                unit is not None
                and unit.snapshot is not None
                and unit.intent is not None
                and unit.intent.state is IntentState.PREPARED
            ),
            requires_image_upload=(
                unit is not None and any(item.publication_assets for item in unit.items)
            ),
            unresolved_conflicts=0 if unit is None else len(unit.conflicting_intents),
            sends_allowed=unit is not None and unit.scope.sends_allowed,
            units_selected=len(selected),
        )
        return evaluate(
            facts,
            self._adoption(),
            execution_mode=self._execution_mode,
            write_status=capability[2],
        )

    def unit(self, draft_id: str) -> UnitView:
        store = self._require_store()
        draft = store.draft(draft_id)
        if draft is None:
            raise NotFoundError("REGISTER_DRAFT_NOT_FOUND", "no such registration draft")
        snapshot = next((s for s in self._snapshots_of([draft])), None)
        intents = {i.registration_snapshot_id: i for i in store.intents(limit=200)}
        return self._unit(draft, snapshot, intents)

    # ------------------------------------------------------------------ actions (§22, PR-F §B)

    def enqueue_create(self, intent_id: str) -> ActionResult:
        """Queue the CREATE the server already holds a frozen request for.

        The execution owner re-checks every rule it owns — the Intent must be sendable, only one
        live job may exist, and the send-time gate runs when the job runs. This owner adds no
        permission of its own.
        """
        store, jobs = self._require_store(), self._require_jobs()
        intent = self._require_intent(store, intent_id)
        payload = self._send_request(intent_id)
        if payload is None:
            raise AppError(NO_SEND_REQUEST, "no frozen send request exists for this Intent")
        budget = self._budget(intent)
        if not budget.sends_allowed:
            raise AppError(
                BUDGET_EXHAUSTED if budget.paused_by is None else SCOPE_PAUSED,
                "this execution scope is stopped",
                details=budget.canonical(),
            )
        job_id = queue_send_request(jobs, store, intent_id=intent_id, payload=payload)
        return ActionResult(
            action=RegisterAction.CREATE_ENQUEUE,
            intent_id=intent_id,
            job_id=job_id,
            intent_state=intent.state,
        )

    def reconcile(self, intent_id: str, *, correlation_id: str) -> ActionResult:
        """Resolve an UNKNOWN with provider evidence only (§10). Never a CREATE."""
        execution = self._require_execution()
        result = execution.reconcile(intent_id, correlation_id=correlation_id)
        return ActionResult(
            action=RegisterAction.RECONCILE,
            intent_id=intent_id,
            intent_state=result.intent_state,
            verification_state=result.verification,
        )

    def verify(self, intent_id: str, *, correlation_id: str) -> ActionResult:
        """Read the applied listing back and compare it with its immutable Snapshot (§11)."""
        execution = self._require_execution()
        result = execution.verify(intent_id, correlation_id=correlation_id)
        return ActionResult(
            action=RegisterAction.VERIFY,
            intent_id=intent_id,
            intent_state=result.intent_state,
            verification_state=result.verification,
        )

    def resume_scope(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        *,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> ActionResult:
        """The explicit audited release of a `POLICY` or `FAILURE_BUDGET` brake (§26).

        An `AUTH` pause is refused by the owner itself: it ends on a fresh authentication proof.
        """
        execution = self._require_execution()
        scope = execution.resume_scope(
            marketplace_key,
            marketplace_account_id,
            actor=actor,
            reason=reason,
            correlation_id=correlation_id,
        )
        return ActionResult(action=RegisterAction.RESUME_SCOPE, scope=self._scope_view(scope))

    # ------------------------------------------------------------------ views

    def _unit(
        self,
        draft: DraftRecord,
        snapshot: SnapshotRecord | None,
        intents: Mapping[str, IntentRecord],
    ) -> UnitView:
        store = self._require_store()
        intent = None if snapshot is None else intents.get(snapshot.registration_snapshot_id)
        attempts = () if intent is None else store.attempts(intent.intent_id)
        scope = self._scope_record(draft.marketplace_key, draft.marketplace_account_id)
        budget = self._budget_of(draft.marketplace_key, draft.marketplace_account_id, scope)
        binding = self._binding(draft.marketplace_key, draft.marketplace_account_id)
        registration = self._registration_of(intent)
        live = self._live_job(intent)
        return UnitView(
            draft_id=draft.draft_id,
            draft_revision=draft.draft_revision,
            listing_shape=draft.listing_shape,
            marketplace_key=draft.marketplace_key,
            marketplace_account_id=draft.marketplace_account_id,
            account_binding=binding,
            preparation=(
                PreparationState.DRAFTED
                if snapshot is None
                else PreparationState.INTENT_OPEN
                if intent is not None
                else PreparationState.SNAPSHOT_FROZEN
            ),
            items=self._items(draft, snapshot),
            snapshot=None if snapshot is None else _snapshot_view(snapshot),
            intent=(
                None
                if intent is None
                else IntentView(
                    intent_id=intent.intent_id,
                    state=intent.state,
                    remote_outcome=intent.remote_outcome,
                    verification_state=intent.verification_state,
                    marketplace_product_id=intent.marketplace_product_id,
                    idempotency_key=intent.idempotency_key,
                    attempts=tuple(_attempt_view(a) for a in attempts),
                    live_job_id=live,
                )
            ),
            registration_id=None if registration is None else registration[0],
            published_state=None if registration is None else registration[1],
            conflicting_intents=(
                ()
                if snapshot is None
                else store.conflicting_intents(snapshot.registration_snapshot_id)
            ),
            scope=self._scope_view(scope, budget=budget),
            actions=self._actions(intent, scope, budget, binding, live),
        )

    def _actions(
        self,
        intent: IntentRecord | None,
        scope: ScopeRecord,
        budget: BudgetState,
        binding: AccountBinding,
        live_job: str | None,
    ) -> tuple[ActionView, ...]:
        """What the server will accept for this unit now, each with its own reason code."""
        return (
            ActionView(
                action=RegisterAction.CREATE_ENQUEUE,
                enabled=self._create_reason(intent, budget, binding, live_job) is None,
                reason_code=self._create_reason(intent, budget, binding, live_job),
            ),
            ActionView(
                action=RegisterAction.RECONCILE,
                enabled=intent is not None and intent.state is IntentState.UNKNOWN,
                reason_code=(
                    None
                    if intent is not None and intent.state is IntentState.UNKNOWN
                    else NO_INTENT
                    if intent is None
                    else NOT_UNKNOWN
                ),
            ),
            ActionView(
                action=RegisterAction.VERIFY,
                enabled=self._verify_reason(intent) is None,
                reason_code=self._verify_reason(intent),
            ),
            ActionView(
                action=RegisterAction.RESUME_SCOPE,
                enabled=self._resume_reason(scope) is None,
                reason_code=self._resume_reason(scope),
            ),
        )

    def _create_reason(
        self,
        intent: IntentRecord | None,
        budget: BudgetState,
        binding: AccountBinding,
        live_job: str | None,
    ) -> str | None:
        if intent is None:
            return NO_INTENT
        if binding is not AccountBinding.BOUND:
            return ACCOUNT_NOT_BOUND
        if intent.state not in (IntentState.PREPARED, IntentState.FAILED):
            return NOT_SENDABLE
        if live_job is not None:
            return JOB_QUEUED
        if not budget.sends_allowed:
            return SCOPE_PAUSED if budget.paused_by is not None else BUDGET_EXHAUSTED
        if self._send_request(intent.intent_id) is None:
            return NO_SEND_REQUEST
        return None

    @staticmethod
    def _verify_reason(intent: IntentRecord | None) -> str | None:
        if intent is None:
            return NO_INTENT
        if intent.remote_outcome is not RemoteOutcome.APPLIED_PROVEN:
            return NOT_APPLIED
        if intent.verification_state is VerificationState.PASS:
            return ALREADY_VERIFIED
        return None

    @staticmethod
    def _resume_reason(scope: ScopeRecord) -> str | None:
        if not scope.paused:
            return SCOPE_ACTIVE
        if scope.pause_reason not in OPERATOR_RESUMABLE:
            return RESUME_NOT_PERMITTED
        return None

    def _items(self, draft: DraftRecord, snapshot: SnapshotRecord | None) -> tuple[ItemView, ...]:
        store = self._require_store()
        frozen = {} if snapshot is None else {i.item_id: i for i in snapshot.items}
        assets = _asset_counts(
            None if snapshot is None else store.snapshot_payload(snapshot.registration_snapshot_id)
        )
        views = []
        with store.reading() as unit:
            for item in draft.items:
                pin = unit.pricing_pin(item.pricing_snapshot_id)
                sent = frozen.get(item.item_id)
                views.append(
                    ItemView(
                        item_id=item.item_id,
                        product_group_id=item.product_group_id,
                        composition_signature=item.composition_signature,
                        ordinal=item.ordinal,
                        pricing_snapshot_id=item.pricing_snapshot_id,
                        sale_price_krw=None if pin is None else pin.final_sale_price_krw,
                        price_basis=None if pin is None else pin.price_basis.value,
                        registration_item_key=(
                            sent.registration_item_key
                            if sent is not None
                            else None
                            if snapshot is None
                            else registration_item_key(
                                snapshot.listing_identity,
                                item.product_group_id,
                                item.composition_signature,
                            )
                        ),
                        publication_assets=assets.get(item.item_id, 0),
                    )
                )
        return tuple(views)

    # ------------------------------------------------------------------ owner reads

    def _snapshots_of(self, drafts: Sequence[DraftRecord]) -> list[SnapshotRecord]:
        """The newest frozen Snapshot of each Draft, when one exists."""
        store = self._require_store()
        found: dict[str, SnapshotRecord] = {}
        for intent in store.intents(limit=400):
            if intent.registration_snapshot_id in found:
                continue
            snapshot = store.snapshot(intent.registration_snapshot_id)
            if snapshot is not None:
                found.setdefault(snapshot.draft_id, snapshot)
        return [found[d.draft_id] for d in drafts if d.draft_id in found]

    def _registration_of(self, intent: IntentRecord | None) -> tuple[str, str] | None:
        if intent is None or intent.state is not IntentState.CONFIRMED:
            return None
        store = self._require_store()
        for registration in store.registrations(limit=200):
            if registration.intent_id == intent.intent_id:
                return registration.registration_id, registration.published_state
        return None

    def _live_job(self, intent: IntentRecord | None) -> str | None:
        if intent is None or self._registrations is None:
            return None
        return self._registrations.active_job(CREATE_JOB_TYPE, intent.intent_id, ACTIVE_JOB_STATES)

    def _send_request(self, intent_id: str) -> Mapping[str, Any] | None:
        """The frozen send request a durable job already carries for this Intent."""
        if self._jobs is None:
            return None
        job = self._jobs.latest_job(CREATE_JOB_TYPE, target_ref(intent_id))
        if job is None:
            return None
        payload = self._jobs.payload(job.job_id)
        return payload or None

    def _capability_facts(self) -> tuple[bool, bool, str]:
        """CONNECT's own verdicts: authentication, write scope and the write status it reports."""
        if self._capability is None:
            return False, False, WriteStatus.UNVERIFIED.value
        try:
            view = self._capability.capability(self._marketplace_key)
        except AppError:
            return False, False, WriteStatus.UNVERIFIED.value
        return (
            view.auth is AuthStatus.READY,
            view.write_scope.status is WriteScopeStatus.READY,
            view.write.status.value,
        )

    def _adoption(self) -> Mapping[str, bool]:
        """What the marketplace adapter proves about its endpoints. Unknown means not adopted."""
        return {} if self._adoption_source is None else dict(self._adoption_source.adoption())

    def _binding(self, marketplace_key: str, marketplace_account_id: str) -> AccountBinding:
        if self._accounts is None:
            return AccountBinding.UNKNOWN_ACCOUNT
        return self._accounts.binding(marketplace_key, marketplace_account_id)

    def _scope_record(self, marketplace_key: str, marketplace_account_id: str) -> ScopeRecord:
        if self._execution is not None:
            return self._execution.scope(marketplace_key, marketplace_account_id)
        store = self._require_store()
        return store.execution_scope(marketplace_key, marketplace_account_id, _CREATE_GROUP)

    def _budget(self, intent: IntentRecord) -> BudgetState:
        return self._budget_of(
            intent.marketplace_key,
            intent.marketplace_account_id,
            self._scope_record(intent.marketplace_key, intent.marketplace_account_id),
        )

    def _budget_of(
        self, marketplace_key: str, marketplace_account_id: str, scope: ScopeRecord
    ) -> BudgetState:
        execution = self._require_execution()
        return execution.budget(marketplace_key, marketplace_account_id, scope=scope)

    def _scope_view(
        self, scope: ScopeRecord, *, budget: BudgetState | None = None
    ) -> ScopeBrakeView:
        state = budget or self._budget_of(
            scope.marketplace_key, scope.marketplace_account_id, scope
        )
        return ScopeBrakeView(
            marketplace_key=scope.marketplace_key,
            marketplace_account_id=scope.marketplace_account_id,
            endpoint_group=scope.endpoint_group,
            state=scope.state,
            pause_reason=scope.pause_reason,
            pause_error_class=scope.pause_error_class,
            paused_at=scope.paused_at,
            pause_policy_version=scope.pause_policy_version,
            resume_generation=scope.resume_generation,
            resumed_at=scope.resumed_at,
            resumed_by=scope.resumed_by,
            resume_reason=scope.resume_reason,
            sends_allowed=state.sends_allowed,
            consecutive_failures=state.consecutive_failures,
            budget_exhausted=state.exhausted,
            operator_resumable=scope.pause_reason in OPERATOR_RESUMABLE,
        )

    # ------------------------------------------------------------------ wiring guards

    def _require_store(self) -> RegistrationStore:
        if self._registrations is None:  # pragma: no cover - the container always wires it
            raise NotFoundError("REGISTER_NOT_WIRED", "the registration store is not wired")
        return self._registrations

    def _require_execution(self) -> RegistrationExecutionService:
        if self._execution is None:  # pragma: no cover - the container always wires it
            raise NotFoundError("REGISTER_NOT_WIRED", "the execution owner is not wired")
        return self._execution

    def _require_jobs(self) -> JobService:
        if self._jobs is None:  # pragma: no cover - the container always wires it
            raise NotFoundError("REGISTER_NOT_WIRED", "the job owner is not wired")
        return self._jobs

    def _require_intent(self, store: RegistrationStore, intent_id: str) -> IntentRecord:
        intent = store.intent(intent_id)
        if intent is None:
            raise NotFoundError("REGISTER_INTENT_NOT_FOUND", "no such registration intent")
        return intent


def _asset_counts(payload: Mapping[str, Any] | None) -> dict[str, int]:
    """How many publication assets the frozen payload sends for each Item (§6)."""
    if payload is None:
        return {}
    items = payload.get("items")
    if not isinstance(items, list):  # pragma: no cover - a Snapshot always has its items
        return {}
    counts: dict[str, int] = {}
    for item in items:
        if isinstance(item, Mapping):
            sent = item.get("publication_assets")
            counts[str(item.get("item_id"))] = len(sent) if isinstance(sent, list) else 0
    return counts


def _snapshot_view(snapshot: SnapshotRecord) -> SnapshotView:
    return SnapshotView(
        registration_snapshot_id=snapshot.registration_snapshot_id,
        listing_identity=snapshot.listing_identity,
        preflight_fingerprint=snapshot.preflight_fingerprint,
        payload_hash=snapshot.payload_hash,
        draft_revision=snapshot.draft_revision,
    )


def _attempt_view(attempt: AttemptRecord) -> AttemptView:
    return AttemptView(
        attempt_no=attempt.attempt_no,
        outcome=attempt.outcome,
        resolved_outcome=attempt.resolved_outcome,
        resolved_by=attempt.resolved_by,
        resolution_evidence_kind=attempt.resolution_evidence_kind,
        error_class=attempt.error_class,
        error_code=attempt.error_code,
        ambiguous_result=attempt.ambiguous_result,
        started_at=attempt.started_at,
    )


_ = ScopePauseReason  # the vocabulary this surface renders, re-exported by the contracts
