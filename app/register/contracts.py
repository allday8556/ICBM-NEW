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

    EVALUATE = "EVALUATE"
    FREEZE = "FREEZE"
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


class AssetView(BaseModel):
    """One selected publication asset of an Item, by its exact local identity (§5, R1).

    The identity is the M4 artifact's own: the role it publishes in, its kind, the exact binary and
    the derivation that produced it. The QA verdict is the M4 image owner's, for **that** binary
    under the selection's validated revision — never a count and never a provider URL.
    """

    role: str
    asset_kind: str
    sha256: str
    derivation_id: str | None
    qa_verdict: str | None
    qa_result_id: str | None
    # Whether a provider-issued identity was frozen for it. The reference itself is never exposed.
    provider_asset_prepared: bool = False


class ItemView(BaseModel):
    """One Item of the unit: its pinned price, the current M4 price, its M4 readiness and assets.

    The pinned price is the exact `PricingSnapshot` the Draft froze (§2); the current one is what
    the M4 pricing owner holds for the account's target context **now**. Both are shown because a
    reprice makes them differ, and `price_pin_current` is the server's own comparison — the screen
    never computes it.
    """

    item_id: str
    product_group_id: str
    composition_signature: str
    ordinal: int
    pricing_snapshot_id: str
    sale_price_krw: int | None
    price_basis: str | None
    registration_item_key: str | None
    publication_assets: tuple[AssetView, ...] = ()
    # The option fields this Item was frozen with, by name only (§4). Empty for a preparation:
    # nothing durable holds an option an operator has not frozen yet.
    option_keys: tuple[str, ...] = ()
    # The M4 target price now, and whether the pin is still it (server verdicts, §22).
    current_pricing_snapshot_id: str | None = None
    current_sale_price_krw: int | None = None
    current_price_basis: str | None = None
    price_pin_current: bool | None = None
    # The M4 owners' own readiness for this Item, with every reason code they returned.
    base_status: str | None = None
    base_reason_codes: tuple[str, ...] = ()
    pricing_status: str | None = None
    pricing_reason_codes: tuple[str, ...] = ()


class SnapshotView(BaseModel):
    registration_snapshot_id: str
    listing_identity: str
    preflight_fingerprint: str
    payload_hash: str
    draft_revision: int


class FieldStateView(BaseModel):
    """One field the category or the platform declares, and whether the unit carries it (§4).

    `provided` is read from the Snapshot's frozen payload — what was actually sent — never from a
    guess about what an operator would type. Only the key is shown: a value is product text and
    the screen has the product for that.
    """

    key: str
    required: bool
    provided: bool
    detail_page_reference_allowed: bool = False


class CategoryView(BaseModel):
    """The unit's category and the required-field state under its reviewed metadata (§4)."""

    category_id: str
    mapping_revision: str
    taxonomy_revision: str
    metadata_revision: str | None = None
    # None when the metadata source has no reviewed entry for this taxonomy revision.
    reviewed: bool | None = None
    notice_type: str | None = None
    attributes: tuple[FieldStateView, ...] = ()
    notice_fields: tuple[FieldStateView, ...] = ()
    # Whether this category's listings may carry several Items as options, and how many (§4).
    options_supported: bool | None = None
    max_options: int | None = None
    # Why the rules above are absent: the exact metadata revision the Snapshot froze could not be
    # resolved within its own key. The current revision is never shown in its place (G1-09).
    metadata_unavailable_reason: str | None = None


class FieldValueView(BaseModel):
    """One authored outbound value and its provenance (§4). The operator's own, read back."""

    value: str = ""
    provenance: str = "OPERATOR_CONFIRMED"
    detail_page_reference: bool = False


class CategoryChoiceView(BaseModel):
    """The category an operator chose, with the revisions it was chosen under (§4).

    ``mapping_revision`` is required and nullable: the client sends back exactly what the server
    gave it, ``None`` included while no mapping owner exists (decision 5800619183)."""

    category_id: str
    mapping_revision: str | None
    taxonomy_revision: str
    confirmation: str = "OPERATOR_CONFIRMED"


class AuthoringFieldView(BaseModel):
    key: str
    required: bool
    detail_page_reference_allowed: bool = False


class AuthoringMetadataView(BaseModel):
    """Server-owned revisions and reviewed fields for one category authoring form.

    ``mapping_revision`` and ``detail_composition_revision`` are the target policy's own values,
    ``None`` while no owner exists for them (decision 5800619183): never a default."""

    category_id: str
    mapping_revision: str | None
    taxonomy_revision: str
    metadata_revision: str
    detail_composition_revision: str | None
    notice_type: str | None = None
    attributes: tuple[AuthoringFieldView, ...] = ()
    notice_fields: tuple[AuthoringFieldView, ...] = ()
    options_supported: bool = False
    max_options: int = 1
    max_option_dimensions: int = 1


class AuthoredInputsView(BaseModel):
    """What an operator authored for one provider-listing unit (§27).

    The same shape is read back and written: a client fills the form from what is stored and sends
    it back. It carries **inputs only** — no status, no readiness and nothing derived.
    """

    category: CategoryChoiceView | None = None
    name: FieldValueView | None = None
    tags: tuple[str, ...] = ()
    attributes: dict[str, FieldValueView] = {}
    notices: dict[str, FieldValueView] = {}
    # Item id → option dimension → option value. Display values only, never an identity.
    options: dict[str, dict[str, str]] = {}
    detail_composition_revision: str | None = None
    detail_body: str | None = None
    detail_sections: tuple[str, ...] = ("BODY",)


class PreparationRevisionView(BaseModel):
    """One authored revision in the preparation's history. Append-only, so this never changes."""

    preparation_revision_id: str
    revision_no: int
    draft_revision: int
    inputs_fingerprint: str
    authored_by: str
    authored_at: datetime
    item_ids: tuple[str, ...]


class PreparationView(BaseModel):
    """The durable preparation of one provider-listing unit, and what is authored now (§27)."""

    preparation_id: str
    draft_id: str
    marketplace_key: str
    marketplace_account_id: str
    revision_no: int
    item_ids: tuple[str, ...]
    inputs: AuthoredInputsView
    inputs_fingerprint: str
    revisions: tuple[PreparationRevisionView, ...]


class PreflightView(BaseModel):
    """The preflight re-evaluated from current truth, by the owner that decides it (§3).

    It is derived, never stored. `source` says what was evaluated: the **frozen send request** a
    CREATE job carries, which is the exact check the send gate makes and the only one that can
    compare fingerprints with the Snapshot, or the durable **preparation**, which is evaluated as
    a mutation-free candidate — every dependency except the provider asset identity (§3).
    """

    status: str
    stage: str
    source: str
    reason_codes: tuple[str, ...]
    rule_version: str
    dependency_fingerprint: str
    # Only a final evaluation of a frozen unit can answer this; a candidate leaves it unanswered.
    fingerprint_matches_snapshot: bool | None = None


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
    """One **provider-listing unit** as the server holds it, from Draft to verified registration.

    A Draft may hold several (§2, R3): `SEPARATE_LISTINGS` gives one per Item, each with its own
    Snapshot, Intent, Attempts and actions. Each is its own row here — a Draft is never collapsed
    into one panel — and a frozen unit's Items are exactly the Snapshot's, never the Draft's.
    """

    # Stable within a Draft: the frozen unit's listing identity, or the prospective unit's Items.
    unit_ref: str
    draft_id: str
    draft_revision: int
    listing_shape: ListingShape
    marketplace_key: str
    marketplace_account_id: str
    account_binding: AccountBinding
    preparation: PreparationState
    items: tuple[ItemView, ...]
    category: CategoryView | None = None
    # The durable preparation this unit is authored in, when one exists (§27).
    authored: PreparationView | None = None
    preflight: PreflightView | None = None
    # Why no preflight is shown, as a server code: its operator inputs are durable only in the
    # frozen send request a CREATE job carries, so a unit without one has no evaluation to show.
    preflight_unavailable_reason: str | None = None
    # Why the per-Item M4 facts are absent, as the owner that refused them named it.
    item_facts_unavailable_reason: str | None = None
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
    preparation_id: str | None = None
    registration_snapshot_id: str | None = None
    # The owner's own evaluation, when the action was one that asks for it.
    preflight: PreflightView | None = None
