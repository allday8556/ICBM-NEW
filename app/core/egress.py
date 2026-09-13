"""Process-wide outbound network guard.

M0 performs zero supplier/marketplace calls (Issue #1). Instead of trusting every library to use
an approved client, the guard observes the interpreter's own socket audit events (PEP 578) and
blocks any non-loopback destination before a packet leaves the machine. Every attempt is counted
so readiness and acceptance can prove the count stayed at zero.
"""

import logging
import sys
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.net import is_loopback_host

logger = logging.getLogger("icbm.egress")

POLICY = "BLOCK_EXTERNAL"
_WATCHED = frozenset(
    {"socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname"}
)


class EgressBlockedError(ConnectionRefusedError):
    """Raised inside the calling library when it targets a non-loopback destination."""


@dataclass(frozen=True)
class EgressAttempt:
    at: datetime
    event: str
    destination: str


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


class EgressGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._installed = False
        self._attempts = 0
        self._recent: deque[EgressAttempt] = deque(maxlen=20)

    @property
    def installed(self) -> bool:
        return self._installed

    @property
    def external_attempts(self) -> int:
        return self._attempts

    def install(self) -> None:
        """Install the audit hook once per process. Audit hooks cannot be removed."""
        with self._lock:
            if self._installed:
                return
            sys.addaudithook(self._hook)
            self._installed = True

    def _hook(self, event: str, args: tuple[Any, ...]) -> None:
        if event not in _WATCHED:
            return
        destination = _destination(event, args)
        if destination is None or is_loopback_host(destination):
            return
        attempt = EgressAttempt(datetime.now(UTC), event, destination)
        with self._lock:
            self._attempts += 1
            self._recent.append(attempt)
        logger.warning(
            "egress.blocked", extra={"event": event, "destination": destination, "policy": POLICY}
        )
        raise EgressBlockedError(f"M0 egress policy blocks external destination {destination!r}")

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
                "recent": recent,
            }


EGRESS = EgressGuard()
