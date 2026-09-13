import pytest

from app.connect.state import (
    TRANSITIONS,
    CapabilityStatus,
    ConnectionState,
    InvalidTransitionError,
    capability_status,
    check_transition,
    startup_state,
)

S = ConnectionState


def test_every_state_is_covered_by_the_transition_table() -> None:
    assert set(TRANSITIONS) == set(ConnectionState)
    for targets in TRANSITIONS.values():
        assert targets <= set(ConnectionState)


def test_ready_is_reachable_only_from_a_verification_step() -> None:
    assert {source for source, targets in TRANSITIONS.items() if S.READY in targets} == {
        S.SESSION_CHECK,
        S.VERIFYING,
    }


def test_paused_is_left_only_towards_disconnected() -> None:
    assert TRANSITIONS[S.PAUSED] == {S.DISCONNECTED}
    assert {source for source, targets in TRANSITIONS.items() if S.PAUSED in targets} == {
        S.AUTHENTICATING,
        S.REAUTHENTICATING,
        S.VERIFYING,
    }


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (S.DISCONNECTED, S.READY),
        (S.AUTHENTICATING, S.READY),
        (S.PAUSED, S.AUTHENTICATING),
        (S.PAUSED, S.READY),
        (S.AUTH_EXPIRED, S.READY),
    ],
)
def test_impossible_moves_are_refused(source: ConnectionState, target: ConnectionState) -> None:
    with pytest.raises(InvalidTransitionError):
        check_transition(source, target)


@pytest.mark.parametrize("state", list(ConnectionState))
def test_a_new_process_keeps_only_paused(state: ConnectionState) -> None:
    assert startup_state(state) is (S.PAUSED if state is S.PAUSED else S.DISCONNECTED)


@pytest.mark.parametrize(
    ("state", "stored", "status"),
    [
        (S.READY, True, CapabilityStatus.READY),
        (S.PAUSED, True, CapabilityStatus.PAUSED),
        (S.AUTH_EXPIRED, True, CapabilityStatus.AUTH_EXPIRED),
        (S.REAUTHENTICATING, True, CapabilityStatus.AUTH_EXPIRED),
        (S.DEGRADED, True, CapabilityStatus.DEGRADED),
        (S.SESSION_CHECK, True, CapabilityStatus.CONNECTING),
        (S.VERIFYING, True, CapabilityStatus.CONNECTING),
        (S.DISCONNECTED, True, CapabilityStatus.DISCONNECTED),
        (S.DISCONNECTED, False, CapabilityStatus.NOT_CONFIGURED),
    ],
)
def test_capability_status_derives_from_the_state(
    state: ConnectionState, stored: bool, status: CapabilityStatus
) -> None:
    assert capability_status(state, credentials_stored=stored) is status
