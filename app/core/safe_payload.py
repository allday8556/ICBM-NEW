"""Allowlisted payloads for supplier audit events and logs (Issue #7 comment 5653608622 §6).

Safety here is an allowlist, not a blocklist of secret-looking names: a field is emitted only if
it is declared below as non-secret diagnostics, and its value must be a scalar (or a short list of
marker names). Anything else raises, so a new field cannot leak by default. Runtime secret
scanning of the real artifacts remains a second, independent control.
"""

import re
from datetime import datetime
from enum import Enum

SAFE_FIELDS: frozenset[str] = frozenset(
    {
        # Issue #7 comment 5653608622 §6
        "supplier_key",
        "connection_id",
        "request_kind",
        "transport",
        "started_at",
        "finished_at",
        "latency_ms",
        "retry_count",
        "result_class",
        "state_from",
        "state_to",
        "correlation_id",
        # further non-secret diagnostics used by CONNECT
        "http_status",
        "signals",
        "target",
        "trigger",
        "error_class",
        "error_code",
        "prior_state",
        "resumed_at",
        "auto_connect",
        "credentials_stored",
        "consecutive_auth_failures",
        "auth_retry_limit",
        "real_login_attempts",
        "session_reuse_count",
        "reauth_count",
        "control_result",
        "authenticated_result",
        "browser_blocked_requests",
        "minimum_request_interval_s",
        "max_concurrency",
        # marketplace capability (M2 PR-B): enum values and overlay markers only
        "marketplace_key",
        "event",
        "auth",
        "write_scope_status",
        "evidence_strength",
        "write_status",
        "contract_freshness",
        "freshness_recorded_at",
        "workflow",
        "remote_outcome",
        "resolution",
    }
)

Scalar = str | int | float | bool | None
SafeValue = Scalar | list[str]

_MAX_TEXT = 200
_MARKER = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")


class UnsafePayloadError(ValueError):
    """A field or value is not an approved non-secret diagnostic."""


def _value(key: str, value: object) -> SafeValue:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TEXT or not value.isprintable():
            raise UnsafePayloadError(f"{key!r} must be printable text of at most {_MAX_TEXT} chars")
        return value
    if isinstance(value, tuple | list) and all(
        isinstance(item, str) and _MARKER.fullmatch(item) for item in value
    ):
        return [str(item) for item in value]
    raise UnsafePayloadError(f"{key!r} has a non-scalar value of type {type(value).__name__}")


def safe_payload(**fields: object) -> dict[str, SafeValue]:
    """Build an audit/log payload from approved fields only; unknown fields are rejected."""
    unknown = sorted(set(fields) - SAFE_FIELDS)
    if unknown:
        raise UnsafePayloadError(f"not approved as safe diagnostics: {', '.join(unknown)}")
    return {key: _value(key, value) for key, value in fields.items()}
