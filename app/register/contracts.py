"""Read shape of the Registration Management surface (Issue #89 PR-F §B, ADR-0014 §22).

Every field here is **server-owned state**. The screen renders it; it never re-decides a price, a
readiness, a duplicate verdict, an option compatibility, a budget, a retry eligibility or a
capability. Whether an action may be taken at all is `ActionView.enabled` with the server's own
reason code, so a client that ignored the state could still not manufacture an action the server
refuses (the route re-checks every rule).

Nothing provider-shaped is exposed: a marketplace product identity appears only once the Intent
proved it (§9), and no payload value, credential, URL or provider response text is carried.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from app.connect.accounts import AccountBinding
from app.connect.marketplace.capability import RemoteOutcome
from app.core.errors import ErrorClass
from app.register.model import (
    ExecutionScopeState,
    IntentState,
    ListingShape,
    ResolutionEvidence,
    ResolvedBy,
    ScopePauseReason,
    VerificationState,
)


class RegisterAction(StrEnum):
    """What an operator may ask of one unit. The server decides whether each is available."""

    CREATE_ENQUEUE = "CREATE_ENQUEUE"
    RECONCILE = "RECONCILE"
    VERIFY = "VERIFY"
    RESUME_SCOPE = "RESUME_SCOPE"


class PreparationState(StrEnum):
    """How far one Draft unit has been prepared, as the durable rows show it (§3, §6, §8)."""

    DRAFTED = "DRAFTED"
    SNAPSHOT_FROZEN = "SNAPSHOT_FROZEN"
    INTENT_OPEN = "INTENT_OPEN"


class ActionView(BaseModel):
    action: RegisterAction
    enabled: bool
    # Why not, as a server code. The screen looks up copy for it; it never derives the verdict.
    reason_code: str | None = None


class ScopeBrakeView(BaseModel):
    """One execution scope's send brake and its durable resume boundary (§26)."""

    marketplace_key: str
    marketplace_account_id: str
    endpoint_group: str
    state: ExecutionScopeState
    pause_reason: ScopePauseReason | None
    pause_error_class: ErrorClass | None
    paused_at: datetime | None
    pause_policy_version: str | None
    resume_generation: int
    resumed_at: datetime | None
    resumed_by: str | None
    resume_reason: str | None
    # Server verdicts, never client arithmetic.
    sends_allowed: bool
    consecutive_failures: int
    budget_exhausted: bool
    operator_resumable: bool


class AttemptView(BaseModel):
    attempt_no: int
    outcome: RemoteOutcome | None
    resolved_outcome: RemoteOutcome | None
    resolved_by: ResolvedBy | None
    resolution_evidence_kind: ResolutionEvidence | None
    error_class: ErrorClass | None
    error_code: str | None
    ambiguous_result: bool
    started_at: datetime | None


class ItemView(BaseModel):
    """One Item of the unit, with the exact M4 price the Draft pinned (§2)."""

    item_id: str
    product_group_id: str
    composition_signature: str
    ordinal: int
    pricing_snapshot_id: str
    sale_price_krw: int | None
    price_basis: str | None
    registration_item_key: str | None
    publication_assets: int


class SnapshotView(BaseModel):
    registration_snapshot_id: str
    listing_identity: str
    preflight_fingerprint: str
    payload_hash: str
    draft_revision: int


class IntentView(BaseModel):
    intent_id: str
    state: IntentState
    remote_outcome: RemoteOutcome | None
    verification_state: VerificationState
    # Only ever present once the provider proved it (§9): never a hopeful identity.
    marketplace_product_id: str | None
    idempotency_key: str
    attempts: tuple[AttemptView, ...]
    live_job_id: str | None


class UnitView(BaseModel):
    """One provider-listing unit as the server holds it, from Draft to verified registration."""

    draft_id: str
    draft_revision: int
    listing_shape: ListingShape
    marketplace_key: str
    marketplace_account_id: str
    account_binding: AccountBinding
    preparation: PreparationState
    items: tuple[ItemView, ...]
    snapshot: SnapshotView | None
    intent: IntentView | None
    registration_id: str | None
    published_state: str | None
    conflicting_intents: tuple[str, ...]
    scope: ScopeBrakeView
    actions: tuple[ActionView, ...]


class RegisterOverview(BaseModel):
    """The Registration Management screen's server-owned state."""

    registration_candidates_total: int
    registrations_total: int
    units: tuple[UnitView, ...]
    paused_scopes: tuple[ScopeBrakeView, ...]


class ActionResult(BaseModel):
    """What one accepted operator action did. It never claims a provider outcome."""

    action: RegisterAction
    intent_id: str | None = None
    job_id: str | None = None
    intent_state: IntentState | None = None
    verification_state: VerificationState | None = None
    scope: ScopeBrakeView | None = None
