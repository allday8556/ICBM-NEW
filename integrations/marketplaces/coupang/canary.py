"""One-shot, read-only Coupang KR credential canary (C-LIVE-R1).

This module deliberately owns one endpoint and no generic caller. It can issue exactly one
``GET`` for the vendor's category-auto-matching agreement flag. It accepts no URL, method,
query, body, retry policy or mutation payload from a caller.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Final

import httpx

from app.platform.core.egress import EGRESS, EgressBlockedError
from app.platform.core.errors import AppError, ErrorClass
from integrations.marketplaces.coupang.auth import (
    COUPANG_BASE_URL,
    COUPANG_PROVIDER_HOST,
    AuthContext,
    Authenticator,
    Market,
    Method,
    RequestTarget,
    UtcClock,
)
from integrations.marketplaces.coupang.errors import classify_auth_response

ENDPOINT_ID: Final = "COUPANG_AUTO_CATEGORY_AGREEMENT_CANARY"
EGRESS_OWNER: Final = "marketplace:coupang:read-canary"
MAX_RESPONSE_BYTES: Final = 65_536
MAX_CLOCK_SKEW_SECONDS: Final = 5.0
CLOCK_PREFLIGHT_TTL: Final = timedelta(minutes=10)
APPROVAL_MAX_TTL: Final = timedelta(hours=24)
CI_MARKERS: Final = ("CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST")
_PATH_PREFIX: Final = "/v2/providers/seller_api/apis/api/v1/marketplace/vendors/"
_PATH_SUFFIX: Final = "/check-auto-category-agreed"
_VENDOR_ID: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


class LiveTransportRefused(RuntimeError):
    """A real Coupang transport was requested in CI or a test process."""


class CoupangCanaryError(AppError):
    """A sanitized canary failure containing no request or response secrets."""

    def __init__(
        self, error_class: ErrorClass, code: str, *, http_status: int | None = None
    ) -> None:
        self.error_class = error_class
        details = {} if http_status is None else {"http_status": http_status}
        super().__init__(code, "Coupang read-only canary failed", details=details)
        self.http_status = http_status


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_generation(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("credential_generation must be a positive integer")
    return value


def _ci_or_test(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    for name in CI_MARKERS:
        if env.get(name):
            return name
    return "pytest" if "pytest" in sys.modules else None


@dataclass(frozen=True)
class ClockPreflight:
    """Operator-observed system-clock skew, bounded and short-lived."""

    observed_skew_seconds: float
    checked_at: datetime

    @classmethod
    def create(cls, observed_skew_seconds: float, *, checked_at: datetime) -> ClockPreflight:
        if not isinstance(observed_skew_seconds, (int, float)) or isinstance(
            observed_skew_seconds, bool
        ):
            raise ValueError("observed clock skew must be numeric")
        return cls(float(observed_skew_seconds), _aware_utc(checked_at, "checked_at"))


@dataclass(frozen=True)
class ReadOnlyCanaryApproval:
    """One bounded approval, bound to the credential identity and declared source IP."""

    scope_id: str
    vendor_id: str
    credential_generation: int
    source_ip: IPv4Address | IPv6Address
    approved_at: datetime
    expires_at: datetime
    market: Market = Market.KR

    @classmethod
    def create(
        cls,
        *,
        scope_id: str,
        vendor_id: str,
        credential_generation: int,
        source_ip: str,
        approved_at: datetime,
        expires_at: datetime,
        market: Market = Market.KR,
    ) -> ReadOnlyCanaryApproval:
        if not isinstance(scope_id, str) or not scope_id.strip() or not scope_id.isprintable():
            raise ValueError("scope_id must be a printable non-empty string")
        if not isinstance(vendor_id, str) or not vendor_id.strip():
            raise ValueError("vendor_id must be non-empty")
        if market is not Market.KR:
            raise ValueError("the read-only canary is KR-only")
        start = _aware_utc(approved_at, "approved_at")
        end = _aware_utc(expires_at, "expires_at")
        if end <= start or end - start > APPROVAL_MAX_TTL:
            raise ValueError("approval window must be positive and at most 24 hours")
        return cls(
            scope_id.strip(),
            vendor_id.strip(),
            _positive_generation(credential_generation),
            ip_address(source_ip),
            start,
            end,
            market,
        )


@dataclass(frozen=True)
class CoupangCanaryResult:
    endpoint_id: str
    http_status: int
    auto_category_agreed: bool
    credential_generation: int


def _read_bounded(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise CoupangCanaryError(ErrorClass.UNKNOWN, "COUPANG_RESPONSE_TOO_LARGE")
        chunks.append(chunk)
    return b"".join(chunks)


def _document(body: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _provider_message(document: dict[str, object] | None) -> str | None:
    if document is None:
        return None
    for key in ("message", "errorMessage"):
        value = document.get(key)
        if isinstance(value, str):
            return value
    return None


class CoupangReadCanary:
    """The only credentialed Coupang caller: one exact KR GET, at most once per instance."""

    def __init__(
        self,
        *,
        context: AuthContext,
        approval: ReadOnlyCanaryApproval,
        clock: UtcClock,
        clock_preflight: ClockPreflight,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if transport is None and (blocker := _ci_or_test(environ)) is not None:
            raise LiveTransportRefused(f"the live Coupang transport never exists under {blocker}")
        self._context = context
        self._approval = approval
        self._clock = clock
        self._clock_preflight = clock_preflight
        self._authenticator = Authenticator(clock)
        self._transport = transport
        self._used = False

    def _preflight(self, now: datetime) -> None:
        credentials = self._context.credentials
        declaration = self._context.ip_allowlist
        approval = self._approval
        if self._context.market is not Market.KR or approval.market is not Market.KR:
            raise CoupangCanaryError(ErrorClass.POLICY_BLOCKED, "COUPANG_CANARY_KR_ONLY")
        if _VENDOR_ID.fullmatch(credentials.vendor_id) is None:
            raise CoupangCanaryError(ErrorClass.POLICY_BLOCKED, "COUPANG_VENDOR_ID_UNSAFE")
        if declaration is None:
            raise CoupangCanaryError(ErrorClass.POLICY_BLOCKED, "COUPANG_IP_EVIDENCE_MISSING")
        if not (
            approval.vendor_id == credentials.vendor_id
            and approval.credential_generation == credentials.credential_generation
            and approval.credential_generation == declaration.credential_generation
            and approval.source_ip == declaration.source_ip
        ):
            raise CoupangCanaryError(ErrorClass.POLICY_BLOCKED, "COUPANG_APPROVAL_BINDING_MISMATCH")
        if not (approval.approved_at <= now <= approval.expires_at):
            raise CoupangCanaryError(ErrorClass.POLICY_BLOCKED, "COUPANG_APPROVAL_NOT_CURRENT")
        if declaration.recorded_at > now:
            raise CoupangCanaryError(ErrorClass.POLICY_BLOCKED, "COUPANG_IP_EVIDENCE_FROM_FUTURE")
        clock_age = now - self._clock_preflight.checked_at
        if clock_age < timedelta(0) or clock_age > CLOCK_PREFLIGHT_TTL:
            raise CoupangCanaryError(ErrorClass.POLICY_BLOCKED, "COUPANG_CLOCK_PREFLIGHT_STALE")
        if abs(self._clock_preflight.observed_skew_seconds) > MAX_CLOCK_SKEW_SECONDS:
            raise CoupangCanaryError(ErrorClass.POLICY_BLOCKED, "COUPANG_CLOCK_SKEW_UNSAFE")

    def run(self) -> CoupangCanaryResult:
        if self._used:
            raise CoupangCanaryError(ErrorClass.POLICY_BLOCKED, "COUPANG_CANARY_ALREADY_USED")
        now = _aware_utc(self._clock.now(), "clock.now")
        self._preflight(now)
        credentials = self._context.credentials
        path = f"{_PATH_PREFIX}{credentials.vendor_id}{_PATH_SUFFIX}"
        target = RequestTarget(Method.GET, path)
        headers = self._authenticator.headers(self._context, target).as_http_headers()
        self._used = True
        try:
            with (
                EGRESS.grant(EGRESS_OWNER, {COUPANG_PROVIDER_HOST}),
                httpx.Client(
                    base_url=COUPANG_BASE_URL,
                    timeout=_TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client,
                client.stream("GET", path, headers=headers) as response,
            ):
                status = response.status_code
                body = _read_bounded(response)
        except CoupangCanaryError:
            raise
        except EgressBlockedError:
            raise CoupangCanaryError(ErrorClass.POLICY_BLOCKED, "COUPANG_EGRESS_BLOCKED") from None
        except (httpx.TimeoutException, httpx.NetworkError):
            raise CoupangCanaryError(ErrorClass.TRANSIENT, "COUPANG_TRANSPORT_FAILED") from None
        except httpx.HTTPError:
            raise CoupangCanaryError(ErrorClass.UNKNOWN, "COUPANG_HTTP_STACK_FAILED") from None

        document = _document(body)
        if status in {301, 302, 303, 307, 308}:
            raise CoupangCanaryError(
                ErrorClass.UNKNOWN, "COUPANG_UNEXPECTED_REDIRECT", http_status=status
            )
        if status != 200:
            classified = classify_auth_response(status, _provider_message(document))
            raise CoupangCanaryError(classified.error_class, classified.code, http_status=status)
        if (
            document is None
            or document.get("code") != "SUCCESS"
            or not isinstance(document.get("data"), bool)
        ):
            raise CoupangCanaryError(
                ErrorClass.UNKNOWN, "COUPANG_SUCCESS_PREDICATE_FAILED", http_status=status
            )
        return CoupangCanaryResult(
            ENDPOINT_ID,
            status,
            bool(document["data"]),
            credentials.credential_generation,
        )
