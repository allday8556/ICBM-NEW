"""The supplier connection state machine (Issue #7 comments 5653591871 and 5653608622 §2).

Authentication is one explicit state with validated transitions, not a set of loose strings or
booleans. READY is reachable only from a verification step, and only with a protected-read proof
(enforced by the service, and by a database CHECK that READY carries ``last_verified_at``).
Capability readiness is derived from this state.
"""

from enum import StrEnum


class ConnectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    SESSION_CHECK = "SESSION_CHECK"
    AUTHENTICATING = "AUTHENTICATING"
    VERIFYING = "VERIFYING"
    READY = "READY"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    REAUTHENTICATING = "REAUTHENTICATING"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"


S = ConnectionState

# The only permitted moves. Anything else is a programming error.
TRANSITIONS: dict[ConnectionState, frozenset[ConnectionState]] = {
    S.DISCONNECTED: frozenset({S.SESSION_CHECK, S.AUTHENTICATING}),
    S.SESSION_CHECK: frozenset({S.READY, S.AUTH_EXPIRED, S.DEGRADED, S.DISCONNECTED}),
    S.AUTHENTICATING: frozenset({S.VERIFYING, S.DISCONNECTED, S.DEGRADED, S.PAUSED}),
    S.AUTH_EXPIRED: frozenset({S.REAUTHENTICATING, S.SESSION_CHECK, S.DISCONNECTED}),
    S.REAUTHENTICATING: frozenset({S.VERIFYING, S.AUTH_EXPIRED, S.DEGRADED, S.PAUSED}),
    S.VERIFYING: frozenset({S.READY, S.DISCONNECTED, S.AUTH_EXPIRED, S.DEGRADED, S.PAUSED}),
    S.READY: frozenset({S.SESSION_CHECK, S.AUTH_EXPIRED, S.DISCONNECTED}),
    S.DEGRADED: frozenset({S.SESSION_CHECK, S.AUTHENTICATING, S.DISCONNECTED}),
    # Only an explicit, audited operator resume leaves PAUSED.
    S.PAUSED: frozenset({S.DISCONNECTED}),
}

IN_FLIGHT = frozenset({S.SESSION_CHECK, S.AUTHENTICATING, S.REAUTHENTICATING, S.VERIFYING})


class InvalidTransitionError(RuntimeError):
    """A connection state change outside ``TRANSITIONS``."""


def check_transition(source: ConnectionState, target: ConnectionState) -> None:
    if target not in TRANSITIONS[source]:
        raise InvalidTransitionError(f"supplier connection cannot move {source} -> {target}")


def startup_state(persisted: ConnectionState) -> ConnectionState:
    """State a connection takes when a new process starts, before any supplier request.

    A READY proof belongs to the process that made it, and an in-flight step was cut off, so
    everything but PAUSED restarts as DISCONNECTED. PAUSED survives restarts until the operator
    resumes it.
    """
    return S.PAUSED if persisted is S.PAUSED else S.DISCONNECTED


class CapabilityStatus(StrEnum):
    READY = "READY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"


def capability_status(state: ConnectionState, *, credentials_stored: bool) -> CapabilityStatus:
    if state is S.READY:
        return CapabilityStatus.READY
    if state is S.PAUSED:
        return CapabilityStatus.PAUSED
    if state in (S.AUTH_EXPIRED, S.REAUTHENTICATING):
        return CapabilityStatus.AUTH_EXPIRED
    if state is S.DEGRADED:
        return CapabilityStatus.DEGRADED
    if state in IN_FLIGHT:
        return CapabilityStatus.CONNECTING
    if not credentials_stored:
        return CapabilityStatus.NOT_CONFIGURED
    return CapabilityStatus.DISCONNECTED
