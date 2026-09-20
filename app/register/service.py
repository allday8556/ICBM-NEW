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
    AssetView,
    AttemptView,
    CategoryView,
    FieldStateView,
    IntentView,
    ItemView,
    PreflightView,
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
    decode_send_request,
    frozen_unit_identity,
    queue_send_request,
    target_ref,
)
from app.register.model import (
    OPERATOR_RESUMABLE,
    IntentState,
    ScopePauseReason,
    VerificationState,
)
from app.register.preflight import CapabilityReader, RegistrationPreflightService
from app.register.store import (
    AttemptRecord,
    DraftItemRecord,
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

# Why a derived value is absent. A preflight is recomputed from the operator's own inputs, and the
# only durable copy of those is the frozen send request a CREATE job carries: a unit without one
# has no evaluation to show, and this surface names that rather than omitting the row (PR-F §B).
PREFLIGHT_INPUTS_NOT_DURABLE = "REGISTER_PREFLIGHT_INPUTS_NOT_DURABLE"
PREFLIGHT_OWNER_ABSENT = "REGISTER_PREFLIGHT_NOT_WIRED"

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
        preflight: RegistrationPreflightService | None = None,
        accounts: MarketplaceAccountStore | None = None,
        jobs: JobService | None = None,
        capability: CapabilityReader | None = None,
        adoption: AdoptionFacts | None = None,
        marketplace_key: str = _MARKETPLACE,
        execution_mode: str = DRY_RUN,
    ) -> None:
        self._registrations = registrations
        self._execution = execution
        self._preflight = preflight
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
        intents = {i.registration_snapshot_id: i for i in store.intents(limit=limit * 4)}
        units = tuple(unit for draft in drafts for unit in self._units_of(draft, intents))
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
        units = self.overview().units if draft_id is None else self.units(draft_id)
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

    def units(self, draft_id: str) -> tuple[UnitView, ...]:
        """Every provider-listing unit of one Draft (§2, R3), frozen or still a preparation."""
        store = self._require_store()
        draft = store.draft(draft_id)
        if draft is None:
            raise NotFoundError("REGISTER_DRAFT_NOT_FOUND", "no such registration draft")
        intents = {i.registration_snapshot_id: i for i in store.intents(limit=200)}
        return self._units_of(draft, intents)

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

    def _units_of(
        self, draft: DraftRecord, intents: Mapping[str, IntentRecord]
    ) -> tuple[UnitView, ...]:
        """One view per provider-listing unit of this Draft — never one row for the Draft (§2).

        A frozen unit is its newest Snapshot for exactly that set of Items, with that Snapshot's
        own Intent, Attempts and actions; an older generation of the same unit stays history, and
        an unresolved one is still named by the conflict scope. The Items the shape would still
        form, and that no current-revision Snapshot covers, are shown separately as preparations:
        a prospective unit never borrows a frozen unit's identity, keys or actions.
        """
        store = self._require_store()
        newest: dict[tuple[str, ...], SnapshotRecord] = {}
        for snapshot in store.snapshots_of_draft(draft.draft_id):
            newest.setdefault(_unit_items(snapshot), snapshot)
        frozen = [
            self._frozen_unit(draft, snapshot, intents.get(snapshot.registration_snapshot_id))
            for snapshot in newest.values()
        ]
        covered = {
            key
            for key, snapshot in newest.items()
            if snapshot.draft_revision == draft.draft_revision
        }
        drafted = [
            self._drafted_unit(draft, item_ids)
            for item_ids in self._prospective_units(draft)
            if tuple(sorted(item_ids)) not in covered
        ]
        return tuple(frozen + drafted)

    def _prospective_units(self, draft: DraftRecord) -> tuple[tuple[str, ...], ...]:
        """What the preflight owner says this Draft's open Items would form (§2). Never derived
        here: without that owner no preparation is shown rather than a composition being guessed."""
        if self._preflight is None:
            return ()
        try:
            return self._preflight.prospective_units(draft.draft_id)
        except AppError:  # pragma: no cover - the Draft was read a moment ago
            return ()

    def _frozen_unit(
        self, draft: DraftRecord, snapshot: SnapshotRecord, intent: IntentRecord | None
    ) -> UnitView:
        store = self._require_store()
        payload = store.snapshot_payload(snapshot.registration_snapshot_id) or {}
        facts, facts_problem = self._item_facts(draft.draft_id, _unit_items(snapshot))
        preflight, preflight_problem = self._preflight_of(snapshot, intent)
        return self._unit_view(
            draft,
            unit_ref=snapshot.listing_identity,
            preparation=(
                PreparationState.INTENT_OPEN
                if intent is not None
                else PreparationState.SNAPSHOT_FROZEN
            ),
            items=self._frozen_items(snapshot, payload, facts),
            category=self._category_of(payload),
            preflight=preflight,
            preflight_unavailable_reason=preflight_problem,
            item_facts_unavailable_reason=facts_problem,
            snapshot=snapshot,
            intent=intent,
            conflicting_intents=store.conflicting_intents(snapshot.registration_snapshot_id),
        )

    def _drafted_unit(self, draft: DraftRecord, item_ids: Sequence[str]) -> UnitView:
        """A unit that exists only as a preparation: no Snapshot froze it, so it has no listing
        identity, no `registration_item_key` and no Intent — and none is invented for it."""
        chosen = [item for item in draft.items if item.item_id in set(item_ids)]
        facts, facts_problem = self._item_facts(draft.draft_id, tuple(sorted(item_ids)))
        return self._unit_view(
            draft,
            unit_ref=f"{draft.draft_id}:{'+'.join(str(item.ordinal) for item in chosen)}",
            preparation=PreparationState.DRAFTED,
            items=self._drafted_items(chosen, facts),
            category=None,
            preflight=None,
            preflight_unavailable_reason=PREFLIGHT_INPUTS_NOT_DURABLE,
            item_facts_unavailable_reason=facts_problem,
            snapshot=None,
            intent=None,
            conflicting_intents=(),
        )

    def _unit_view(
        self,
        draft: DraftRecord,
        *,
        unit_ref: str,
        preparation: PreparationState,
        items: tuple[ItemView, ...],
        category: CategoryView | None,
        preflight: PreflightView | None,
        preflight_unavailable_reason: str | None,
        item_facts_unavailable_reason: str | None,
        snapshot: SnapshotRecord | None,
        intent: IntentRecord | None,
        conflicting_intents: tuple[str, ...],
    ) -> UnitView:
        store = self._require_store()
        attempts = () if intent is None else store.attempts(intent.intent_id)
        scope = self._scope_record(draft.marketplace_key, draft.marketplace_account_id)
        budget = self._budget_of(draft.marketplace_key, draft.marketplace_account_id, scope)
        binding = self._binding(draft.marketplace_key, draft.marketplace_account_id)
        registration = self._registration_of(intent)
        live = self._live_job(intent)
        return UnitView(
            unit_ref=unit_ref,
            draft_id=draft.draft_id,
            draft_revision=draft.draft_revision,
            listing_shape=draft.listing_shape,
            marketplace_key=draft.marketplace_key,
            marketplace_account_id=draft.marketplace_account_id,
            account_binding=binding,
            preparation=preparation,
            items=items,
            category=category,
            preflight=preflight,
            preflight_unavailable_reason=preflight_unavailable_reason,
            item_facts_unavailable_reason=item_facts_unavailable_reason,
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
            conflicting_intents=conflicting_intents,
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

    def _frozen_items(
        self,
        snapshot: SnapshotRecord,
        payload: Mapping[str, Any],
        facts: Mapping[str, Any],
    ) -> tuple[ItemView, ...]:
        """The unit's Items exactly as the Snapshot froze them (§6, §7).

        Every row here is an `ItemSnapshot` of **this** Snapshot: its own registration item key,
        its own pinned price and the publication assets the frozen payload carries. A Draft Item
        that is not in this Snapshot belongs to another unit and no key is derived for it.
        """
        assets = _frozen_assets(payload)
        options = _frozen_options(payload)
        return tuple(
            self._item_view(
                item_id=row.item_id,
                product_group_id=row.group_id_at_registration,
                composition_signature=row.composition_signature,
                ordinal=row.ordinal,
                pricing_snapshot_id=row.pricing_snapshot_id,
                registration_item_key=row.registration_item_key,
                publication_assets=assets.get(row.item_id, ()),
                option_keys=options.get(row.item_id, ()),
                fact=facts.get(row.item_id),
            )
            for row in snapshot.items
        )

    def _drafted_items(
        self, items: Sequence[DraftItemRecord], facts: Mapping[str, Any]
    ) -> tuple[ItemView, ...]:
        return tuple(
            self._item_view(
                item_id=item.item_id,
                product_group_id=item.product_group_id,
                composition_signature=item.composition_signature,
                ordinal=item.ordinal,
                pricing_snapshot_id=item.pricing_snapshot_id,
                registration_item_key=None,
                publication_assets=_current_assets(facts.get(item.item_id)),
                fact=facts.get(item.item_id),
            )
            for item in items
        )

    def _item_view(
        self,
        *,
        item_id: str,
        product_group_id: str,
        composition_signature: str,
        ordinal: int,
        pricing_snapshot_id: str,
        registration_item_key: str | None,
        publication_assets: tuple[AssetView, ...],
        option_keys: tuple[str, ...] = (),
        fact: Any,
    ) -> ItemView:
        """One Item's durable facts: the pinned price, the M4 price now, and M4's own readiness."""
        store = self._require_store()
        pin = store.pricing_pin(pricing_snapshot_id)
        current_id = None if fact is None else fact.current_price_id
        current = None if current_id is None else store.pricing_pin(current_id)
        return ItemView(
            item_id=item_id,
            product_group_id=product_group_id,
            composition_signature=composition_signature,
            ordinal=ordinal,
            pricing_snapshot_id=pricing_snapshot_id,
            sale_price_krw=None if pin is None else pin.final_sale_price_krw,
            price_basis=None if pin is None else pin.price_basis.value,
            registration_item_key=registration_item_key,
            publication_assets=publication_assets,
            option_keys=option_keys,
            current_pricing_snapshot_id=current_id,
            current_sale_price_krw=None if current is None else current.final_sale_price_krw,
            current_price_basis=None if current is None else current.price_basis.value,
            price_pin_current=None if fact is None else current_id == pricing_snapshot_id,
            base_status=None if fact is None else fact.base.status.value,
            base_reason_codes=() if fact is None else _codes(fact.base),
            pricing_status=None if fact is None else fact.pricing.status.value,
            pricing_reason_codes=() if fact is None else _codes(fact.pricing),
        )

    def _item_facts(
        self, draft_id: str, item_ids: Sequence[str]
    ) -> tuple[Mapping[str, Any], str | None]:
        """The M4 truth of these Items now, gathered by the preflight owner (§3). It decides
        nothing here: the readiness, the price and the QA are each their own owner's verdict."""
        if self._preflight is None:
            return {}, PREFLIGHT_OWNER_ABSENT
        try:
            resolved = self._preflight.unit_truth(draft_id, item_ids=list(item_ids))
        except AppError as refused:
            # A target policy the account does not have, a Draft that moved: named, never hidden.
            return {}, refused.code
        return {item.item_id: item for item in resolved.items}, None

    def _category_of(self, payload: Mapping[str, Any]) -> CategoryView | None:
        """The category the Snapshot froze, with the required-field state of its reviewed
        metadata (§4). What is `provided` is read from the frozen payload, never assumed."""
        category = payload.get("category")
        if not isinstance(category, Mapping):
            return None
        taxonomy, category_id = (
            str(category.get("taxonomy_revision", "")),
            str(category.get("category_id", "")),
        )
        metadata = (
            None
            if self._preflight is None or not (taxonomy and category_id)
            else self._preflight.category_metadata(taxonomy, category_id)
        )
        attributes = payload.get("attributes")
        notice = payload.get("notice")
        provided = set(attributes) if isinstance(attributes, Mapping) else set()
        notice_fields = notice.get("fields") if isinstance(notice, Mapping) else None
        provided_notice = set(notice_fields) if isinstance(notice_fields, Mapping) else set()
        return CategoryView(
            category_id=category_id,
            mapping_revision=str(category.get("mapping_revision", "")),
            taxonomy_revision=taxonomy,
            metadata_revision=(
                str(category["metadata_revision"]) if "metadata_revision" in category else None
            ),
            reviewed=None if metadata is None else metadata.reviewed,
            notice_type=(
                None if metadata is None or metadata.notice is None else metadata.notice.notice_type
            ),
            attributes=_fields(() if metadata is None else metadata.attributes, provided),
            notice_fields=_fields(
                () if metadata is None or metadata.notice is None else metadata.notice.fields,
                provided_notice,
            ),
            options_supported=None if metadata is None else metadata.options.options_supported,
            max_options=None if metadata is None else metadata.options.max_options,
        )

    def _preflight_of(
        self, snapshot: SnapshotRecord, intent: IntentRecord | None
    ) -> tuple[PreflightView | None, str | None]:
        """Re-evaluate this frozen unit from current truth, by the owner that decides it (§3).

        The evaluation needs the operator's preflight inputs, and the only durable copy of them is
        the **frozen send request** a CREATE job carries (`registration-send-request/v1`). A unit
        with no such job therefore has no evaluation to show, and says so with a code rather than
        showing a status it cannot stand behind.
        """
        if self._preflight is None:
            return None, PREFLIGHT_OWNER_ABSENT
        payload = None if intent is None else self._send_request(intent.intent_id)
        if payload is None:
            return None, PREFLIGHT_INPUTS_NOT_DURABLE
        try:
            request, prepared = decode_send_request(payload)
            _identity, generation = frozen_unit_identity(payload)
            fresh = self._preflight.final(request, prepared, identity_generation=generation)
        except AppError as refused:
            return None, refused.code
        return (
            PreflightView(
                status=fresh.status.value,
                reason_codes=tuple(sorted(set(fresh.codes))),
                rule_version=fresh.rule_version,
                dependency_fingerprint=fresh.dependency_fingerprint,
                fingerprint_matches_snapshot=(
                    fresh.dependency_fingerprint == snapshot.preflight_fingerprint
                ),
            ),
            None,
        )

    # ------------------------------------------------------------------ owner reads

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


def _unit_items(snapshot: SnapshotRecord) -> tuple[str, ...]:
    """The Items one provider-listing unit holds: its identity within a Draft (§2, §7)."""
    return tuple(sorted(item.item_id for item in snapshot.items))


def _codes(readiness: Any) -> tuple[str, ...]:
    """Every reason code an M4 owner returned, in its own order."""
    return tuple(reason.code for reason in readiness.reasons)


def _fields(rules: Sequence[Any], provided: set[str]) -> tuple[FieldStateView, ...]:
    return tuple(
        FieldStateView(
            key=rule.key,
            required=rule.required,
            provided=rule.key in provided,
            detail_page_reference_allowed=rule.detail_page_reference_allowed,
        )
        for rule in rules
    )


def _frozen_assets(payload: Mapping[str, Any]) -> dict[str, tuple[AssetView, ...]]:
    """The publication assets the Snapshot froze for each Item, by their exact identity (§5).

    The provider reference itself is never exposed — only whether one was frozen — so no provider
    URL or opaque credential-shaped value reaches the screen.
    """
    items = payload.get("items")
    if not isinstance(items, list):  # pragma: no cover - a Snapshot always has its items
        return {}
    frozen: dict[str, tuple[AssetView, ...]] = {}
    for item in items:
        if not isinstance(item, Mapping):  # pragma: no cover - the payload is a frozen document
            continue
        assets = item.get("publication_assets")
        frozen[str(item.get("item_id"))] = tuple(
            AssetView(
                role=str(asset.get("role", "")),
                asset_kind=str(asset.get("asset_kind", "")),
                sha256=str(asset.get("sha256", "")),
                derivation_id=_text(asset.get("derivation_id")),
                qa_verdict=_text(asset.get("qa_verdict")),
                qa_result_id=_text(asset.get("qa_result_id")),
                provider_asset_prepared=asset.get("provider_asset_ref") is not None,
            )
            for asset in (assets if isinstance(assets, list) else [])
            if isinstance(asset, Mapping)
        )
    return frozen


def _frozen_options(payload: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """The option fields each Item was frozen with, by name (§4). Values are product text and the
    screen shows the product for that; what the surface owes is which fields were sent."""
    items = payload.get("items")
    if not isinstance(items, list):  # pragma: no cover - a Snapshot always has its items
        return {}
    return {
        str(item.get("item_id")): tuple(sorted(options))
        for item in items
        if isinstance(item, Mapping)
        for options in [item.get("options")]
        if isinstance(options, Mapping)
    }


def _current_assets(fact: Any) -> tuple[AssetView, ...]:
    """The Item's current operator image selection and its exact-binary QA (M4 owner)."""
    if fact is None:
        return ()
    return tuple(
        AssetView(
            role=image.role.value,
            asset_kind=image.asset_kind.value,
            sha256=image.sha256,
            derivation_id=image.derivation_id,
            qa_verdict=None if image.qa_verdict is None else image.qa_verdict.value,
            qa_result_id=image.qa_result_id,
        )
        for image in fact.images
    )


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


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
