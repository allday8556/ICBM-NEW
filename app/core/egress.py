"""Process-wide outbound network guard.

Instead of trusting every library to use an approved client, the guard observes the interpreter's
own socket audit events (PEP 578) and blocks any non-loopback destination before a packet leaves
the machine. Every blocked attempt is counted so readiness and acceptance can prove the count
stayed at zero.

From M1 a supplier adapter may open an explicit, scoped *grant* for the hosts its profile declares
(Issue #7 §8, ADR-0007). Inside the grant — and only in the calling context — those hosts are
reachable; every other destination stays blocked and counted. The global guard is never disabled.
"""

import logging
import socket
import sys
import threading
from collections import Counter, deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.net import is_loopback_host

logger = logging.getLogger("icbm.egress")

POLICY = "BLOCK_EXTERNAL_EXCEPT_GRANTED"
_WATCHED = frozenset(
    {"socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname"}
)
_NAME_LOOKUPS = frozenset({"socket.getaddrinfo", "socket.gethostbyname"})


class EgressBlockedError(ConnectionRefusedError):
    """Raised inside the calling library when it targets a non-loopback destination."""


@dataclass(frozen=True)
class EgressAttempt:
    at: datetime
    event: str
    destination: str


@dataclass
class _Grant:
    owner: str
    hosts: frozenset[str]
    # Addresses the granted hosts resolve to; connect events carry addresses, not names.
    addresses: set[str] = field(default_factory=set)


_active_grant: ContextVar[_Grant | None] = ContextVar("icbm_egress_grant", default=None)


def _normalise(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _host_from_address(address: Any) -> str | None:
    if isinstance(address, tuple) and address:
        address = address[0]
    if isinstance(address, bytes):
        address = address.decode("ascii", "replace")
    # A str that is not a tuple element is an AF_UNIX path or similar: local by definition.
    return address if isinstance(address, str) and address else None


def _destination(event: str, args: tuple[Any, ...]) -> str | None:
    if event in ("socket.connect", "socket.sendto"):
        address = args[1] if len(args) > 1 else None
        if isinstance(address, str):
            return None
        return _host_from_address(address)
    host = args[0] if args else None
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return host if isinstance(host, str) and host else None


def _resolve(hosts: Iterable[str]) -> set[str]:
    addresses: set[str] = set()
    for host in hosts:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            continue
        addresses.update(str(info[4][0]).split("%", 1)[0] for info in infos)
    return addresses


class EgressGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._installed = False
        self._attempts = 0
        self._recent: deque[EgressAttempt] = deque(maxlen=20)
        self._granted: Counter[str] = Counter()

    @property
    def installed(self) -> bool:
        return self._installed

    @property
    def external_attempts(self) -> int:
        """Blocked (unauthorised) external attempts. Granted supplier egress is not counted."""
        return self._attempts

    def install(self) -> None:
        """Install the audit hook once per process. Audit hooks cannot be removed."""
        with self._lock:
            if self._installed:
                return
            sys.addaudithook(self._hook)
            self._installed = True

    @contextmanager
    def grant(self, owner: str, hosts: Iterable[str]) -> Iterator[None]:
        """Allow ``hosts`` (and nothing else) from the calling context for the block's duration.

        Only supplier transports open grants (a repository rule enforces it). A nested grant
        replaces the outer one rather than widening it.
        """
        normalised = frozenset(_normalise(h) for h in hosts)
        if not owner or not normalised or any(not h or is_loopback_host(h) for h in normalised):
            raise ValueError("an egress grant needs an owner and at least one external host")
        token = _active_grant.set(_Grant(owner=owner, hosts=normalised))
        try:
            yield
        finally:
            _active_grant.reset(token)

    def _permits(self, grant: _Grant, event: str, destination: str) -> bool:
        name = _normalise(destination).split("%", 1)[0]
        if event in _NAME_LOOKUPS or name in grant.hosts:
            return name in grant.hosts
        if name in grant.addresses:
            return True
        # DNS answers rotate: refresh the granted hosts' addresses once before refusing.
        grant.addresses.update(_resolve(grant.hosts))
        return name in grant.addresses

    def _hook(self, event: str, args: tuple[Any, ...]) -> None:
        if event not in _WATCHED:
            return
        destination = _destination(event, args)
        if destination is None or is_loopback_host(destination):
            return
        grant = _active_grant.get()
        if grant is not None and self._permits(grant, event, destination):
            with self._lock:
                self._granted[grant.owner] += 1
            return
        attempt = EgressAttempt(datetime.now(UTC), event, destination)
        with self._lock:
            self._attempts += 1
            self._recent.append(attempt)
        logger.warning(
            "egress.blocked",
            extra={
                "event": event,
                "destination": destination,
                "policy": POLICY,
                "grant_owner": grant.owner if grant else None,
            },
        )
        raise EgressBlockedError(f"egress policy blocks external destination {destination!r}")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            recent = [
                {"at": a.at.isoformat(), "event": a.event, "destination": a.destination}
                for a in self._recent
            ]
            return {
                "policy": POLICY,
                "installed": self._installed,
                "external_attempts": self._attempts,
                "granted_events": dict(self._granted),
                "recent": recent,
            }


EGRESS = EgressGuard()
