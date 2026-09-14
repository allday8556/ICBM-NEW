"""Which phase a SmartStore request failed in, and what that proves (ERRORS.md §15; M2 PR-A).

``NOT_APPLIED_PROVEN`` is a whitelist-only safety claim. It is made only when this request's own
evidence shows it could not have reached the provider: a local preflight rejection, ICBM's egress
policy refusing the connection, or a *new* connection that failed in DNS, TCP connect or the TLS
handshake before any request byte was written. Everything else — a read timeout, a failure after
the request write started, a drop while reading, a connection this request did not open (pooled
or reused), an HTTP/2 stream reset, an unobserved phase — is ``UNKNOWN``.

The evidence is the httpcore trace of this one request plus the exception's cause chain. An
exception's class name alone never proves a phase (§15.2, §23.13).
"""

import socket
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum

from app.connect.marketplace.capability import RemoteOutcome
from app.core.egress import EgressBlockedError


class Phase(StrEnum):
    # Transmission precluded: whitelisted (§15.1).
    LOCAL_PREFLIGHT = "LOCAL_PREFLIGHT"
    EGRESS_BLOCKED = "EGRESS_BLOCKED"
    DNS_FAILURE = "DNS_FAILURE"
    TCP_CONNECT_FAILURE = "TCP_CONNECT_FAILURE"
    TLS_HANDSHAKE_FAILURE = "TLS_HANDSHAKE_FAILURE"
    # Transmission possible: never whitelisted (§15.2).
    REQUEST_SENT = "REQUEST_SENT"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
    NO_NEW_CONNECTION = "NO_NEW_CONNECTION"
    UNOBSERVED = "UNOBSERVED"


TRANSMISSION_PRECLUDED: frozenset[Phase] = frozenset(
    {
        Phase.LOCAL_PREFLIGHT,
        Phase.EGRESS_BLOCKED,
        Phase.DNS_FAILURE,
        Phase.TCP_CONNECT_FAILURE,
        Phase.TLS_HANDSHAKE_FAILURE,
    }
)

# httpcore emits "<connection|http11|http2>.<step>.<started|complete|failed>" for each request.
_REQUEST_WRITE_STARTED = frozenset(
    {"http11.send_request_headers.started", "http2.send_request_headers.started"}
)
_CONNECT_STARTED = "connection.connect_tcp.started"
_CONNECT_COMPLETE = "connection.connect_tcp.complete"
_CONNECT_FAILED = "connection.connect_tcp.failed"
_TLS_COMPLETE = "connection.start_tls.complete"
_TLS_FAILED = "connection.start_tls.failed"


class TraceRecorder:
    """The httpcore ``trace`` extension for one request. It keeps event names only: the info
    payload can hold the request itself, headers included, and is never retained."""

    def __init__(self) -> None:
        self._events: list[str] = []

    def __call__(self, name: str, info: Mapping[str, object]) -> None:
        self._events.append(name)

    @property
    def events(self) -> tuple[str, ...]:
        return tuple(self._events)


def _causes(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def transmission_phase(events: Sequence[str], error: BaseException) -> Phase:
    """The phase a request that produced no response failed in."""
    seen = set(events)
    if seen & _REQUEST_WRITE_STARTED:
        return Phase.REQUEST_SENT
    causes = list(_causes(error))
    if any(isinstance(cause, EgressBlockedError) for cause in causes):
        # The guard refuses the name lookup or the connect of a connection being opened.
        return Phase.EGRESS_BLOCKED
    if _CONNECT_STARTED not in seen:
        # This request opened no connection: a pooled/reused one, or a phase nobody observed.
        return Phase.NO_NEW_CONNECTION if seen else Phase.UNOBSERVED
    if _CONNECT_COMPLETE not in seen:
        if _CONNECT_FAILED in seen:
            if any(isinstance(cause, socket.gaierror) for cause in causes):
                return Phase.DNS_FAILURE
            return Phase.TCP_CONNECT_FAILURE
        return Phase.UNOBSERVED
    if _TLS_FAILED in seen and _TLS_COMPLETE not in seen:
        return Phase.TLS_HANDSHAKE_FAILURE
    return Phase.UNOBSERVED


def remote_outcome(phase: Phase) -> RemoteOutcome:
    return (
        RemoteOutcome.NOT_APPLIED_PROVEN
        if phase in TRANSMISSION_PRECLUDED
        else RemoteOutcome.UNKNOWN
    )
