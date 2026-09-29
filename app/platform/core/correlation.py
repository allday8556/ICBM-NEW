"""Correlation ID propagation (ARCHITECT_REVIEW C6).

A correlation ID is issued at flow entry (HTTP request) and carried through log records,
Job rows and AuditEvent rows. Worker attempts re-enter the Job's correlation scope.
"""

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

CORRELATION_HEADER = "X-Correlation-ID"

_VALID_ID = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")
_current: ContextVar[str | None] = ContextVar("icbm_correlation_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def is_valid_correlation_id(value: str) -> bool:
    return bool(_VALID_ID.fullmatch(value))


def accept_or_issue(incoming: str | None) -> str:
    """Accept a well-formed caller-supplied ID, otherwise issue a new one."""
    if incoming and is_valid_correlation_id(incoming):
        return incoming
    return new_correlation_id()


def get_correlation_id() -> str | None:
    return _current.get()


def set_correlation_id(value: str) -> Token[str | None]:
    return _current.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    _current.reset(token)


@contextmanager
def correlation_scope(value: str) -> Iterator[str]:
    token = set_correlation_id(value)
    try:
        yield value
    finally:
        reset_correlation_id(token)
