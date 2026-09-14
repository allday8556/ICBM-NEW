"""The first-binding transition (ACCOUNT_IDENTITY §5, CAPABILITY_MAPPING F5; review 5200019078
blocker 2): binding an account is new trust, gated on a CURRENT contract in its own right."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.connect.marketplace.capability import (
    INITIAL,
    AuthEvidence,
    AuthStatus,
    CapabilityInvariantError,
    ContractFreshness,
    ExpansionBlockedError,
    Generations,
    IdentityProof,
    WorkflowOverlay,
    WorkflowScope,
    WorkflowState,
    observe_auth,
    observe_first_binding,
)

NOW = datetime(2026, 9, 15, tzinfo=UTC)
GENERATIONS = Generations(1, 1)
EVIDENCE = AuthEvidence(
    binding_committed=True,
    expected_account_uid="uid-fixture-A",
    current=GENERATIONS,
    proof=IdentityProof(GENERATIONS, "uid-fixture-A", NOW),
)
REVIEW = WorkflowOverlay(WorkflowState.REVIEW_REQUIRED, WorkflowScope.AUTHENTICATION)


def _recorded(freshness: ContractFreshness) -> object:
    return replace(INITIAL, contract_freshness=freshness, freshness_recorded_at=NOW)


@pytest.mark.parametrize(
    "state",
    [
        INITIAL,
        _recorded(ContractFreshness.STALE),
        _recorded(ContractFreshness.REVIEW_REQUIRED),
    ],
    ids=["UNRECORDED", "STALE", "REVIEW_REQUIRED"],
)
def test_a_first_binding_waits_for_a_current_contract(state: object) -> None:
    with pytest.raises(ExpansionBlockedError):
        observe_first_binding(state, EVIDENCE)  # type: ignore[arg-type]


def test_the_gate_holds_even_when_an_overlay_keeps_auth_from_ready() -> None:
    state = replace(INITIAL, overlays=(REVIEW,))
    # observe_auth alone never reaches its READY gate here: the overlay keeps auth NOT_READY.
    assert observe_auth(state, EVIDENCE).auth is AuthStatus.NOT_READY
    with pytest.raises(ExpansionBlockedError):
        observe_first_binding(state, EVIDENCE)


def test_a_current_contract_admits_the_first_binding() -> None:
    state = _recorded(ContractFreshness.CURRENT)
    assert observe_first_binding(state, EVIDENCE).auth is AuthStatus.READY  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "evidence",
    [replace(EVIDENCE, binding_committed=False), replace(EVIDENCE, expected_account_uid=None)],
)
def test_a_first_binding_carries_the_identity_it_binds(evidence: AuthEvidence) -> None:
    with pytest.raises(CapabilityInvariantError):
        observe_first_binding(_recorded(ContractFreshness.CURRENT), evidence)  # type: ignore[arg-type]
