"""Bounded read-only category metadata preflight for the approved Coupang KR test set.

The caller supplies one closed set of at most eight numeric display-category codes.  Each code is
requested at most once, one failure never causes a retry, and raw provider bodies never leave this
owner.  The parser is shared with the provider-zero catalog contract.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx

from app.platform.core.egress import EGRESS, EgressBlockedError
from app.platform.core.errors import ErrorClass
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
from integrations.marketplaces.coupang.canary import ClockPreflight
from integrations.marketplaces.coupang.catalog import (
    CategoryMetadata,
    CoupangCatalogContractError,
    category_metadata_from_data,
)
from integrations.marketplaces.coupang.errors import classify_auth_response

EGRESS_OWNER: Final = "marketplace:coupang:category-metadata-preflight"
MAX_CATEGORY_CODES: Final = 8
MAX_RESPONSE_BYTES: Final = 131_072
MAX_CLOCK_SKEW_SECONDS: Final = 5.0
CLOCK_PREFLIGHT_TTL: Final = timedelta(minutes=10)
CI_MARKERS: Final = ("CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST")
_CATEGORY_CODE: Final = re.compile(r"^[0-9]+$")
_PATH: Final = (
    "/v2/providers/seller_api/apis/api/v1/marketplace/meta/"
    "category-related-metas/display-category-codes/"
)
_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


class LiveCategoryMetadataTransportRefused(RuntimeError):
    """A real category metadata transport was requested in CI or a test process."""


@dataclass(frozen=True)
class CategoryMetadataAttempt:
    category_code: str
    metadata: CategoryMetadata | None
    error_class: ErrorClass | None = None
    error_code: str | None = None
    http_status: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.metadata is not None


@dataclass(frozen=True)
class CategoryMetadataPreflightResult:
    credential_generation: int
    attempts: tuple[CategoryMetadataAttempt, ...]


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _ci_or_test(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    for name in CI_MARKERS:
        if env.get(name):
            return name
    return "pytest" if "pytest" in sys.modules else None


def _read_bounded(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("COUPANG_CATEGORY_METADATA_RESPONSE_TOO_LARGE")
        chunks.append(chunk)
    return b"".join(chunks)


def _document(body: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _provider_message(document: Mapping[str, object] | None) -> str | None:
    if document is None:
        return None
    for name in ("message", "errorMessage"):
        value = document.get(name)
        if isinstance(value, str):
            return value
    return None


class CoupangCategoryMetadataPreflight:
    """Read each exact category once and retain only parsed contract fields."""

    def __init__(
        self,
        *,
        context: AuthContext,
        category_codes: tuple[str, ...],
        clock: UtcClock,
        clock_preflight: ClockPreflight,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if transport is None and (blocker := _ci_or_test(environ)) is not None:
            raise LiveCategoryMetadataTransportRefused(
                f"the live Coupang category metadata transport never exists under {blocker}"
            )
        if not 1 <= len(category_codes) <= MAX_CATEGORY_CODES:
            raise ValueError("category metadata preflight requires 1..8 category codes")
        if len(set(category_codes)) != len(category_codes) or any(
            _CATEGORY_CODE.fullmatch(code) is None for code in category_codes
        ):
            raise ValueError("category metadata codes must be unique numeric strings")
        self._context = context
        self._category_codes = category_codes
        self._clock = clock
        self._clock_preflight = clock_preflight
        self._transport = transport
        self._authenticator = Authenticator(clock)
        self._used = False

    def _preflight(self, now: datetime) -> None:
        declaration = self._context.ip_allowlist
        credentials = self._context.credentials
        if self._context.market is not Market.KR:
            raise ValueError("COUPANG_CATEGORY_METADATA_KR_ONLY")
        if declaration is None:
            raise ValueError("COUPANG_IP_EVIDENCE_MISSING")
        if declaration.credential_generation != credentials.credential_generation:
            raise ValueError("COUPANG_CATEGORY_METADATA_CREDENTIAL_GENERATION_MISMATCH")
        if declaration.recorded_at > now:
            raise ValueError("COUPANG_IP_EVIDENCE_FROM_FUTURE")
        age = now - self._clock_preflight.checked_at
        if age < timedelta(0) or age > CLOCK_PREFLIGHT_TTL:
            raise ValueError("COUPANG_CLOCK_PREFLIGHT_STALE")
        if abs(self._clock_preflight.observed_skew_seconds) > MAX_CLOCK_SKEW_SECONDS:
            raise ValueError("COUPANG_CLOCK_SKEW_UNSAFE")

    def _attempt(self, code: str) -> CategoryMetadataAttempt:
        path = f"{_PATH}{code}"
        target = RequestTarget(Method.GET, path)
        headers = self._authenticator.headers(self._context, target).as_http_headers()
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
        except EgressBlockedError:
            return CategoryMetadataAttempt(
                code, None, ErrorClass.POLICY_BLOCKED, "COUPANG_EGRESS_BLOCKED"
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return CategoryMetadataAttempt(
                code, None, ErrorClass.TRANSIENT, "COUPANG_TRANSPORT_FAILED"
            )
        except (httpx.HTTPError, ValueError):
            return CategoryMetadataAttempt(
                code, None, ErrorClass.UNKNOWN, "COUPANG_CATEGORY_METADATA_RESPONSE_FAILED"
            )

        document = _document(body)
        if status in {301, 302, 303, 307, 308}:
            return CategoryMetadataAttempt(
                code, None, ErrorClass.UNKNOWN, "COUPANG_UNEXPECTED_REDIRECT", status
            )
        if status != 200:
            classified = classify_auth_response(status, _provider_message(document))
            return CategoryMetadataAttempt(
                code, None, classified.error_class, classified.code, status
            )
        if document is None or document.get("code") != "SUCCESS":
            return CategoryMetadataAttempt(
                code,
                None,
                ErrorClass.UNKNOWN,
                "COUPANG_CATEGORY_METADATA_SUCCESS_PREDICATE_FAILED",
                status,
            )
        try:
            metadata = category_metadata_from_data(document.get("data"))
        except CoupangCatalogContractError:
            return CategoryMetadataAttempt(
                code,
                None,
                ErrorClass.UNKNOWN,
                "COUPANG_CATEGORY_METADATA_CONTRACT_INVALID",
                status,
            )
        return CategoryMetadataAttempt(code, metadata, http_status=status)

    def run(
        self,
        *,
        on_attempt: Callable[[CategoryMetadataAttempt], None] | None = None,
    ) -> CategoryMetadataPreflightResult:
        if self._used:
            raise ValueError("COUPANG_CATEGORY_METADATA_PREFLIGHT_ALREADY_USED")
        self._used = True
        self._preflight(_aware_utc(self._clock.now(), "clock.now"))
        attempts: list[CategoryMetadataAttempt] = []
        for code in self._category_codes:
            attempt = self._attempt(code)
            attempts.append(attempt)
            if on_attempt is not None:
                on_attempt(attempt)
        return CategoryMetadataPreflightResult(
            self._context.credentials.credential_generation,
            tuple(attempts),
        )


__all__ = [
    "CategoryMetadataAttempt",
    "CategoryMetadataPreflightResult",
    "CoupangCategoryMetadataPreflight",
    "LiveCategoryMetadataTransportRefused",
]
