"""The Phase C send guard seam (Issue #110 C1 PREP-0 `5841947773`).

A context variable that an ordinary collection sets only while a Phase-C-accounted run executes:
a run whose frozen capture request binds it to a campaign (``app.collect.shadow.SendAccounting``).
Every supplier send point that Phase C accounts calls ``reserve_send`` immediately before it
transmits:

- the COLLECT request budget, which the collection transport reserves before each product read
  and image request;
- the CONNECT control and protected reads, and the login, of the connection a collection uses.

The guard durably reserves the send, or refuses it before a byte leaves. With no guard set —
every ordinary collection, every connection test, every other caller — ``reserve_send`` does
nothing at all, so those paths are unchanged.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final, Protocol

PRODUCT_READ: Final = "PRODUCT_READ"
IMAGE_REQUEST: Final = "IMAGE_REQUEST"
POLICY_READ: Final = "POLICY_READ"
CONNECT_CONTROL_READ: Final = "CONNECT_CONTROL_READ"
CONNECT_PROTECTED_READ: Final = "CONNECT_PROTECTED_READ"
CONNECT_AUTHENTICATE: Final = "CONNECT_AUTHENTICATE"
REQUEST_CLASSES: Final = (
    PRODUCT_READ,
    IMAGE_REQUEST,
    POLICY_READ,
    CONNECT_CONTROL_READ,
    CONNECT_PROTECTED_READ,
    CONNECT_AUTHENTICATE,
)


class SendGuard(Protocol):
    def reserve(self, request_class: str, subject: str) -> None:
        """Durably reserve one send of ``request_class``, or raise an ``AppError`` so that
        nothing is sent. ``subject`` names the target; the guard keeps only its digest."""


_GUARD: ContextVar[SendGuard | None] = ContextVar("phase_c_send_guard", default=None)


@contextmanager
def guarding(guard: SendGuard | None) -> Iterator[None]:
    """Account every guarded send made inside the block to ``guard`` (none: no accounting)."""
    token = _GUARD.set(guard)
    try:
        yield
    finally:
        _GUARD.reset(token)


def reserve_send(request_class: str, subject: str) -> None:
    """Called at a send point, immediately before transmission."""
    guard = _GUARD.get()
    if guard is not None:
        guard.reserve(request_class, subject)
