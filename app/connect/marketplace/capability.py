"""SmartStore capability truth model (M2 PR-B).

Contract: ``docs/platforms/smartstore/CAPABILITY_MAPPING.md``. Its §17 targets are owned per
``CAPABILITY_MAPPING_IMPLEMENTATION_OWNERSHIP.md``; PR-B owns the domain/state portion.

This module is pure: no I/O, no clock and no provider call. Every value it can construct
satisfies the contract's invariants, so neither persistence nor the read API can carry a
forbidden combination. The nine axes stay independent (M2 instructions §4.2)::

    auth · write_scope.status · write.status · contract_freshness
    workflow_state · workflow_scope · reason_code · error_class · remote_outcome

One axis constrains another only through an explicit invariant below (CAPABILITY_MAPPING §3).
Human action is a scoped overlay: at most one per ``workflow_scope``.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from app.core.errors import ErrorClass

# ---------------------------------------------------------------- axes


class AuthStatus(StrEnum):
    """Whether the current committed generations prove the intended account (§2.1)."""

    READY = "READY"
    NOT_READY = "NOT_READY"
    AUTH_MISMATCH = "AUTH_MISMATCH"
    NOT_BOUND = "NOT_BOUND"


class WriteScopeStatus(StrEnum):
    """Provider API-group permission evidence, not OAuth scope strings (§2.2)."""

    READY = "READY"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class EvidenceStrength(StrEnum):
    OPERATOR_ATTESTED = "OPERATOR_ATTESTED"
    MACHINE_VERIFIED = "MACHINE_VERIFIED"


class EvidenceGrade(StrEnum):
    """Semantic strength marker of positive permission evidence (§14.1). The UI draws ◐ for
    LIMITED and ● for STRONG (PR-D); the domain keeps the two grades from ever merging."""

    LIMITED = "LIMITED"
    STRONG = "STRONG"


class WriteStatus(StrEnum):
    """Real operation proof, never a permission declaration (§2.3)."""

    UNVERIFIED = "UNVERIFIED"
    READY = "READY"
    BLOCKED = "BLOCKED"


class ContractFreshness(StrEnum):
    """Freshness of ICBM's adopted contract understanding, not runtime truth (§2.4)."""

    CURRENT = "CURRENT"
    STALE = "STALE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class WorkflowState(StrEnum):
    """Scoped human-action overlay: not an error class and not the Job state machine (§2.5)."""

    PAUSED = "PAUSED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class WorkflowScope(StrEnum):
    """The M2 frozen set (§2.6). A new scope needs contract review."""

    AUTHENTICATION = "AUTHENTICATION"
    PRODUCT_REGISTRATION = "PRODUCT_REGISTRATION"


class PauseReason(StrEnum):
    """The M2 frozen PAUSED reasons (§9). Adapters never invent another."""

    AUTH_RETRY_LIMIT = "AUTH_RETRY_LIMIT"
    APPLICATION_REAUTH_REQUIRED = "APPLICATION_REAUTH_REQUIRED"
    SCOPE_INSUFFICIENT = "SCOPE_INSUFFICIENT"
    ACCOUNT_RESTRICTED = "ACCOUNT_RESTRICTED"


class RemoteOutcome(StrEnum):
    """Evidence of whether a remote mutation happened (ERRORS.md §2.2)."""

    APPLIED_PROVEN = "APPLIED_PROVEN"
    NOT_APPLIED_PROVEN = "NOT_APPLIED_PROVEN"
    UNKNOWN = "UNKNOWN"


# §9: the scope each frozen reason may pause.
REASON_SCOPES: dict[PauseReason, frozenset[WorkflowScope]] = {
    PauseReason.AUTH_RETRY_LIMIT: frozenset({WorkflowScope.AUTHENTICATION}),
    PauseReason.APPLICATION_REAUTH_REQUIRED: frozenset({WorkflowScope.AUTHENTICATION}),
    PauseReason.SCOPE_INSUFFICIENT: frozenset({WorkflowScope.PRODUCT_REGISTRATION}),
    PauseReason.ACCOUNT_RESTRICTED: frozenset(
        {WorkflowScope.AUTHENTICATION, WorkflowScope.PRODUCT_REGISTRATION}
    ),
}

EVIDENCE_GRADES: dict[EvidenceStrength, EvidenceGrade] = {
    EvidenceStrength.OPERATOR_ATTESTED: EvidenceGrade.LIMITED,
    EvidenceStrength.MACHINE_VERIFIED: EvidenceGrade.STRONG,
}

# W1/E3: the product mutation and read-back endpoints are NOT_ADOPTED in M2, so nothing can prove
# product write. Only a reviewed M5 contract change may alter this.
PRODUCT_WRITE_PROVABLE = False

# §9.2: SMARTSTORE-R0-APP-REAUTH is PENDING (SOURCES.md). Until a reviewed mapping accepts it,
# automatic convergence to APPLICATION_REAUTH_REQUIRED stays disabled.
APP_REAUTH_DETECTION_ACCEPTED = False


class CapabilityInvariantError(ValueError):
    """A value or combination the capability contract forbids."""


class ExpansionBlockedError(CapabilityInvariantError):
    """A new-trust decision refused because the adopted contract is not CURRENT (§8 F2/F3)."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CapabilityInvariantError(message)


def _typed(value: object, enum: type[StrEnum], name: str, *, optional: bool = False) -> None:
    """Axes hold enum members only: free text equal to a member's value is still refused."""
    if optional and value is None:
        return
    _require(
        isinstance(value, enum), f"{name} must be a {enum.__name__}, got {type(value).__name__}"
    )


# ---------------------------------------------------------------- values


@dataclass(frozen=True)
class WriteScope:
    """Permission status with its evidence strength (PERMISSIONS_SCOPES §5, §17 #6/#7).

    READY and MISSING are positive evidence and never travel without a strength (S1); UNKNOWN is
    the absence of evidence and carries none. The rest of the evidence envelope — source,
    fingerprint, groups, mapping revision, freshness — arrives with A0 attestation (PR-C).
    """

    status: WriteScopeStatus
    evidence_strength: EvidenceStrength | None = None

    def __post_init__(self) -> None:
        _typed(self.status, WriteScopeStatus, "write_scope.status")
        if self.status is WriteScopeStatus.UNKNOWN:
            _require(self.evidence_strength is None, "UNKNOWN permission carries no evidence")
        else:
            _typed(self.evidence_strength, EvidenceStrength, "write_scope.evidence_strength")

    @property
    def evidence_grade(self) -> EvidenceGrade | None:
        """Positive evidence only: MISSING is a blocker, never a positive marker (§14.1)."""
        if self.status is WriteScopeStatus.READY and self.evidence_strength is not None:
            return EVIDENCE_GRADES[self.evidence_strength]
        return None


PERMISSION_UNKNOWN = WriteScope(WriteScopeStatus.UNKNOWN)


@dataclass(frozen=True)
class WorkflowOverlay:
    """One scoped human-action overlay (§9, §10)."""

    state: WorkflowState
    scope: WorkflowScope
    reason_code: PauseReason | None = None
    # APPLICATION_REAUTH_REQUIRED only: the session generation current when the pause began.
    # Recovery needs a newer committed session (§17 #15).
    session_generation: int | None = None

    def __post_init__(self) -> None:
        _typed(self.state, WorkflowState, "workflow_state")
        _typed(self.scope, WorkflowScope, "workflow_scope")
        reason = self.reason_code
        if self.state is WorkflowState.PAUSED:
            if not isinstance(reason, PauseReason):
                raise CapabilityInvariantError("PAUSED requires one frozen reason_code (§9.1)")
            _require(
                self.scope in REASON_SCOPES[reason],
                f"{reason} does not apply to workflow_scope {self.scope} (§9)",
            )
        else:
            _require(reason is None, "REVIEW_REQUIRED never carries a PAUSED reason_code (§10)")
        if reason is PauseReason.APPLICATION_REAUTH_REQUIRED:
            _require(
                isinstance(self.session_generation, int),
                "an application re-auth pause records the paused session generation",
            )
        else:
            _require(
                self.session_generation is None,
                "only an application re-auth pause records a session generation",
            )


SCOPE_INSUFFICIENT_PAUSE = WorkflowOverlay(
    WorkflowState.PAUSED, WorkflowScope.PRODUCT_REGISTRATION, PauseReason.SCOPE_INSUFFICIENT
)


@dataclass(frozen=True)
class CapabilityState:
    """One marketplace account's capability truth.

    Build it through the transitions below; ``__post_init__`` refuses every combination the
    contract forbids, whatever produced it (a transition, a database row or a test).
    """

    auth: AuthStatus = AuthStatus.NOT_BOUND
    write_scope: WriteScope = PERMISSION_UNKNOWN
    write: WriteStatus = WriteStatus.UNVERIFIED
    # F4: freshness is never assumed. A new state starts REVIEW_REQUIRED, so no new-trust decision
    # can happen until the current freshness is recorded from an owning source (PR #26 audit
    # 5657617043); proven behaviour that depends on no disputed invariant still continues.
    contract_freshness: ContractFreshness = ContractFreshness.REVIEW_REQUIRED
    overlays: tuple[WorkflowOverlay, ...] = ()
    error_class: ErrorClass | None = None
    remote_outcome: RemoteOutcome | None = None
    auth_verified_at: datetime | None = None
    # A proof must come from a session newer than this one (set when an application re-auth
    # pause is resolved, §17 #15).
    session_generation_floor: int | None = None

    def __post_init__(self) -> None:
        _typed(self.auth, AuthStatus, "auth")
        _require(isinstance(self.write_scope, WriteScope), "write_scope must be a WriteScope")
        _typed(self.write, WriteStatus, "write.status")
        _typed(self.contract_freshness, ContractFreshness, "contract_freshness")
        _typed(self.error_class, ErrorClass, "error_class", optional=True)
        _typed(self.remote_outcome, RemoteOutcome, "remote_outcome", optional=True)
        _require(
            self.session_generation_floor is None or isinstance(self.session_generation_floor, int),
            "session_generation_floor must be an int",
        )
        overlays = tuple(self.overlays)
        _require(
            all(isinstance(o, WorkflowOverlay) for o in overlays),
            "overlays must be WorkflowOverlay values",
        )
        scopes = [o.scope for o in overlays]
        _require(len(scopes) == len(set(scopes)), "at most one overlay per workflow_scope")
        object.__setattr__(self, "overlays", tuple(sorted(overlays, key=lambda o: o.scope.value)))

        authentication = self.overlay(WorkflowScope.AUTHENTICATION)
        registration = self.overlay(WorkflowScope.PRODUCT_REGISTRATION)
        if self.write is WriteStatus.READY:
            _require(
                self.auth is AuthStatus.READY, "auth != READY can never produce write=READY (A2)"
            )
            _require(
                PRODUCT_WRITE_PROVABLE,
                "M2 product write stays UNVERIFIED: its endpoints are NOT_ADOPTED (W1)",
            )
        if self.auth is AuthStatus.READY:
            _require(self.auth_verified_at is not None, "auth=READY needs its proof time (A1)")
            _require(authentication is None, "an open AUTHENTICATION overlay keeps auth from READY")
        if self.auth is AuthStatus.AUTH_MISMATCH:
            _require(
                authentication is not None
                and authentication.state is WorkflowState.REVIEW_REQUIRED,
                "AUTH_MISMATCH requires REVIEW_REQUIRED/AUTHENTICATION (A3)",
            )
        if self.write_scope.status is WriteScopeStatus.MISSING:
            _require(
                self.write is WriteStatus.BLOCKED and registration == SCOPE_INSUFFICIENT_PAUSE,
                "positive missing permission requires write=BLOCKED + "
                "PAUSED/PRODUCT_REGISTRATION/SCOPE_INSUFFICIENT (S2)",
            )
        if registration is not None and registration.reason_code is PauseReason.SCOPE_INSUFFICIENT:
            _require(
                self.write_scope.status is WriteScopeStatus.MISSING,
                "SCOPE_INSUFFICIENT needs positive missing-permission evidence (S3)",
            )
        if self.write is WriteStatus.BLOCKED:
            _require(
                registration is not None and registration.state is WorkflowState.PAUSED,
                "write=BLOCKED needs a proven PRODUCT_REGISTRATION pause",
            )

    def overlay(self, scope: WorkflowScope) -> WorkflowOverlay | None:
        return next((o for o in self.overlays if o.scope is scope), None)


INITIAL = CapabilityState()

# ---------------------------------------------------------------- evidence


@dataclass(frozen=True)
class Generations:
    """Committed credential and session generations (AUTH.md §13)."""

    credential: int
    session: int

    def __post_init__(self) -> None:
        _require(
            isinstance(self.credential, int) and isinstance(self.session, int),
            "generations are integers",
        )


@dataclass(frozen=True)
class IdentityProof:
    """One protected seller-account read and the generations it belongs to (AUTH.md §13.3)."""

    generations: Generations
    observed_account_uid: str
    proven_at: datetime

    def __post_init__(self) -> None:
        _require(bool(self.observed_account_uid), "an identity proof carries the observed uid")
        _require(self.proven_at.tzinfo is not None, "proven_at must be timezone-aware")


@dataclass(frozen=True)
class AuthEvidence:
    """Current authentication evidence. The adapter supplies it (PR-A); here, fixtures do."""

    binding_committed: bool
    expected_account_uid: str | None
    current: Generations | None
    proof: IdentityProof | None


class Finding(StrEnum):
    """What bounded diagnostics established about a failure (§11). Typed findings are the only
    source of a durable reason: provider codes and error classes never select one (§3, §9.3)."""

    RECOVERABLE = "RECOVERABLE"
    AUTH_RECOVERY_EXHAUSTED = "AUTH_RECOVERY_EXHAUSTED"
    APPLICATION_REAUTH_SUSPECTED = "APPLICATION_REAUTH_SUSPECTED"
    # Honoured only under an accepted SMARTSTORE-R0-APP-REAUTH detection contract (§9.2).
    APPLICATION_REAUTH_DETECTED = "APPLICATION_REAUTH_DETECTED"
    ACCOUNT_RESTRICTION_PROVEN = "ACCOUNT_RESTRICTION_PROVEN"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class FailureEvidence:
    scope: WorkflowScope
    error_class: ErrorClass
    finding: Finding
    remote_outcome: RemoteOutcome | None = None
    session_generation: int | None = None
    # Adapter diagnostics such as "GW.AUTHN"; never selects durable state.
    provider_code: str | None = None

    def __post_init__(self) -> None:
        _typed(self.scope, WorkflowScope, "workflow_scope")
        _typed(self.error_class, ErrorClass, "error_class")
        _typed(self.finding, Finding, "finding")
        _typed(self.remote_outcome, RemoteOutcome, "remote_outcome", optional=True)


class Resolution(StrEnum):
    """An explicit operator action that lifts an overlay (§9, §10)."""

    RESUME = "RESUME"
    REVIEW_RESOLVED = "REVIEW_RESOLVED"
    PROVIDER_REAUTH_COMPLETED = "PROVIDER_REAUTH_COMPLETED"


@dataclass(frozen=True)
class CapabilityPolicy:
    app_reauth_detection_accepted: bool = APP_REAUTH_DETECTION_ACCEPTED


DEFAULT_POLICY = CapabilityPolicy()


class ContractDecision(StrEnum):
    """What a caller intends to do under the current contract freshness (§8 F2)."""

    CONTINUE_PROVEN_BEHAVIOR = "CONTINUE_PROVEN_BEHAVIOR"
    ADOPT_ENDPOINT = "ADOPT_ENDPOINT"
    WIDEN_ALLOW_LIST = "WIDEN_ALLOW_LIST"
    PROMOTE_UNVERIFIED_CAPABILITY = "PROMOTE_UNVERIFIED_CAPABILITY"
    CHANGE_RETRY_OR_REPLAY = "CHANGE_RETRY_OR_REPLAY"
    CHANGE_PERMISSION_MAPPING = "CHANGE_PERMISSION_MAPPING"
    CHANGE_PROVIDER_ASSUMPTIONS = "CHANGE_PROVIDER_ASSUMPTIONS"
    EXPAND_ACCOUNT_MODES = "EXPAND_ACCOUNT_MODES"


EXPANSION_DECISIONS = frozenset(ContractDecision) - {ContractDecision.CONTINUE_PROVEN_BEHAVIOR}

# ---------------------------------------------------------------- gates


def freshness_allows(
    freshness: ContractFreshness,
    decision: ContractDecision,
    *,
    depends_on_disputed_invariant: bool = False,
) -> bool:
    """F2/F3: STALE keeps proven behaviour and blocks expansion without extra judgement;
    REVIEW_REQUIRED also stops whatever depends on the disputed invariant."""
    _typed(freshness, ContractFreshness, "contract_freshness")
    _typed(decision, ContractDecision, "decision")
    if freshness is ContractFreshness.CURRENT:
        return True
    if decision in EXPANSION_DECISIONS:
        return False
    return not (freshness is ContractFreshness.REVIEW_REQUIRED and depends_on_disputed_invariant)


def _gate(freshness: ContractFreshness, decision: ContractDecision) -> None:
    if not freshness_allows(freshness, decision):
        raise ExpansionBlockedError(f"{decision} is blocked while contract_freshness={freshness}")


# ---------------------------------------------------------------- transitions


def _put(
    overlays: tuple[WorkflowOverlay, ...], overlay: WorkflowOverlay
) -> tuple[WorkflowOverlay, ...]:
    return (*(o for o in overlays if o.scope is not overlay.scope), overlay)


def _drop(
    overlays: tuple[WorkflowOverlay, ...], scope: WorkflowScope
) -> tuple[WorkflowOverlay, ...]:
    return tuple(o for o in overlays if o.scope is not scope)


def _find(overlays: tuple[WorkflowOverlay, ...], scope: WorkflowScope) -> WorkflowOverlay | None:
    return next((o for o in overlays if o.scope is scope), None)


def _write_for(write_scope: WriteScope, overlays: tuple[WorkflowOverlay, ...]) -> WriteStatus:
    registration = _find(overlays, WorkflowScope.PRODUCT_REGISTRATION)
    if write_scope.status is WriteScopeStatus.MISSING or (
        registration is not None and registration.state is WorkflowState.PAUSED
    ):
        return WriteStatus.BLOCKED
    # W1: no M2 evidence can prove product write.
    return WriteStatus.UNVERIFIED


def _measure_auth(
    evidence: AuthEvidence, *, floor: int | None
) -> tuple[AuthStatus, datetime | None]:
    if not evidence.binding_committed or not evidence.expected_account_uid:
        return AuthStatus.NOT_BOUND, None  # A4
    proof, current = evidence.proof, evidence.current
    if proof is None or current is None or proof.generations != current:
        return AuthStatus.NOT_READY, None  # A1: old credential/session evidence proves nothing now
    if floor is not None and current.session <= floor:
        return AuthStatus.NOT_READY, None  # §17 #15: the paused session never comes back READY
    if proof.observed_account_uid != evidence.expected_account_uid:
        return AuthStatus.AUTH_MISMATCH, None
    return AuthStatus.READY, proof.proven_at


def observe_auth(state: CapabilityState, evidence: AuthEvidence) -> CapabilityState:
    """Converge ``auth`` on current authentication evidence (A1–A4)."""
    _require(isinstance(evidence, AuthEvidence), "evidence must be AuthEvidence")
    measured, proven_at = _measure_auth(evidence, floor=state.session_generation_floor)
    auth, overlays, verified_at = measured, state.overlays, state.auth_verified_at
    if measured is AuthStatus.AUTH_MISMATCH:
        # A3: human review with no PAUSED reason. The expected identity is never replaced here, so
        # the observed account can never be bound automatically (§17 #2).
        overlays = _put(
            overlays, WorkflowOverlay(WorkflowState.REVIEW_REQUIRED, WorkflowScope.AUTHENTICATION)
        )
    elif measured is AuthStatus.READY:
        if state.overlay(WorkflowScope.AUTHENTICATION) is not None:
            # Only an operator lifts a human-action overlay; a matching proof alone never does.
            auth = AuthStatus.NOT_READY
        else:
            if verified_at is None:
                # The first proof of an account is new trust (F2).
                _gate(state.contract_freshness, ContractDecision.PROMOTE_UNVERIFIED_CAPABILITY)
            verified_at = proven_at
    return replace(
        state,
        auth=auth,
        overlays=overlays,
        write=_write_for(state.write_scope, overlays),
        auth_verified_at=verified_at,
    )


def observe_permission(state: CapabilityState, write_scope: WriteScope) -> CapabilityState:
    """Converge on new permission evidence (S1–S4). Authentication is untouched (S2)."""
    _require(isinstance(write_scope, WriteScope), "write_scope must be a WriteScope")
    if (
        write_scope.status is WriteScopeStatus.READY
        and state.write_scope.status is not WriteScopeStatus.READY
    ):
        _gate(state.contract_freshness, ContractDecision.PROMOTE_UNVERIFIED_CAPABILITY)
    overlays = state.overlays
    registration = state.overlay(WorkflowScope.PRODUCT_REGISTRATION)
    if write_scope.status is WriteScopeStatus.MISSING:
        overlays = _put(overlays, SCOPE_INSUFFICIENT_PAUSE)  # S2
    elif registration is not None and registration.reason_code is PauseReason.SCOPE_INSUFFICIENT:
        # That pause was the missing-permission evidence itself; fresh evidence lifts it.
        overlays = _drop(overlays, WorkflowScope.PRODUCT_REGISTRATION)
    return replace(
        state, write_scope=write_scope, overlays=overlays, write=_write_for(write_scope, overlays)
    )


def _overlay_for(failure: FailureEvidence, policy: CapabilityPolicy) -> WorkflowOverlay | None:
    review, paused = WorkflowState.REVIEW_REQUIRED, WorkflowState.PAUSED
    authentication = WorkflowScope.AUTHENTICATION
    if failure.remote_outcome is RemoteOutcome.UNKNOWN:
        # W3: reconcile first; unresolved ambiguity is a review, never a blind replay.
        return WorkflowOverlay(review, failure.scope)
    finding = failure.finding
    if finding is Finding.RECOVERABLE:
        return None
    if finding is Finding.AUTH_RECOVERY_EXHAUSTED:
        return WorkflowOverlay(paused, authentication, PauseReason.AUTH_RETRY_LIMIT)  # A5
    if (
        finding is Finding.APPLICATION_REAUTH_DETECTED
        and policy.app_reauth_detection_accepted
        and failure.session_generation is not None
    ):
        return WorkflowOverlay(
            paused,
            authentication,
            PauseReason.APPLICATION_REAUTH_REQUIRED,
            failure.session_generation,
        )
    if finding in (Finding.APPLICATION_REAUTH_SUSPECTED, Finding.APPLICATION_REAUTH_DETECTED):
        # §9.2: without an accepted detection contract the reason is never fabricated.
        return WorkflowOverlay(review, authentication)
    if finding is Finding.ACCOUNT_RESTRICTION_PROVEN:
        return WorkflowOverlay(paused, failure.scope, PauseReason.ACCOUNT_RESTRICTED)
    return WorkflowOverlay(review, failure.scope)  # UNRESOLVED


def _converge_overlay(state: CapabilityState, proposed: WorkflowOverlay) -> WorkflowOverlay:
    """The overlay a scope holds once ``proposed`` arrives (PR #26 re-review 5193155486, #27).

    Current evidence narrows uncertainty: an open REVIEW_REQUIRED becomes the PAUSED reason that
    later evidence positively proves. Ambiguity never erases a proven reason, and one proven
    reason never silently replaces another. The review an AUTH_MISMATCH requires stands until an
    operator resolves it (A3).
    """
    existing = state.overlay(proposed.scope)
    if existing is None:
        return proposed
    if (
        existing.state is WorkflowState.REVIEW_REQUIRED
        and proposed.state is WorkflowState.PAUSED
        and not (
            existing.scope is WorkflowScope.AUTHENTICATION
            and state.auth is AuthStatus.AUTH_MISMATCH
        )
    ):
        return proposed
    return existing


def observe_failure(
    state: CapabilityState, failure: FailureEvidence, policy: CapabilityPolicy = DEFAULT_POLICY
) -> CapabilityState:
    """Record a classified failure and converge the workflow overlay (§11).

    ``error_class`` is kept exactly as measured: an exhausted budget or a review never
    reclassifies it (A5, ERRORS.md §1). Overlays converge per ``_converge_overlay``. A failure
    that carries no mutation outcome leaves a prior ``remote_outcome`` as it was: it proves
    nothing about that mutation, so an UNKNOWN outcome still forbids blind replay (W3).
    """
    _require(isinstance(failure, FailureEvidence), "failure must be FailureEvidence")
    proposed = _overlay_for(failure, policy)
    overlays = state.overlays
    if proposed is not None:
        converged = _converge_overlay(state, proposed)
        if converged != state.overlay(proposed.scope):
            overlays = _put(overlays, converged)
    touched = {failure.scope} | ({proposed.scope} if proposed is not None else set())
    auth = state.auth
    if auth is AuthStatus.READY and WorkflowScope.AUTHENTICATION in touched:
        # The current proof just failed; recovery in progress is never READY (§14.3).
        auth = AuthStatus.NOT_READY
    return replace(
        state,
        auth=auth,
        overlays=overlays,
        write=_write_for(state.write_scope, overlays),
        error_class=failure.error_class,
        remote_outcome=(
            failure.remote_outcome if failure.remote_outcome is not None else state.remote_outcome
        ),
    )


def _resolution_for(overlay: WorkflowOverlay) -> Resolution | None:
    if overlay.state is WorkflowState.REVIEW_REQUIRED:
        return Resolution.REVIEW_RESOLVED
    if overlay.reason_code is PauseReason.APPLICATION_REAUTH_REQUIRED:
        return Resolution.PROVIDER_REAUTH_COMPLETED
    if overlay.reason_code is PauseReason.SCOPE_INSUFFICIENT:
        return None  # lifted only by fresh permission evidence
    return Resolution.RESUME


def resolve(
    state: CapabilityState, scope: WorkflowScope, resolution: Resolution
) -> CapabilityState:
    """An explicit operator action lifts one overlay. It never makes anything READY: READY
    still needs fresh evidence afterwards."""
    _typed(scope, WorkflowScope, "workflow_scope")
    _typed(resolution, Resolution, "resolution")
    overlay = state.overlay(scope)
    if overlay is None:
        raise CapabilityInvariantError(f"no {scope} overlay to resolve")
    required = _resolution_for(overlay)
    if required is None:
        raise CapabilityInvariantError(
            "SCOPE_INSUFFICIENT lifts only with fresh permission evidence"
        )
    _require(
        resolution is required, f"this {overlay.state} is lifted by {required}, not {resolution}"
    )
    floor = state.session_generation_floor
    if overlay.session_generation is not None:
        floor = max(
            floor if floor is not None else overlay.session_generation, overlay.session_generation
        )
    auth = state.auth
    if scope is WorkflowScope.AUTHENTICATION and auth is AuthStatus.AUTH_MISMATCH:
        auth = AuthStatus.NOT_READY  # reviewed; READY still needs a fresh matching proof
    overlays = _drop(state.overlays, scope)
    return replace(
        state,
        auth=auth,
        overlays=overlays,
        write=_write_for(state.write_scope, overlays),
        session_generation_floor=floor,
    )


def record_freshness(state: CapabilityState, freshness: ContractFreshness) -> CapabilityState:
    """F1/F4: freshness is its own axis; it never demotes nor promotes another."""
    _typed(freshness, ContractFreshness, "contract_freshness")
    return replace(state, contract_freshness=freshness)


def on_process_start(state: CapabilityState) -> CapabilityState:
    """A new process holds no proof: a persisted READY is never trusted (ACCOUNT_IDENTITY §8,
    AUTH.md §17). Human-action overlays and a mismatch survive until an operator acts."""
    if state.auth is not AuthStatus.READY:
        return state
    return replace(state, auth=AuthStatus.NOT_READY)
