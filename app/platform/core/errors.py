"""Canonical error taxonomy: ARCHITECTURE.md §8, aligned by ADR-0008 (ARCHITECT_REVIEW B8).

The core error class is a cause; platform-specific codes remain adapter details carried in
``code``. Only TRANSIENT and RATE_LIMITED are ever retried automatically (ADR-0004).

The taxonomy only grows. Persisted rows and the append-only audit log hold these values, so no
member is ever removed or renamed (ADR-0008 D). ``RATE_LIMITED`` and ``POLICY_BLOCKED`` are the
persisted spellings of Canonical v3.1's ``RATE_LIMIT`` and ``POLICY`` (ADR-0008 A).
"""

from enum import StrEnum
from typing import Any


class ErrorClass(StrEnum):
    TRANSIENT = "TRANSIENT"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH = "AUTH"
    VALIDATION = "VALIDATION"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    # An ICBM-local addressed resource only; never a provider/wire classification (ADR-0008 C).
    NOT_FOUND = "NOT_FOUND"
    # Added by ADR-0008 (B). None is retried automatically.
    CONFLICT = "CONFLICT"
    DUPLICATE = "DUPLICATE"
    # A cause; it is not workflow_state=REVIEW_REQUIRED.
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FATAL = "FATAL"
    UNKNOWN = "UNKNOWN"


AUTO_RETRYABLE: frozenset[ErrorClass] = frozenset({ErrorClass.TRANSIENT, ErrorClass.RATE_LIMITED})

HTTP_STATUS: dict[ErrorClass, int] = {
    ErrorClass.TRANSIENT: 503,
    ErrorClass.RATE_LIMITED: 429,
    ErrorClass.AUTH: 401,
    ErrorClass.VALIDATION: 422,
    ErrorClass.POLICY_BLOCKED: 403,
    ErrorClass.NOT_FOUND: 404,
    ErrorClass.CONFLICT: 409,
    ErrorClass.DUPLICATE: 409,
    ErrorClass.REVIEW_REQUIRED: 409,
    ErrorClass.FATAL: 500,
    ErrorClass.UNKNOWN: 500,
}


class AppError(Exception):
    """An error whose class and code are part of the application contract."""

    error_class: ErrorClass = ErrorClass.UNKNOWN

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        return self.error_class in AUTO_RETRYABLE

    def envelope(self, correlation_id: str | None) -> dict[str, Any]:
        return error_envelope(
            self.error_class, self.code, self.message, correlation_id, details=self.details
        )


class TransientError(AppError):
    error_class = ErrorClass.TRANSIENT


class RateLimitedError(AppError):
    error_class = ErrorClass.RATE_LIMITED


class AuthError(AppError):
    error_class = ErrorClass.AUTH


class InputValidationError(AppError):
    error_class = ErrorClass.VALIDATION


class PolicyBlockedError(AppError):
    error_class = ErrorClass.POLICY_BLOCKED


class NotFoundError(AppError):
    error_class = ErrorClass.NOT_FOUND


class UnknownOutcomeError(AppError):
    error_class = ErrorClass.UNKNOWN


def classify(exc: BaseException) -> tuple[ErrorClass, str, str]:
    """Map any exception onto (class, code, message). Non-contract exceptions are UNKNOWN."""
    if isinstance(exc, AppError):
        return exc.error_class, exc.code, exc.message
    return ErrorClass.UNKNOWN, "UNHANDLED_EXCEPTION", f"{type(exc).__name__}: {exc}"


def error_envelope(
    error_class: ErrorClass,
    code: str,
    message: str,
    correlation_id: str | None,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "class": error_class.value,
        "code": code,
        "message": message,
        "retryable": error_class in AUTO_RETRYABLE,
        "correlation_id": correlation_id,
    }
    if details:
        body["details"] = details
    return {"error": body}
