"""SmartStore capability truth model (M2 PR-B): fixtures only, no credential, no provider call.

Each ``test_s17_NN_*`` is a named test for CAPABILITY_MAPPING.md §17 target NN at the domain
layer (ownership: CAPABILITY_MAPPING_IMPLEMENTATION_OWNERSHIP.md). Targets 16 and 17, and the
persistence/service/API portion of 19, are proven in
tests/integration/test_marketplace_capability_store.py. Target 8 belongs to PR-A and target 18
to PR-C, so neither has a stand-in here.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import product

import pytest

from app.connect.marketplace.capability import (
    APP_REAUTH_DETECTION_ACCEPTED,
    EVIDENCE_GRADES,
    EXPANSION_DECISIONS,
    FRESHNESS_TRANSITIONS,
    INITIAL,
    PERMISSION_UNKNOWN,
    PRODUCT_WRITE_PROVABLE,
    REASON_SCOPES,
    SCOPE_INSUFFICIENT_PAUSE,
    AuthEvidence,
    AuthStatus,
    CapabilityInvariantError,
    CapabilityPolicy,
    CapabilityState,
    ContractDecision,
    ContractFreshness,
    EvidenceGrade,
    EvidenceStrength,
    ExpansionBlockedError,
    FailureEvidence,
    Finding,
    FreshnessTransitionError,
    Generations,
    IdentityProof,
    PauseReason,
    RemoteOutcome,
    Resolution,
    WorkflowOverlay,
    WorkflowScope,
    WorkflowState,
    WriteScope,
    WriteScopeStatus,
    WriteStatus,
    freshness_after_upstream_change,
    freshness_allows,
    observe_auth,
    observe_failure,
    observe_permission,
    on_process_start,
    record_freshness,
    resolve,
)
from app.core.errors import ErrorClass

T0 = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
EXPECTED = "account-uid-A"
OTHER = "account-uid-B"
AUTH = WorkflowScope.AUTHENTICATION
REGISTRATION = WorkflowScope.PRODUCT_REGISTRATION
REVIEW = WorkflowState.REVIEW_REQUIRED
PAUSED = WorkflowState.PAUSED
UNRECORDED = ContractFreshness.UNRECORDED
CURRENT = ContractFreshness.CURRENT
STALE = ContractFreshness.STALE
DISPUTED = ContractFreshness.REVIEW_REQUIRED
CONTINUE = ContractDecision.CONTINUE_PROVEN_BEHAVIOR
ATTESTED = WriteScope(WriteScopeStatus.READY, EvidenceStrength.OPERATOR_ATTESTED)
MACHINE = WriteScope(WriteScopeStatus.READY, EvidenceStrength.MACHINE_VERIFIED)
MISSING = WriteScope(WriteScopeStatus.MISSING, EvidenceStrength.OPERATOR_ATTESTED)
POLICIES = (CapabilityPolicy(), CapabilityPolicy(app_reauth_detection_accepted=True))


def _evidence(
    observed: str = EXPECTED, *, proof_session: int = 1, current_session: int = 1
) -> AuthEvidence:
    return AuthEvidence(
        binding_committed=True,
        expected_account_uid=EXPECTED,
        current=Generations(credential=1, session=current_session),
        proof=IdentityProof(Generations(credential=1, session=proof_session), observed, T0),
    )


def _recorded(state: CapabilityState, freshness: ContractFreshness) -> CapabilityState:
    """A reviewed freshness recording at T0."""
    return record_freshness(state, freshness, recorded_at=T0)


# A state whose current contract freshness has been recorded; it is never assumed.
RECORDED = _recorded(INITIAL, CURRENT)


def _at(freshness: ContractFreshness) -> CapabilityState:
    """A state that holds ``freshness``, reached only through lawful recordings."""
    if freshness is UNRECORDED:
        return INITIAL
    return RECORDED if freshness is CURRENT else _recorded(RECORDED, freshness)


def _ready() -> CapabilityState:
    state = observe_auth(RECORDED, _evidence())
    assert state.auth is AuthStatus.READY
    return state


def _axes(state: CapabilityState) -> dict[str, object]:
    return {
        "auth": state.auth,
        "write_scope.status": state.write_scope.status,
        "write.status": state.write,
        "contract_freshness": state.contract_freshness,
        "workflow": tuple((o.state, o.scope, o.reason_code) for o in state.overlays),
        "error_class": state.error_class,
        "remote_outcome": state.remote_outcome,
    }


def _changed(before: CapabilityState, after: CapabilityState) -> set[str]:
    old, new = _axes(before), _axes(after)
    return {axis for axis in old if old[axis] != new[axis]}


# ---------------------------------------------------------------- the axes themselves


@pytest.mark.parametrize(
    ("axis", "values"),
    [
        (AuthStatus, {"READY", "NOT_READY", "AUTH_MISMATCH", "NOT_BOUND"}),
        (WriteScopeStatus, {"READY", "MISSING", "UNKNOWN"}),
        (EvidenceStrength, {"OPERATOR_ATTESTED", "MACHINE_VERIFIED"}),
        (WriteStatus, {"UNVERIFIED", "READY", "BLOCKED"}),
        (ContractFreshness, {"UNRECORDED", "CURRENT", "STALE", "REVIEW_REQUIRED"}),
        (WorkflowState, {"PAUSED", "REVIEW_REQUIRED"}),
        (WorkflowScope, {"AUTHENTICATION", "PRODUCT_REGISTRATION"}),
        (
            PauseReason,
            {
                "AUTH_RETRY_LIMIT",
                "APPLICATION_REAUTH_REQUIRED",
                "SCOPE_INSUFFICIENT",
                "ACCOUNT_RESTRICTED",
            },
        ),
        (RemoteOutcome, {"APPLIED_PROVEN", "NOT_APPLIED_PROVEN", "UNKNOWN"}),
    ],
)
def test_each_axis_holds_exactly_its_contract_values(axis: type[StrEnum], values: set[str]) -> None:
    # No convenience composite (READY_TO_REGISTER, AUTH_OR_SCOPE_ERROR, ...) can hide in an axis.
    assert {member.value for member in axis} == values


def test_the_nine_axes_move_independently() -> None:
    base = observe_permission(_ready(), ATTESTED)
    assert _changed(base, _recorded(base, STALE)) == {"contract_freshness"}
    assert _changed(base, observe_permission(base, PERMISSION_UNKNOWN)) == {"write_scope.status"}
    transient = FailureEvidence(REGISTRATION, ErrorClass.TRANSIENT, Finding.RECOVERABLE)
    assert _changed(base, observe_failure(base, transient)) == {"error_class"}
    rejected = FailureEvidence(
        REGISTRATION,
        ErrorClass.VALIDATION,
        Finding.RECOVERABLE,
        remote_outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
    )
    assert _changed(base, observe_failure(base, rejected)) == {"error_class", "remote_outcome"}
    # A cause class and a workflow state are different axes (ERRORS.md §2.3).
    unresolved = observe_failure(
        base, FailureEvidence(REGISTRATION, ErrorClass.TRANSIENT, Finding.UNRESOLVED)
    )
    assert _changed(base, unresolved) == {"error_class", "workflow"}
    assert unresolved.error_class is ErrorClass.TRANSIENT
    assert unresolved.overlays == (WorkflowOverlay(REVIEW, REGISTRATION),)
    # Losing the authentication proof leaves permission evidence alone.
    assert _changed(base, observe_auth(base, _evidence(current_session=2))) == {"auth"}


def test_free_text_never_stands_in_for_an_axis_value() -> None:
    with pytest.raises(CapabilityInvariantError, match="auth"):
        CapabilityState(auth="NOT_BOUND")  # type: ignore[arg-type]
    with pytest.raises(CapabilityInvariantError, match=r"write\.status"):
        CapabilityState(write="UNVERIFIED")  # type: ignore[arg-type]
    with pytest.raises(CapabilityInvariantError, match="contract_freshness"):
        CapabilityState(contract_freshness="UNRECORDED")  # type: ignore[arg-type]
    with pytest.raises(CapabilityInvariantError, match="error_class"):
        CapabilityState(error_class="AUTH")  # type: ignore[arg-type]
    with pytest.raises(CapabilityInvariantError, match="remote_outcome"):
        CapabilityState(remote_outcome="UNKNOWN")  # type: ignore[arg-type]
    with pytest.raises(CapabilityInvariantError, match=r"write_scope\.status"):
        WriteScope("UNKNOWN")  # type: ignore[arg-type]


# ---------------------------------------------------------------- §17, PR-B-owned targets


def test_s17_01_auth_not_ready_can_never_produce_write_ready() -> None:
    for auth in AuthStatus:
        if auth is AuthStatus.READY:
            continue
        with pytest.raises(CapabilityInvariantError, match="A2"):
            CapabilityState(auth=auth, write=WriteStatus.READY)
    not_ready = [
        INITIAL,
        observe_permission(RECORDED, MACHINE),
        observe_auth(_ready(), _evidence(OTHER)),
        observe_auth(_ready(), _evidence(current_session=2)),
        observe_failure(
            _ready(), FailureEvidence(AUTH, ErrorClass.AUTH, Finding.AUTH_RECOVERY_EXHAUSTED)
        ),
    ]
    for state in not_ready:
        assert state.auth is not AuthStatus.READY
        assert state.write is not WriteStatus.READY


def test_s17_02_auth_mismatch_requires_review_and_never_rebinds() -> None:
    mismatch = observe_auth(_ready(), _evidence(OTHER))
    assert mismatch.auth is AuthStatus.AUTH_MISMATCH
    assert mismatch.overlays == (WorkflowOverlay(REVIEW, AUTH),)  # reason_code is None
    assert mismatch.write is not WriteStatus.READY
    # The observed account never becomes the expected one: repeating it keeps the mismatch ...
    assert observe_auth(mismatch, _evidence(OTHER)).auth is AuthStatus.AUTH_MISMATCH
    # ... and even a matching proof does not lift the review on its own.
    matching = observe_auth(mismatch, _evidence(proof_session=2, current_session=2))
    assert matching.auth is AuthStatus.NOT_READY
    assert matching.overlays == (WorkflowOverlay(REVIEW, AUTH),)
    with pytest.raises(CapabilityInvariantError, match="A3"):
        CapabilityState(auth=AuthStatus.AUTH_MISMATCH)
    # Only an explicit operator resolution lifts it, and READY still needs a fresh proof.
    with pytest.raises(CapabilityInvariantError):
        resolve(mismatch, AUTH, Resolution.RESUME)
    reviewed = resolve(mismatch, AUTH, Resolution.REVIEW_RESOLVED)
    assert (reviewed.auth, reviewed.overlays) == (AuthStatus.NOT_READY, ())
    assert observe_auth(reviewed, _evidence(OTHER)).auth is AuthStatus.AUTH_MISMATCH
    assert observe_auth(reviewed, _evidence(proof_session=2, current_session=2)).auth is (
        AuthStatus.READY
    )


def test_s17_03_positive_missing_scope_blocks_write_and_pauses_registration() -> None:
    blocked = observe_permission(_ready(), MISSING)
    assert blocked.write_scope == MISSING
    assert blocked.write is WriteStatus.BLOCKED
    assert blocked.overlays == (
        WorkflowOverlay(PAUSED, REGISTRATION, PauseReason.SCOPE_INSUFFICIENT),
    )
    assert blocked.auth is AuthStatus.READY  # authentication may remain READY
    with pytest.raises(CapabilityInvariantError, match="S2"):
        CapabilityState(write_scope=MISSING)
    with pytest.raises(CapabilityInvariantError, match="S2"):
        CapabilityState(write_scope=MISSING, write=WriteStatus.BLOCKED)
    # Fresh positive evidence is the remediation; it lifts the pause and the block.
    lifted = observe_permission(blocked, ATTESTED)
    assert (lifted.write, lifted.overlays) == (WriteStatus.UNVERIFIED, ())


def test_s17_04_unknown_scope_never_becomes_scope_insufficient_without_positive_evidence() -> None:
    state = _ready()
    assert state.write_scope == PERMISSION_UNKNOWN  # no attestation at all
    outcomes: tuple[RemoteOutcome | None, ...] = (None, *RemoteOutcome)
    for scope, error_class, finding, outcome, policy in product(
        WorkflowScope, ErrorClass, Finding, outcomes, POLICIES
    ):
        failure = FailureEvidence(
            scope, error_class, finding, outcome, session_generation=1, provider_code="GW.AUTHN"
        )
        after = observe_failure(state, failure, policy)
        assert after.write_scope == PERMISSION_UNKNOWN
        assert PauseReason.SCOPE_INSUFFICIENT not in {o.reason_code for o in after.overlays}
    assert observe_permission(state, PERMISSION_UNKNOWN).overlays == ()
    with pytest.raises(CapabilityInvariantError):
        WriteScope(WriteScopeStatus.UNKNOWN, EvidenceStrength.OPERATOR_ATTESTED)
    with pytest.raises(CapabilityInvariantError, match="S3"):
        CapabilityState(write=WriteStatus.BLOCKED, overlays=(SCOPE_INSUFFICIENT_PAUSE,))


def test_s17_05_workflow_scope_is_the_frozen_enum_never_free_text() -> None:
    assert {scope.value for scope in WorkflowScope} == {"AUTHENTICATION", "PRODUCT_REGISTRATION"}
    for text in ("AUTHENTICATION", "PRODUCT_REGISTRATION", "SMARTSTORE_PRODUCT_API", "상품 등록"):
        with pytest.raises(CapabilityInvariantError, match="workflow_scope"):
            WorkflowOverlay(REVIEW, text)  # type: ignore[arg-type]
        with pytest.raises(CapabilityInvariantError, match="workflow_scope"):
            FailureEvidence(text, ErrorClass.UNKNOWN, Finding.UNRESOLVED)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WorkflowScope("SMARTSTORE_PRODUCT_API")


def test_s17_06_operator_attested_permission_keeps_limited_strength() -> None:
    attested = observe_permission(_ready(), ATTESTED)
    assert attested.write_scope.evidence_strength is EvidenceStrength.OPERATOR_ATTESTED
    assert attested.write_scope.evidence_grade is EvidenceGrade.LIMITED
    assert attested.write is WriteStatus.UNVERIFIED  # attestation never promotes write
    with pytest.raises(CapabilityInvariantError, match="evidence_strength"):
        WriteScope(WriteScopeStatus.READY)  # a bare READY that hides its strength
    assert EVIDENCE_GRADES[EvidenceStrength.OPERATOR_ATTESTED] is EvidenceGrade.LIMITED


def test_s17_07_machine_verified_permission_has_a_distinct_strong_strength() -> None:
    assert MACHINE.evidence_grade is EvidenceGrade.STRONG
    assert MACHINE.evidence_grade is not ATTESTED.evidence_grade
    assert MACHINE != ATTESTED
    # A blocker or an unknown never borrows a positive marker (§14.1).
    assert WriteScope(
        WriteScopeStatus.MISSING, EvidenceStrength.MACHINE_VERIFIED
    ).evidence_grade is (None)
    assert PERMISSION_UNKNOWN.evidence_grade is None


def test_s17_09_m2_product_write_can_never_become_ready() -> None:
    assert PRODUCT_WRITE_PROVABLE is False
    with pytest.raises(CapabilityInvariantError, match="W1"):
        CapabilityState(auth=AuthStatus.READY, auth_verified_at=T0, write=WriteStatus.READY)
    best = observe_permission(_ready(), MACHINE)
    reached = [
        best,
        _recorded(best, CURRENT),
        observe_auth(best, _evidence(proof_session=2, current_session=2)),
        observe_failure(
            best,
            FailureEvidence(
                REGISTRATION,
                ErrorClass.TRANSIENT,
                Finding.RECOVERABLE,
                remote_outcome=RemoteOutcome.APPLIED_PROVEN,
            ),
        ),
        on_process_start(best),
    ]
    for state in reached:
        assert state.write is WriteStatus.UNVERIFIED


def test_s17_10_stale_keeps_runtime_proof_and_blocks_expansion() -> None:
    proven = observe_permission(_ready(), ATTESTED)
    stale = _recorded(proven, STALE)
    assert _changed(proven, stale) == {"contract_freshness"}  # F1: no mechanical demotion
    # Existing proven behaviour continues without an extra safety judgement.
    assert observe_auth(stale, _evidence(proof_session=2, current_session=2)).auth is (
        AuthStatus.READY
    )
    assert freshness_allows(STALE, CONTINUE, depends_on_disputed_invariant=True)
    for decision in EXPANSION_DECISIONS:
        assert not freshness_allows(STALE, decision)
    # New trust is refused by the state machine itself.
    with pytest.raises(ExpansionBlockedError):
        observe_auth(_recorded(RECORDED, STALE), _evidence())
    with pytest.raises(ExpansionBlockedError):
        observe_permission(_recorded(_ready(), STALE), ATTESTED)


def test_s17_11_review_required_freshness_stops_dependent_operations() -> None:
    assert not freshness_allows(DISPUTED, CONTINUE, depends_on_disputed_invariant=True)
    assert freshness_allows(DISPUTED, CONTINUE, depends_on_disputed_invariant=False)
    for decision in EXPANSION_DECISIONS:
        assert not freshness_allows(DISPUTED, decision)
    # Stronger than STALE: the same dependent operation still runs under STALE.
    assert freshness_allows(STALE, CONTINUE, depends_on_disputed_invariant=True)
    # The axis converges nothing else by itself (F9).
    assert _changed(_ready(), _recorded(_ready(), DISPUTED)) == {"contract_freshness"}
    with pytest.raises(ExpansionBlockedError):
        observe_auth(_recorded(RECORDED, DISPUTED), _evidence())


def test_s17_12_paused_requires_one_frozen_reason_and_one_frozen_scope() -> None:
    with pytest.raises(CapabilityInvariantError, match="reason_code"):
        WorkflowOverlay(PAUSED, AUTH)
    with pytest.raises(CapabilityInvariantError, match="reason_code"):
        WorkflowOverlay(PAUSED, AUTH, "AUTH_RETRY_LIMIT")  # type: ignore[arg-type]
    with pytest.raises(CapabilityInvariantError, match="does not apply"):
        WorkflowOverlay(PAUSED, REGISTRATION, PauseReason.AUTH_RETRY_LIMIT)
    with pytest.raises(CapabilityInvariantError, match="does not apply"):
        WorkflowOverlay(PAUSED, AUTH, PauseReason.SCOPE_INSUFFICIENT)
    with pytest.raises(CapabilityInvariantError, match="never carries"):
        WorkflowOverlay(REVIEW, AUTH, PauseReason.AUTH_RETRY_LIMIT)
    with pytest.raises(CapabilityInvariantError, match="one overlay"):
        CapabilityState(
            overlays=(
                WorkflowOverlay(REVIEW, AUTH),
                WorkflowOverlay(PAUSED, AUTH, PauseReason.AUTH_RETRY_LIMIT),
            )
        )
    for reason, scopes in REASON_SCOPES.items():
        generation = 1 if reason is PauseReason.APPLICATION_REAUTH_REQUIRED else None
        for scope in scopes:
            assert WorkflowOverlay(PAUSED, scope, reason, generation).reason_code is reason


GENERIC_GW_AUTHN = [
    FailureEvidence(scope, error_class, finding, session_generation=1, provider_code="GW.AUTHN")
    for scope in WorkflowScope
    for error_class in (ErrorClass.AUTH, ErrorClass.UNKNOWN)
    for finding in (
        Finding.RECOVERABLE,
        Finding.UNRESOLVED,
        Finding.APPLICATION_REAUTH_SUSPECTED,
        Finding.AUTH_RECOVERY_EXHAUSTED,
    )
]


def test_s17_13_generic_gw_authn_never_maps_to_scope_or_app_reauth_reasons() -> None:
    forbidden = {PauseReason.SCOPE_INSUFFICIENT, PauseReason.APPLICATION_REAUTH_REQUIRED}
    for failure, policy in product(GENERIC_GW_AUTHN, POLICIES):
        after = observe_failure(_ready(), failure, policy)
        assert {o.reason_code for o in after.overlays}.isdisjoint(forbidden)
        assert after.error_class is failure.error_class  # the measured class is kept
        # The provider code selects nothing: without it the evidence converges identically.
        assert after == observe_failure(_ready(), replace(failure, provider_code=None), policy)
    unresolved = observe_failure(
        _ready(),
        FailureEvidence(AUTH, ErrorClass.UNKNOWN, Finding.UNRESOLVED, provider_code="GW.AUTHN"),
    )
    assert unresolved.overlays == (WorkflowOverlay(REVIEW, AUTH),)
    assert unresolved.auth is AuthStatus.NOT_READY


def test_s17_14_suspected_app_reauth_before_r0_is_review_not_pause() -> None:
    assert APP_REAUTH_DETECTION_ACCEPTED is False
    for finding in (Finding.APPLICATION_REAUTH_SUSPECTED, Finding.APPLICATION_REAUTH_DETECTED):
        after = observe_failure(
            _ready(), FailureEvidence(AUTH, ErrorClass.AUTH, finding, session_generation=1)
        )
        assert after.auth is AuthStatus.NOT_READY
        assert after.overlays == (WorkflowOverlay(REVIEW, AUTH),)  # reason_code is None


def test_s17_15_app_reauth_recovery_needs_provider_reauth_fresh_session_and_identity_proof() -> (
    None
):
    accepted = CapabilityPolicy(app_reauth_detection_accepted=True)
    detected = FailureEvidence(
        AUTH, ErrorClass.AUTH, Finding.APPLICATION_REAUTH_DETECTED, session_generation=1
    )
    paused = observe_failure(_ready(), detected, accepted)
    assert paused.auth is AuthStatus.NOT_READY
    assert paused.overlays == (
        WorkflowOverlay(PAUSED, AUTH, PauseReason.APPLICATION_REAUTH_REQUIRED, 1),
    )
    # A fresh proof alone does not end the pause.
    assert observe_auth(paused, _evidence(proof_session=2, current_session=2)).auth is (
        AuthStatus.NOT_READY
    )
    # Provider-UI re-authentication is its own explicit resolution.
    for wrong in (Resolution.RESUME, Resolution.REVIEW_RESOLVED):
        with pytest.raises(CapabilityInvariantError):
            resolve(paused, AUTH, wrong)
    reauthenticated = resolve(paused, AUTH, Resolution.PROVIDER_REAUTH_COMPLETED)
    assert (reauthenticated.auth, reauthenticated.overlays) == (AuthStatus.NOT_READY, ())
    # The paused session never comes back READY, even with a matching proof of that session.
    assert observe_auth(reauthenticated, _evidence()).auth is AuthStatus.NOT_READY
    # A new committed session with a proof from the old one proves nothing either.
    assert observe_auth(reauthenticated, _evidence(current_session=2)).auth is AuthStatus.NOT_READY
    # Fresh session + fresh identity proof of the expected account: READY.
    fresh = observe_auth(reauthenticated, _evidence(proof_session=2, current_session=2))
    assert fresh.auth is AuthStatus.READY
    # And that proof must still match the expected account.
    other = observe_auth(reauthenticated, _evidence(OTHER, proof_session=2, current_session=2))
    assert other.auth is AuthStatus.AUTH_MISMATCH


# §17 #19 — CAPABILITY_MAPPING §8 F2, F5–F8


def test_s17_19_a_new_state_starts_unrecorded_and_claims_nothing() -> None:
    assert (INITIAL.contract_freshness, INITIAL.freshness_recorded_at) == (UNRECORDED, None)
    assert CapabilityState().contract_freshness is UNRECORDED
    # Until a reviewed recording there is no new trust: no first proof, no permission promotion ...
    with pytest.raises(ExpansionBlockedError):
        observe_auth(INITIAL, _evidence())
    with pytest.raises(ExpansionBlockedError):
        observe_permission(INITIAL, ATTESTED)
    # ... yet UNRECORDED claims no contradiction, so proven and fail-closed behaviour goes on.
    assert freshness_allows(UNRECORDED, CONTINUE, depends_on_disputed_invariant=True)
    assert observe_permission(INITIAL, MISSING).write is WriteStatus.BLOCKED
    assert observe_auth(RECORDED, _evidence()).auth is AuthStatus.READY
    # A recorded determination always carries its time; UNRECORDED never does.
    with pytest.raises(CapabilityInvariantError, match="UNRECORDED"):
        CapabilityState(freshness_recorded_at=T0)
    with pytest.raises(CapabilityInvariantError, match="freshness_recorded_at"):
        CapabilityState(contract_freshness=CURRENT)


# F5: (expansion / new trust, existing proven behavior, behavior depending on a disputed invariant)
F5_MATRIX = {
    CURRENT: ("allow", "allow", "allow"),
    UNRECORDED: ("block", "allow", "allow"),
    STALE: ("block", "allow", "allow"),
    DISPUTED: ("block", "allow", "block"),
}


def test_s17_19_freshness_gates_follow_the_f5_matrix() -> None:
    assert set(F5_MATRIX) == set(ContractFreshness)
    for freshness, (expansion, proven, disputed) in F5_MATRIX.items():
        for decision in EXPANSION_DECISIONS:
            for depends in (False, True):
                allowed = freshness_allows(
                    freshness, decision, depends_on_disputed_invariant=depends
                )
                assert allowed is (expansion == "allow"), (freshness, decision, depends)
        assert freshness_allows(freshness, CONTINUE) is (proven == "allow"), freshness
        assert freshness_allows(freshness, CONTINUE, depends_on_disputed_invariant=True) is (
            disputed == "allow"
        ), freshness


# F6 + F7: every allowed reviewed recording; anything else is refused.
ALLOWED_RECORDINGS = {
    (UNRECORDED, CURRENT),
    (CURRENT, CURRENT),
    (CURRENT, STALE),
    (CURRENT, DISPUTED),
    (STALE, STALE),
    (STALE, CURRENT),
    (STALE, DISPUTED),
    (DISPUTED, DISPUTED),
    (DISPUTED, CURRENT),
}


def test_s17_19_the_freshness_transition_graph_is_closed_and_non_reentrant() -> None:
    for source, target in product(ContractFreshness, ContractFreshness):
        state = _at(source)
        if (source, target) in ALLOWED_RECORDINGS:
            assert _recorded(state, target).contract_freshness is target
        else:
            with pytest.raises(FreshnessTransitionError):
                _recorded(state, target)
    forbidden = [
        (UNRECORDED, STALE),
        (UNRECORDED, DISPUTED),
        (UNRECORDED, UNRECORDED),
        (CURRENT, UNRECORDED),
        (STALE, UNRECORDED),
        (DISPUTED, UNRECORDED),
        (DISPUTED, STALE),
    ]
    assert not ALLOWED_RECORDINGS & set(forbidden)
    assert {(s, t) for s, targets in FRESHNESS_TRANSITIONS.items() for t in targets} == (
        ALLOWED_RECORDINGS
    )


def test_s17_19_same_value_recordings_refresh_their_provenance() -> None:
    later = T0 + timedelta(days=30)
    for freshness in (CURRENT, STALE, DISPUTED):
        state = _at(freshness)
        again = record_freshness(state, freshness, recorded_at=later)
        assert (again.contract_freshness, again.freshness_recorded_at) == (freshness, later)
        assert again != state  # an event, not a silent no-op
        assert _changed(state, again) == set()  # and it changes no axis
    with pytest.raises(FreshnessTransitionError):
        record_freshness(INITIAL, UNRECORDED, recorded_at=later)
    with pytest.raises(CapabilityInvariantError, match="timezone-aware"):
        record_freshness(RECORDED, CURRENT, recorded_at=datetime(2026, 9, 14))


def test_s17_19_an_upstream_change_goes_stale_unless_a_contradiction_is_proven() -> None:
    after = freshness_after_upstream_change
    assert after(CURRENT, contradiction_established=False) is STALE
    assert after(CURRENT, contradiction_established=True) is DISPUTED
    assert after(STALE, contradiction_established=False) is STALE
    assert after(STALE, contradiction_established=True) is DISPUTED
    assert after(DISPUTED, contradiction_established=False) is DISPUTED  # never softens
    # Nothing was ever determined, so nothing can have aged or be disputed.
    assert after(UNRECORDED, contradiction_established=True) is UNRECORDED
    for source, proven in product((CURRENT, STALE, DISPUTED), (False, True)):
        assert after(source, contradiction_established=proven) in FRESHNESS_TRANSITIONS[source]


# §17 #20 — CAPABILITY_MAPPING §10.1


def test_s17_20_a_later_proven_reason_narrows_an_open_review_to_a_pause() -> None:
    reviewed = observe_failure(
        _ready(), FailureEvidence(AUTH, ErrorClass.UNKNOWN, Finding.UNRESOLVED)
    )
    assert reviewed.overlays == (WorkflowOverlay(REVIEW, AUTH),)
    narrowed = observe_failure(
        reviewed, FailureEvidence(AUTH, ErrorClass.AUTH, Finding.AUTH_RECOVERY_EXHAUSTED)
    )
    assert narrowed.overlays == (WorkflowOverlay(PAUSED, AUTH, PauseReason.AUTH_RETRY_LIMIT),)
    assert narrowed.error_class is ErrorClass.AUTH
    # The same on PRODUCT_REGISTRATION: an unresolved review becomes a proven restriction.
    registration = observe_failure(
        _ready(), FailureEvidence(REGISTRATION, ErrorClass.UNKNOWN, Finding.UNRESOLVED)
    )
    restricted = observe_failure(
        registration,
        FailureEvidence(
            REGISTRATION, ErrorClass.POLICY_BLOCKED, Finding.ACCOUNT_RESTRICTION_PROVEN
        ),
    )
    assert restricted.overlays == (
        WorkflowOverlay(PAUSED, REGISTRATION, PauseReason.ACCOUNT_RESTRICTED),
    )
    assert restricted.write is WriteStatus.BLOCKED
    # A suspected application re-auth review becomes the proven pause once detection counts.
    suspected = observe_failure(
        _ready(),
        FailureEvidence(
            AUTH, ErrorClass.AUTH, Finding.APPLICATION_REAUTH_SUSPECTED, session_generation=1
        ),
    )
    detected = observe_failure(
        suspected,
        FailureEvidence(
            AUTH, ErrorClass.AUTH, Finding.APPLICATION_REAUTH_DETECTED, session_generation=1
        ),
        CapabilityPolicy(app_reauth_detection_accepted=True),
    )
    assert detected.overlays == (
        WorkflowOverlay(PAUSED, AUTH, PauseReason.APPLICATION_REAUTH_REQUIRED, 1),
    )


def test_s17_20_ambiguity_never_erases_a_proven_pause() -> None:
    paused = observe_failure(
        _ready(), FailureEvidence(AUTH, ErrorClass.AUTH, Finding.AUTH_RECOVERY_EXHAUSTED)
    )
    later = [
        FailureEvidence(AUTH, ErrorClass.UNKNOWN, Finding.UNRESOLVED),
        FailureEvidence(AUTH, ErrorClass.AUTH, Finding.APPLICATION_REAUTH_SUSPECTED),
        FailureEvidence(
            AUTH, ErrorClass.TRANSIENT, Finding.RECOVERABLE, remote_outcome=RemoteOutcome.UNKNOWN
        ),
        # A second proven reason does not silently replace the first either.
        FailureEvidence(AUTH, ErrorClass.POLICY_BLOCKED, Finding.ACCOUNT_RESTRICTION_PROVEN),
    ]
    for failure in later:
        assert observe_failure(paused, failure).overlays == paused.overlays


# ---------------------------------------------------------------- supporting convergence


def test_a_new_process_never_trusts_a_persisted_ready() -> None:
    restarted = on_process_start(observe_permission(_ready(), ATTESTED))
    assert (restarted.auth, restarted.write) == (AuthStatus.NOT_READY, WriteStatus.UNVERIFIED)
    assert restarted.write_scope == ATTESTED  # evidence stays; its own validity is PR-C's
    assert restarted.contract_freshness is CURRENT  # a restart never re-enters UNRECORDED
    # A pending human action and a mismatch survive the restart.
    mismatch = observe_auth(_ready(), _evidence(OTHER))
    assert on_process_start(mismatch) == mismatch


def test_exhausted_auth_recovery_pauses_without_reclassifying_the_error() -> None:
    exhausted = observe_failure(
        _ready(), FailureEvidence(AUTH, ErrorClass.AUTH, Finding.AUTH_RECOVERY_EXHAUSTED)
    )
    assert exhausted.overlays == (WorkflowOverlay(PAUSED, AUTH, PauseReason.AUTH_RETRY_LIMIT),)
    assert (exhausted.auth, exhausted.error_class) == (AuthStatus.NOT_READY, ErrorClass.AUTH)
    resumed = resolve(exhausted, AUTH, Resolution.RESUME)
    assert (resumed.auth, resumed.overlays) == (AuthStatus.NOT_READY, ())


def test_an_unknown_remote_outcome_is_reviewed_never_replayed() -> None:
    after = observe_failure(
        _ready(),
        FailureEvidence(
            REGISTRATION,
            ErrorClass.TRANSIENT,
            Finding.RECOVERABLE,
            remote_outcome=RemoteOutcome.UNKNOWN,
        ),
    )
    assert after.overlays == (WorkflowOverlay(REVIEW, REGISTRATION),)
    assert (after.remote_outcome, after.auth) == (RemoteOutcome.UNKNOWN, AuthStatus.READY)


def test_a_scope_insufficient_pause_is_lifted_only_by_permission_evidence() -> None:
    blocked = observe_permission(_ready(), MISSING)
    for resolution in Resolution:
        with pytest.raises(CapabilityInvariantError):
            resolve(blocked, REGISTRATION, resolution)


def test_the_review_an_auth_mismatch_requires_is_never_narrowed_away() -> None:
    mismatch = observe_auth(_ready(), _evidence(OTHER))
    after = observe_failure(
        mismatch, FailureEvidence(AUTH, ErrorClass.AUTH, Finding.AUTH_RECOVERY_EXHAUSTED)
    )
    assert after.auth is AuthStatus.AUTH_MISMATCH
    assert after.overlays == (WorkflowOverlay(REVIEW, AUTH),)


def test_a_failure_without_a_mutation_keeps_an_unknown_remote_outcome() -> None:
    unknown = observe_failure(
        _ready(),
        FailureEvidence(
            REGISTRATION,
            ErrorClass.TRANSIENT,
            Finding.RECOVERABLE,
            remote_outcome=RemoteOutcome.UNKNOWN,
        ),
    )
    later = observe_failure(
        unknown, FailureEvidence(AUTH, ErrorClass.TRANSIENT, Finding.RECOVERABLE)
    )
    assert later.remote_outcome is RemoteOutcome.UNKNOWN  # still no blind replay (W3)
    reconciled = observe_failure(
        unknown,
        FailureEvidence(
            REGISTRATION,
            ErrorClass.VALIDATION,
            Finding.RECOVERABLE,
            remote_outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
        ),
    )
    assert reconciled.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
