"""Bounded, crash-safe live owner for the approved Coupang KR test products.

Only the eleven user-bound synthetic products are accepted.  This owner is deliberately
separate from the provider-zero ``products`` adapter: it owns one fixed create -> partial update
-> read-back -> delete workflow, persists an ambiguity barrier before every provider call, and
never retries a live request.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Final
from urllib.parse import quote, urlsplit

import httpx

from app.platform.core.egress import EGRESS, EgressBlockedError
from app.platform.core.errors import AppError, ErrorClass
from integrations.marketplaces.coupang.auth import (
    COUPANG_BASE_URL,
    COUPANG_PROVIDER_HOST,
    PROVIDER_READ_PROOF_ID,
    PROVIDER_READ_PROOF_TTL,
    AuthContext,
    Authenticator,
    Market,
    Method,
    RequestTarget,
    UtcClock,
)
from integrations.marketplaces.coupang.canary import ClockPreflight
from integrations.marketplaces.coupang.errors import classify_auth_response
from integrations.marketplaces.coupang.products import FrozenDocument

EGRESS_OWNER: Final = "marketplace:coupang:test-product-mutations"
SCOPE_ID: Final = "C-LIVE-MUT-1"
MAX_RESPONSE_BYTES: Final = 65_536
MAX_CLOCK_SKEW_SECONDS: Final = 5.0
CLOCK_PREFLIGHT_TTL: Final = timedelta(minutes=10)
APPROVAL_MAX_TTL: Final = timedelta(hours=24)
CI_MARKERS: Final = ("CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST")
_API: Final = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
_VENDOR_ID: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_PROVIDER_MARKER: Final = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,80}$")
_PROVIDER_DIAGNOSTIC_LIMIT: Final = 200
_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)


@dataclass(frozen=True)
class TestProductBinding:
    test_name: str
    synthetic_id: str
    source_product_id: str
    template: str


TEST_PRODUCT_BINDINGS: Final = (
    TestProductBinding(
        "test 1",
        "cbb23c35-9ce7-412d-a2da-ec81dfa02896",
        "synthetic-cbb23c35-9ce7-412d-a2da-ec81dfa02896",
        "kmretail/88",
    ),
    TestProductBinding(
        "test 2",
        "3c06c137-2110-4282-9265-667fcb4be9bc",
        "synthetic-3c06c137-2110-4282-9265-667fcb4be9bc",
        "kmretail/190",
    ),
    TestProductBinding(
        "test 3",
        "118a2702-4ab8-41a2-9110-f5e5d34797d5",
        "synthetic-118a2702-4ab8-41a2-9110-f5e5d34797d5",
        "kmretail/192",
    ),
    TestProductBinding(
        "test 4",
        "b1f6df31-eaff-4e9e-8fc0-faeb41aa43d9",
        "synthetic-b1f6df31-eaff-4e9e-8fc0-faeb41aa43d9",
        "kmretail/202",
    ),
    TestProductBinding(
        "test 5",
        "a85e4c5a-0660-4329-a2ad-1269c1406eea",
        "synthetic-a85e4c5a-0660-4329-a2ad-1269c1406eea",
        "kmretail/297",
    ),
    TestProductBinding(
        "test 6",
        "bfbcbdd9-70eb-488f-a4e5-adab52349e30",
        "synthetic-bfbcbdd9-70eb-488f-a4e5-adab52349e30",
        "kmretail/302",
    ),
    TestProductBinding(
        "test 7",
        "9a8cbeba-bc19-4d82-9423-c18b1feaa399",
        "synthetic-9a8cbeba-bc19-4d82-9423-c18b1feaa399",
        "kmretail/305",
    ),
    TestProductBinding(
        "test 8",
        "8177443a-5e9c-4dce-a570-6ef048f0bb34",
        "synthetic-8177443a-5e9c-4dce-a570-6ef048f0bb34",
        "kmretail/308",
    ),
    TestProductBinding(
        "test 9",
        "46481ebd-bf55-4cc4-a3bd-81e21d98d11a",
        "synthetic-46481ebd-bf55-4cc4-a3bd-81e21d98d11a",
        "kmretail/88",
    ),
    TestProductBinding(
        "test 10",
        "0c0cdcea-e508-4ce5-baee-90a9f6670a1b",
        "synthetic-0c0cdcea-e508-4ce5-baee-90a9f6670a1b",
        "kmretail/88",
    ),
    TestProductBinding(
        "test 11",
        "ca69022a-c0ff-4b4f-9dd3-75b6241b2666",
        "synthetic-ca69022a-c0ff-4b4f-9dd3-75b6241b2666",
        "kmretail/88",
    ),
)


class LiveMutationTransportRefused(RuntimeError):
    """A real mutation transport was requested in CI or a test process."""


class CoupangLiveMutationError(AppError):
    """Sanitized live-mutation failure; provider bodies and payloads never cross it."""

    def __init__(
        self,
        error_class: ErrorClass,
        code: str,
        *,
        http_status: int | None = None,
        provider_code: str | None = None,
        provider_message: str | None = None,
        provider_invalid_input: str | None = None,
    ) -> None:
        self.error_class = error_class
        self.http_status = http_status
        self.provider_code = provider_code
        self.provider_message = provider_message
        self.provider_invalid_input = provider_invalid_input
        details = {
            name: value
            for name, value in {
                "http_status": http_status,
                "provider_code": provider_code,
                "provider_message": provider_message,
                "provider_invalid_input": provider_invalid_input,
            }.items()
            if value is not None
        }
        super().__init__(code, "Coupang test-product mutation failed", details=details)


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


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("mutation bundle must be canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _public_https_url(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port != 443)
        or not parsed.path
    ):
        return False
    lowered = host.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith((".localhost", ".invalid")):
        return False
    try:
        address = ip_address(lowered)
    except ValueError:
        return "." in lowered
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _valid_coupang_cdn_path(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if any(character.isspace() for character in value) or "://" in value or value.startswith("//"):
        return False
    host = urlsplit(f"//{value}").hostname
    return host == "coupangcdn.com" or bool(host and host.endswith(".coupangcdn.com"))


def _validate_live_images(item: Mapping[str, object]) -> None:
    images = item.get("images")
    if (
        not isinstance(images, list)
        or not images
        or not all(isinstance(image, dict) for image in images)
    ):
        raise ValueError("each bounded test product needs provider image objects")
    representatives = 0
    for image in images:
        assert isinstance(image, dict)
        if image.get("imageType") == "REPRESENTATION":
            representatives += 1
        has_vendor = "vendorPath" in image
        has_cdn = "cdnPath" in image
        if has_vendor == has_cdn:
            raise ValueError("each live image must use exactly one of vendorPath or cdnPath")
        if has_vendor and not _public_https_url(image.get("vendorPath")):
            raise ValueError("vendorPath must be a public HTTPS image URL")
        if has_cdn and not _valid_coupang_cdn_path(image.get("cdnPath")):
            raise ValueError("cdnPath must be an existing Coupang CDN path")
    if representatives != 1:
        raise ValueError("each bounded test product needs exactly one representative image")


@dataclass(frozen=True)
class TestProductPlan:
    binding: TestProductBinding
    create_document: FrozenDocument
    update_message: str

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], binding: TestProductBinding
    ) -> TestProductPlan:
        if set(value) != {
            "test_name",
            "synthetic_id",
            "source_product_id",
            "template",
            "create",
            "update_message",
        }:
            raise ValueError("each mutation plan must use the closed C-LIVE-MUT-1 schema")
        if (
            value["test_name"],
            value["synthetic_id"],
            value["source_product_id"],
            value["template"],
        ) != (
            binding.test_name,
            binding.synthetic_id,
            binding.source_product_id,
            binding.template,
        ):
            raise ValueError("mutation plan does not match the approved test-product binding")
        create = value["create"]
        message = value["update_message"]
        if not isinstance(create, Mapping) or not isinstance(message, str) or not message:
            raise ValueError("mutation plan create/update values are invalid")
        document = FrozenDocument.from_mapping(create)
        body = document.value()
        index = int(binding.test_name.removeprefix("test "))
        if body.get("sellerProductName") != f"ICBM TEST {index}":
            raise ValueError("sellerProductName must be the exact bounded ICBM test label")
        if body.get("requested") is not False:
            raise ValueError("C-LIVE-MUT-1 forbids requesting product approval")
        items = body.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("each bounded test product must contain exactly one item")
        if items[0].get("externalVendorSku") != binding.source_product_id:
            raise ValueError("externalVendorSku must equal the bound synthetic source product id")
        _validate_live_images(items[0])
        if len(message) > 100 or "ICBM TEST" not in message:
            raise ValueError("update_message must remain an ICBM test marker")
        return cls(binding, document, message)


@dataclass(frozen=True)
class TestProductBundle:
    plans: tuple[TestProductPlan, ...]
    payload_digest: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TestProductBundle:
        if set(value) != {"products"} or not isinstance(value["products"], list):
            raise ValueError("mutation bundle must contain only products")
        rows = value["products"]
        if len(rows) != len(TEST_PRODUCT_BINDINGS):
            raise ValueError("mutation bundle must contain exactly test 1 through test 11")
        plans: list[TestProductPlan] = []
        for row, binding in zip(rows, TEST_PRODUCT_BINDINGS, strict=True):
            if not isinstance(row, Mapping):
                raise ValueError("mutation product plan must be an object")
            plans.append(TestProductPlan.from_mapping(row, binding))
        return cls(tuple(plans), _sha256(_canonical(value)))


@dataclass(frozen=True)
class TestProductMutationApproval:
    scope_id: str
    vendor_id: str
    credential_generation: int
    payload_digest: str
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
        payload_digest: str,
        approved_at: datetime,
        expires_at: datetime,
    ) -> TestProductMutationApproval:
        start = _aware_utc(approved_at, "approved_at")
        end = _aware_utc(expires_at, "expires_at")
        if scope_id != SCOPE_ID:
            raise ValueError("scope_id must be C-LIVE-MUT-1")
        if _VENDOR_ID.fullmatch(vendor_id) is None:
            raise ValueError("vendor_id is unsafe")
        if not isinstance(credential_generation, int) or credential_generation < 1:
            raise ValueError("credential_generation must be positive")
        if re.fullmatch(r"[0-9a-f]{64}", payload_digest) is None:
            raise ValueError("payload_digest must be a lowercase SHA-256 digest")
        if end <= start or end - start > APPROVAL_MAX_TTL:
            raise ValueError("approval window must be positive and at most 24 hours")
        return cls(
            scope_id,
            vendor_id,
            credential_generation,
            payload_digest,
            start,
            end,
        )


@dataclass(frozen=True)
class TestProductMutationResult:
    scope_id: str
    payload_digest: str
    completed: int
    total: int
    credential_generation: int


@dataclass(frozen=True)
class AmbiguousCreateRecoveryResult:
    scope_id: str
    payload_digest: str
    test_name: str
    recovered: bool
    credential_generation: int


@dataclass(frozen=True)
class RemainingTestProductIssue:
    test_name: str
    journal_state: str
    code: str
    provider_code: str | None = None
    provider_message: str | None = None
    provider_invalid_input: str | None = None


@dataclass(frozen=True)
class RemainingTestProductMutationResult(TestProductMutationResult):
    attempted: int
    quarantined: tuple[RemainingTestProductIssue, ...]


class MutationJournal:
    """Atomic ambiguity barrier; stores identities, states and redacted diagnostics only."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_UNREADABLE"
            ) from exc
        if not isinstance(value, dict):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_INVALID"
            )
        return value

    def commit(self, value: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        encoded = _canonical(value)
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_WRITE_FAILED"
            ) from exc


def _read_bounded(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise CoupangLiveMutationError(ErrorClass.UNKNOWN, "COUPANG_RESPONSE_TOO_LARGE")
        chunks.append(chunk)
    return b"".join(chunks)


def _response_document(body: bytes) -> dict[str, object] | None:
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


def _provider_marker(value: object) -> str | None:
    return value if isinstance(value, str) and _PROVIDER_MARKER.fullmatch(value) else None


def _diagnostic_text(value: object, *, forbidden: tuple[str, ...] = ()) -> str | None:
    """Keep one bounded provider explanation without retaining echoed request values."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in (
            "authorization",
            "bearer ",
            "access_key",
            "accesskey",
            "secret_key",
            "secretkey",
        )
    ):
        return None
    for submitted in sorted(
        {candidate for candidate in forbidden if len(candidate) >= 4},
        key=len,
        reverse=True,
    ):
        text = text.replace(submitted, "[redacted]")
    return text[:_PROVIDER_DIAGNOSTIC_LIMIT]


def _invalid_input(
    document: Mapping[str, object], *, forbidden: tuple[str, ...] = ()
) -> str | None:
    values = document.get("invalidInputs")
    if not isinstance(values, list):
        return None
    summaries: list[str] = []
    for value in values[:3]:
        if not isinstance(value, dict):
            continue
        name = _provider_marker(value.get("name"))
        kind = _provider_marker(value.get("type"))
        message = _diagnostic_text(value.get("message"), forbidden=forbidden)
        parts = [part for part in (name, kind, message) if part]
        if parts:
            summaries.append(": ".join(parts))
    return _diagnostic_text("; ".join(summaries), forbidden=forbidden)


def _provider_rejection(
    document: Mapping[str, object], *, forbidden: tuple[str, ...] = ()
) -> tuple[str | None, str | None, str | None]:
    """Extract only allowlisted diagnostics from top-level or nested provider errors."""
    nested = document.get("data")
    error = nested if isinstance(nested, dict) and nested.get("code") == "ERROR" else document
    provider_code = _provider_marker(error.get("errorCode"))
    if provider_code is None:
        candidate = _provider_marker(error.get("code"))
        provider_code = candidate if candidate not in {"ERROR", "SUCCESS"} else None
    provider_message = _diagnostic_text(
        error.get("message", error.get("errorMessage")),
        forbidden=forbidden,
    )
    provider_invalid_input = _invalid_input(error, forbidden=forbidden)
    return provider_code, provider_message, provider_invalid_input


def _submitted_text_values(document: FrozenDocument | None) -> tuple[str, ...]:
    if document is None:
        return ()
    values: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(document.value())
    return tuple(values)


class CoupangTestProductMutationRun:
    """One fixed KR run for test 1..11; each provider call is dispatched at most once."""

    def __init__(
        self,
        *,
        context: AuthContext,
        approval: TestProductMutationApproval,
        bundle: TestProductBundle,
        clock: UtcClock,
        clock_preflight: ClockPreflight,
        journal: MutationJournal,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if transport is None and (blocker := _ci_or_test(environ)) is not None:
            raise LiveMutationTransportRefused(
                f"the live Coupang mutation transport never exists under {blocker}"
            )
        self._context = context
        self._approval = approval
        self._bundle = bundle
        self._clock = clock
        self._clock_preflight = clock_preflight
        self._journal = journal
        self._transport = transport
        self._authenticator = Authenticator(clock)
        self._used = False

    def _preflight(self, now: datetime) -> None:
        credentials = self._context.credentials
        proof = self._context.provider_read_proof
        approval = self._approval
        if self._context.market is not Market.KR or approval.market is not Market.KR:
            raise CoupangLiveMutationError(ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_KR_ONLY")
        if proof is None:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_CANARY_PROOF_MISSING"
            )
        if not (
            approval.vendor_id == credentials.vendor_id
            and approval.credential_generation == credentials.credential_generation
            and proof.endpoint_id == PROVIDER_READ_PROOF_ID
            and proof.credential_generation == credentials.credential_generation
            and approval.payload_digest == self._bundle.payload_digest
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_APPROVAL_BINDING_MISMATCH"
            )
        if any(
            plan.create_document.value().get("vendorId") != credentials.vendor_id
            for plan in self._bundle.plans
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_VENDOR_BODY_MISMATCH"
            )
        if not (approval.approved_at <= now <= approval.expires_at):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_APPROVAL_NOT_CURRENT"
            )
        proof_age = now - proof.verified_at
        if proof_age < timedelta(0) or proof_age > PROVIDER_READ_PROOF_TTL:
            raise CoupangLiveMutationError(ErrorClass.POLICY_BLOCKED, "COUPANG_CANARY_PROOF_STALE")
        age = now - self._clock_preflight.checked_at
        if age < timedelta(0) or age > CLOCK_PREFLIGHT_TTL:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_CLOCK_PREFLIGHT_STALE"
            )
        if abs(self._clock_preflight.observed_skew_seconds) > MAX_CLOCK_SKEW_SECONDS:
            raise CoupangLiveMutationError(ErrorClass.POLICY_BLOCKED, "COUPANG_CLOCK_SKEW_UNSAFE")

    def _request(
        self, method: Method, path: str, *, document: FrozenDocument | None = None
    ) -> dict[str, object]:
        target = RequestTarget(method, path)
        headers = self._authenticator.headers(self._context, target).as_http_headers()
        request_method = method.value
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
                client.stream(
                    request_method,
                    path,
                    headers=headers,
                    content=None if document is None else document.body,
                ) as response,
            ):
                status = response.status_code
                body = _read_bounded(response)
        except CoupangLiveMutationError:
            raise
        except EgressBlockedError:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_EGRESS_BLOCKED"
            ) from None
        except (httpx.TimeoutException, httpx.NetworkError):
            raise CoupangLiveMutationError(
                ErrorClass.TRANSIENT, "COUPANG_MUTATION_TRANSPORT_AMBIGUOUS"
            ) from None
        except httpx.HTTPError:
            raise CoupangLiveMutationError(
                ErrorClass.UNKNOWN, "COUPANG_MUTATION_HTTP_STACK_FAILED"
            ) from None
        parsed = _response_document(body)
        forbidden = (
            self._context.credentials.access_key,
            self._context.credentials.secret_key,
            self._context.credentials.vendor_id,
            *_submitted_text_values(document),
        )
        if status in {301, 302, 303, 307, 308}:
            raise CoupangLiveMutationError(
                ErrorClass.UNKNOWN, "COUPANG_UNEXPECTED_REDIRECT", http_status=status
            )
        if status != 200:
            classified = classify_auth_response(status, _provider_message(parsed))
            raise CoupangLiveMutationError(
                classified.error_class, classified.code, http_status=status
            )
        if parsed is not None and parsed.get("code") == "ERROR":
            provider_code, provider_message, provider_invalid_input = _provider_rejection(
                parsed,
                forbidden=forbidden,
            )
            raise CoupangLiveMutationError(
                ErrorClass.VALIDATION,
                "COUPANG_MUTATION_PROVIDER_REJECTED",
                http_status=status,
                provider_code=provider_code,
                provider_message=provider_message,
                provider_invalid_input=provider_invalid_input,
            )
        if parsed is None or parsed.get("code") not in {"SUCCESS", "200", 200}:
            raise CoupangLiveMutationError(
                ErrorClass.UNKNOWN, "COUPANG_MUTATION_SUCCESS_PREDICATE_FAILED", http_status=status
            )
        return parsed

    @staticmethod
    def _created_identity(
        response: Mapping[str, object], *, forbidden: tuple[str, ...] = ()
    ) -> int:
        data = response.get("data")
        if isinstance(data, dict) and data.get("code") == "ERROR":
            provider_code, provider_message, provider_invalid_input = _provider_rejection(
                response,
                forbidden=forbidden,
            )
            raise CoupangLiveMutationError(
                ErrorClass.VALIDATION,
                "COUPANG_MUTATION_PROVIDER_REJECTED",
                provider_code=provider_code,
                provider_message=provider_message,
                provider_invalid_input=provider_invalid_input,
            )
        if isinstance(data, dict) and data.get("code") == "SUCCESS":
            data = data.get("data")
        if not isinstance(data, int) or isinstance(data, bool) or data < 1:
            raise CoupangLiveMutationError(
                ErrorClass.UNKNOWN, "COUPANG_MUTATION_CREATE_IDENTITY_MISSING"
            )
        return data

    @staticmethod
    def _full_update_document(
        plan: TestProductPlan,
        *,
        seller_product_id: int,
        readback: Mapping[str, object],
    ) -> FrozenDocument:
        remote_items = readback.get("items")
        if not isinstance(remote_items, list):
            raise CoupangLiveMutationError(
                ErrorClass.UNKNOWN, "COUPANG_MUTATION_UPDATE_ITEM_IDENTITY_MISSING"
            )
        matching = [
            item
            for item in remote_items
            if isinstance(item, dict)
            and item.get("externalVendorSku") == plan.binding.source_product_id
        ]
        if len(remote_items) != 1 or len(matching) != 1:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_UPDATE_ITEM_SCOPE_MISMATCH"
            )
        seller_product_item_id = matching[0].get("sellerProductItemId")
        if (
            not isinstance(seller_product_item_id, int)
            or isinstance(seller_product_item_id, bool)
            or seller_product_item_id < 1
        ):
            raise CoupangLiveMutationError(
                ErrorClass.UNKNOWN, "COUPANG_MUTATION_UPDATE_ITEM_IDENTITY_MISSING"
            )
        document = plan.create_document.value()
        document["sellerProductId"] = seller_product_id
        document["sellerProductName"] = plan.update_message
        items = document["items"]
        assert isinstance(items, list) and isinstance(items[0], dict)
        items[0]["sellerProductItemId"] = seller_product_item_id
        vendor_item_id = matching[0].get("vendorItemId")
        if vendor_item_id is not None and (
            not isinstance(vendor_item_id, int)
            or isinstance(vendor_item_id, bool)
            or vendor_item_id < 1
        ):
            raise CoupangLiveMutationError(
                ErrorClass.UNKNOWN, "COUPANG_MUTATION_UPDATE_VENDOR_ITEM_ID_INVALID"
            )
        items[0]["vendorItemId"] = vendor_item_id
        return FrozenDocument.from_mapping(document)

    @staticmethod
    def _verify_identity_scope(
        response: Mapping[str, object],
        *,
        seller_product_id: int,
        source_product_id: str,
        seller_product_name: str,
    ) -> Mapping[str, object]:
        data = response.get("data")
        if not isinstance(data, dict) or data.get("sellerProductId") != seller_product_id:
            raise CoupangLiveMutationError(
                ErrorClass.UNKNOWN, "COUPANG_MUTATION_READBACK_IDENTITY_MISMATCH"
            )
        if data.get("statusName") != "SAVED" and data.get("status") != "SAVED":
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_DELETE_NOT_SAVED"
            )
        if data.get("sellerProductName") != seller_product_name:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_READBACK_NAME_MISMATCH"
            )
        items = data.get("items")
        if (
            not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
            or items[0].get("externalVendorSku") != source_product_id
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_READBACK_SCOPE_MISMATCH"
            )
        return data

    @classmethod
    def _verify_readback(
        cls,
        response: Mapping[str, object],
        *,
        seller_product_id: int,
        source_product_id: str,
        seller_product_name: str,
    ) -> None:
        cls._verify_identity_scope(
            response,
            seller_product_id=seller_product_id,
            source_product_id=source_product_id,
            seller_product_name=seller_product_name,
        )

    @staticmethod
    def _assert_unique_identity(
        records: Mapping[str, object], *, synthetic_id: str, seller_product_id: int
    ) -> None:
        for other_id, other in records.items():
            if other_id == synthetic_id or not isinstance(other, dict):
                continue
            if other.get("seller_product_id") == seller_product_id:
                raise CoupangLiveMutationError(
                    ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_DUPLICATE_CREATE_IDENTITY"
                )

    def _initial_journal(self) -> dict[str, object]:
        return {
            "version": 1,
            "scope_id": self._approval.scope_id,
            "payload_digest": self._bundle.payload_digest,
            "credential_generation": self._approval.credential_generation,
            "records": {
                plan.binding.synthetic_id: {"state": "pending"} for plan in self._bundle.plans
            },
        }

    def _load_journal(self) -> dict[str, object]:
        state = self._journal.load()
        if state is None:
            state = self._initial_journal()
            self._journal.commit(state)
        if (
            state.get("version") != 1
            or state.get("scope_id") != self._approval.scope_id
            or state.get("payload_digest") != self._bundle.payload_digest
            or state.get("credential_generation") != self._approval.credential_generation
            or not isinstance(state.get("records"), dict)
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_BINDING_MISMATCH"
            )
        records = state["records"]
        assert isinstance(records, dict)
        if set(records) != {plan.binding.synthetic_id for plan in self._bundle.plans}:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_SCOPE_MISMATCH"
            )
        for record in records.values():
            if not isinstance(record, dict) or str(record.get("state", "")).endswith("_inflight"):
                raise CoupangLiveMutationError(
                    ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_RECOVERY_REQUIRED"
                )
        return state

    def _transition(
        self,
        state: dict[str, object],
        synthetic_id: str,
        phase: str,
        *,
        seller_product_id: int | None = None,
        failure: CoupangLiveMutationError | None = None,
    ) -> None:
        records = state["records"]
        assert isinstance(records, dict)
        record: dict[str, object] = {"state": phase}
        if seller_product_id is not None:
            record["seller_product_id"] = seller_product_id
        if failure is not None:
            record.update(
                {
                    name: value
                    for name, value in {
                        "provider_code": failure.provider_code,
                        "provider_message": failure.provider_message,
                        "provider_invalid_input": failure.provider_invalid_input,
                    }.items()
                    if value is not None
                }
            )
        records[synthetic_id] = record
        self._journal.commit(state)

    def _run_plan(
        self,
        state: dict[str, object],
        records: dict[str, object],
        plan: TestProductPlan,
    ) -> None:
        verified_in_this_run = False
        synthetic_id = plan.binding.synthetic_id
        record = records[synthetic_id]
        assert isinstance(record, dict)
        phase = record["state"]
        seller_product_id = record.get("seller_product_id")
        if phase == "pending":
            self._transition(state, synthetic_id, "create_inflight")
            try:
                created = self._request(Method.POST, _API, document=plan.create_document)
                seller_product_id = self._created_identity(
                    created,
                    forbidden=(
                        self._context.credentials.access_key,
                        self._context.credentials.secret_key,
                        self._context.credentials.vendor_id,
                        *_submitted_text_values(plan.create_document),
                    ),
                )
            except CoupangLiveMutationError as exc:
                if exc.code == "COUPANG_MUTATION_PROVIDER_REJECTED":
                    self._transition(state, synthetic_id, "create_rejected", failure=exc)
                raise
            self._transition(state, synthetic_id, "created", seller_product_id=seller_product_id)
            phase = "created"
        if not isinstance(seller_product_id, int) or seller_product_id < 1:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_IDENTITY_MISSING"
            )
        self._assert_unique_identity(
            records,
            synthetic_id=synthetic_id,
            seller_product_id=seller_product_id,
        )
        if phase == "created":
            resumed = self._request(Method.GET, f"{_API}/{seller_product_id}")
            remote = self._verify_identity_scope(
                resumed,
                seller_product_id=seller_product_id,
                source_product_id=plan.binding.source_product_id,
                seller_product_name=str(plan.create_document.value()["sellerProductName"]),
            )
            update = self._full_update_document(
                plan,
                seller_product_id=seller_product_id,
                readback=remote,
            )
            self._transition(
                state, synthetic_id, "update_inflight", seller_product_id=seller_product_id
            )
            self._request(Method.PUT, _API, document=update)
            self._transition(state, synthetic_id, "updated", seller_product_id=seller_product_id)
            phase = "updated"
        if phase == "updated":
            self._transition(
                state, synthetic_id, "readback_inflight", seller_product_id=seller_product_id
            )
            readback = self._request(Method.GET, f"{_API}/{seller_product_id}")
            self._verify_readback(
                readback,
                seller_product_id=seller_product_id,
                source_product_id=plan.binding.source_product_id,
                seller_product_name=plan.update_message,
            )
            self._transition(state, synthetic_id, "verified", seller_product_id=seller_product_id)
            phase = "verified"
            verified_in_this_run = True
        if phase == "verified":
            if not verified_in_this_run:
                resumed = self._request(Method.GET, f"{_API}/{seller_product_id}")
                self._verify_readback(
                    resumed,
                    seller_product_id=seller_product_id,
                    source_product_id=plan.binding.source_product_id,
                    seller_product_name=plan.update_message,
                )
            self._transition(
                state, synthetic_id, "delete_inflight", seller_product_id=seller_product_id
            )
            self._request(Method.DELETE, f"{_API}/{seller_product_id}")
            self._transition(state, synthetic_id, "deleted", seller_product_id=seller_product_id)
            phase = "deleted"
        if phase != "deleted":
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_STATE_INVALID"
            )

    def run(self) -> TestProductMutationResult:
        if self._used:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_RUN_ALREADY_USED"
            )
        self._used = True
        now = _aware_utc(self._clock.now(), "clock.now")
        self._preflight(now)
        state = self._load_journal()
        records = state["records"]
        assert isinstance(records, dict)
        completed = 0
        for plan in self._bundle.plans:
            self._run_plan(state, records, plan)
            completed += 1
        return TestProductMutationResult(
            self._approval.scope_id,
            self._bundle.payload_digest,
            completed,
            len(self._bundle.plans),
            self._approval.credential_generation,
        )


class CoupangRemainingTestProductMutationRun(CoupangTestProductMutationRun):
    """Quarantine test 1's ambiguous create and finish each still-pending test once.

    The owner is intentionally one-shot and accepts only the exact durable state produced by
    the original test-1 ambiguity: test 1 is ``create_inflight`` and tests 2..11 are ``pending``.
    A failed request is never retried in this run. Its journal state is retained and the next
    independent pending test is attempted.
    """

    def _load_continuation_journal(self) -> dict[str, object]:
        state = self._journal.load()
        if state is None:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_CONTINUATION_JOURNAL_MISSING"
            )
        if (
            state.get("version") != 1
            or state.get("scope_id") != self._approval.scope_id
            or state.get("payload_digest") != self._bundle.payload_digest
            or state.get("credential_generation") != self._approval.credential_generation
            or not isinstance(state.get("records"), dict)
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_BINDING_MISMATCH"
            )
        records = state["records"]
        assert isinstance(records, dict)
        if set(records) != {plan.binding.synthetic_id for plan in self._bundle.plans}:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_SCOPE_MISMATCH"
            )
        expected = {
            self._bundle.plans[0].binding.synthetic_id: "create_inflight",
            **{plan.binding.synthetic_id: "pending" for plan in self._bundle.plans[1:]},
        }
        if any(
            not isinstance(records.get(synthetic_id), dict)
            or records[synthetic_id].get("state") != phase
            or set(records[synthetic_id]) != {"state"}
            for synthetic_id, phase in expected.items()
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_CONTINUATION_SCOPE_INVALID"
            )
        return state

    def run(self) -> RemainingTestProductMutationResult:
        if self._used:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_RUN_ALREADY_USED"
            )
        self._used = True
        self._preflight(_aware_utc(self._clock.now(), "clock.now"))
        state = self._load_continuation_journal()
        records = state["records"]
        assert isinstance(records, dict)
        issues = [
            RemainingTestProductIssue(
                self._bundle.plans[0].binding.test_name,
                "create_inflight",
                "COUPANG_MUTATION_AMBIGUOUS_CREATE_QUARANTINED",
            )
        ]
        completed = 0
        attempted = 0
        for plan in self._bundle.plans[1:]:
            attempted += 1
            try:
                self._run_plan(state, records, plan)
            except CoupangLiveMutationError as exc:
                record = records[plan.binding.synthetic_id]
                assert isinstance(record, dict)
                issues.append(
                    RemainingTestProductIssue(
                        plan.binding.test_name,
                        str(record.get("state", "unknown")),
                        exc.code,
                        exc.provider_code,
                        exc.provider_message,
                        exc.provider_invalid_input,
                    )
                )
                continue
            completed += 1
        return RemainingTestProductMutationResult(
            scope_id=self._approval.scope_id,
            payload_digest=self._bundle.payload_digest,
            completed=completed,
            total=len(self._bundle.plans) - 1,
            credential_generation=self._approval.credential_generation,
            attempted=attempted,
            quarantined=tuple(issues),
        )


class CoupangCorrectedRejectedTestProductMutationRun(CoupangTestProductMutationRun):
    """Apply one changed payload only to tests whose earlier CREATE was explicitly rejected.

    Test 1's ambiguous old CREATE remains quarantined forever in this owner. Tests 2..11 may move
    from the exact old ``create_rejected`` records to one new attempt because the reviewed payload
    digest changed. A crash barrier or ambiguous result still stops that item from being retried.
    """

    def __init__(
        self,
        *,
        context: AuthContext,
        approval: TestProductMutationApproval,
        bundle: TestProductBundle,
        clock: UtcClock,
        clock_preflight: ClockPreflight,
        journal: MutationJournal,
        prior_payload_digest: str,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            context=context,
            approval=approval,
            bundle=bundle,
            clock=clock,
            clock_preflight=clock_preflight,
            journal=journal,
            transport=transport,
            environ=environ,
        )
        if (
            re.fullmatch(r"[0-9a-f]{64}", prior_payload_digest) is None
            or prior_payload_digest == self._bundle.payload_digest
        ):
            raise ValueError("corrected run needs one distinct prior payload digest")
        self._prior_payload_digest = prior_payload_digest

    def _migrate_rejected_journal(self) -> dict[str, object]:
        state = self._journal.load()
        if state is None:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_CORRECTED_JOURNAL_MISSING"
            )
        if (
            state.get("version") != 1
            or state.get("scope_id") != self._approval.scope_id
            or state.get("payload_digest") != self._prior_payload_digest
            or state.get("credential_generation") != self._approval.credential_generation
            or not isinstance(state.get("records"), dict)
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_CORRECTED_JOURNAL_BINDING_MISMATCH"
            )
        records = state["records"]
        assert isinstance(records, dict)
        expected_ids = {plan.binding.synthetic_id for plan in self._bundle.plans}
        allowed_rejection_keys = {
            "state",
            "provider_code",
            "provider_message",
            "provider_invalid_input",
        }
        first_id = self._bundle.plans[0].binding.synthetic_id
        if (
            set(records) != expected_ids
            or records.get(first_id) != {"state": "create_inflight"}
            or any(
                not isinstance(records.get(plan.binding.synthetic_id), dict)
                or records[plan.binding.synthetic_id].get("state") != "create_rejected"
                or not set(records[plan.binding.synthetic_id]).issubset(allowed_rejection_keys)
                for plan in self._bundle.plans[1:]
            )
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_CORRECTED_JOURNAL_SCOPE_INVALID"
            )
        migrated: dict[str, object] = {
            "version": 2,
            "scope_id": self._approval.scope_id,
            "payload_digest": self._bundle.payload_digest,
            "prior_payload_digest": self._prior_payload_digest,
            "credential_generation": self._approval.credential_generation,
            "records": {
                first_id: {
                    "state": "create_inflight",
                    "payload_digest": self._prior_payload_digest,
                },
                **{
                    plan.binding.synthetic_id: {
                        "state": "pending",
                        "prior_state": "create_rejected",
                    }
                    for plan in self._bundle.plans[1:]
                },
            },
        }
        self._journal.commit(migrated)
        return migrated

    def run(self) -> RemainingTestProductMutationResult:
        if self._used:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_RUN_ALREADY_USED"
            )
        self._used = True
        self._preflight(_aware_utc(self._clock.now(), "clock.now"))
        state = self._migrate_rejected_journal()
        records = state["records"]
        assert isinstance(records, dict)
        issues = [
            RemainingTestProductIssue(
                self._bundle.plans[0].binding.test_name,
                "create_inflight",
                "COUPANG_MUTATION_AMBIGUOUS_CREATE_QUARANTINED",
            )
        ]
        completed = 0
        attempted = 0
        for plan in self._bundle.plans[1:]:
            attempted += 1
            try:
                self._run_plan(state, records, plan)
            except CoupangLiveMutationError as exc:
                record = records[plan.binding.synthetic_id]
                assert isinstance(record, dict)
                issues.append(
                    RemainingTestProductIssue(
                        plan.binding.test_name,
                        str(record.get("state", "unknown")),
                        exc.code,
                        exc.provider_code,
                        exc.provider_message,
                        exc.provider_invalid_input,
                    )
                )
                continue
            completed += 1
        return RemainingTestProductMutationResult(
            scope_id=self._approval.scope_id,
            payload_digest=self._bundle.payload_digest,
            completed=completed,
            total=len(self._bundle.plans) - 1,
            credential_generation=self._approval.credential_generation,
            attempted=attempted,
            quarantined=tuple(issues),
        )


class CoupangCorrectedAmbiguousCreateCompletion(CoupangTestProductMutationRun):
    """Resolve test 1 positively, then update/read/delete it with the corrected payload.

    This owner is available only after the corrected tests 2..11 are all deleted.  It never
    treats an empty lookup as absence and never repeats CREATE.
    """

    def __init__(
        self,
        *,
        context: AuthContext,
        approval: TestProductMutationApproval,
        bundle: TestProductBundle,
        clock: UtcClock,
        clock_preflight: ClockPreflight,
        journal: MutationJournal,
        prior_payload_digest: str,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            context=context,
            approval=approval,
            bundle=bundle,
            clock=clock,
            clock_preflight=clock_preflight,
            journal=journal,
            transport=transport,
            environ=environ,
        )
        if (
            re.fullmatch(r"[0-9a-f]{64}", prior_payload_digest) is None
            or prior_payload_digest == bundle.payload_digest
        ):
            raise ValueError("corrected recovery needs one distinct prior payload digest")
        self._prior_payload_digest = prior_payload_digest

    def _completion_state(self) -> tuple[dict[str, object], dict[str, object], TestProductPlan]:
        state = self._journal.load()
        if state is None:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_CORRECTED_JOURNAL_MISSING"
            )
        if (
            state.get("version") != 2
            or state.get("scope_id") != self._approval.scope_id
            or state.get("payload_digest") != self._bundle.payload_digest
            or state.get("prior_payload_digest") != self._prior_payload_digest
            or state.get("credential_generation") != self._approval.credential_generation
            or not isinstance(state.get("records"), dict)
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_CORRECTED_JOURNAL_BINDING_MISMATCH"
            )
        records = state["records"]
        assert isinstance(records, dict)
        expected_ids = {plan.binding.synthetic_id for plan in self._bundle.plans}
        first = self._bundle.plans[0]
        first_record = records.get(first.binding.synthetic_id)
        if (
            set(records) != expected_ids
            or first_record
            != {"state": "create_inflight", "payload_digest": self._prior_payload_digest}
            or any(
                not isinstance(records.get(plan.binding.synthetic_id), dict)
                or records[plan.binding.synthetic_id].get("state") != "deleted"
                or not isinstance(records[plan.binding.synthetic_id].get("seller_product_id"), int)
                for plan in self._bundle.plans[1:]
            )
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_CORRECTED_RECOVERY_SCOPE_INVALID"
            )
        return state, records, first

    def run(self) -> TestProductMutationResult:
        if self._used:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_RUN_ALREADY_USED"
            )
        self._used = True
        self._preflight(_aware_utc(self._clock.now(), "clock.now"))
        state, records, plan = self._completion_state()
        source_product_id = plan.binding.source_product_id
        summary = self._request(
            Method.GET,
            f"{_API}/external-vendor-sku-codes/{quote(source_product_id, safe='')}",
        )
        data = summary.get("data")
        if not isinstance(data, list):
            raise CoupangLiveMutationError(
                ErrorClass.UNKNOWN, "COUPANG_MUTATION_RECOVERY_RESPONSE_INVALID"
            )
        expected_name = str(plan.create_document.value()["sellerProductName"])
        vendor_id = self._context.credentials.vendor_id
        if not data:
            raise CoupangLiveMutationError(
                ErrorClass.REVIEW_REQUIRED, "COUPANG_MUTATION_RECOVERY_NO_POSITIVE_MATCH"
            )
        if len(data) != 1:
            raise CoupangLiveMutationError(
                ErrorClass.REVIEW_REQUIRED, "COUPANG_MUTATION_RECOVERY_MULTIPLE_MATCHES"
            )
        candidate = data[0]
        if (
            not isinstance(candidate, dict)
            or candidate.get("vendorId") != vendor_id
            or candidate.get("sellerProductName") != expected_name
            or not isinstance(candidate.get("sellerProductId"), int)
            or isinstance(candidate.get("sellerProductId"), bool)
            or int(candidate["sellerProductId"]) < 1
        ):
            raise CoupangLiveMutationError(
                ErrorClass.REVIEW_REQUIRED, "COUPANG_MUTATION_RECOVERY_CANDIDATE_MISMATCH"
            )
        seller_product_id = int(candidate["sellerProductId"])
        self._assert_unique_identity(
            records,
            synthetic_id=plan.binding.synthetic_id,
            seller_product_id=seller_product_id,
        )
        readback = self._request(Method.GET, f"{_API}/{seller_product_id}")
        remote = self._verify_identity_scope(
            readback,
            seller_product_id=seller_product_id,
            source_product_id=source_product_id,
            seller_product_name=expected_name,
        )
        if remote.get("vendorId") != vendor_id:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_RECOVERY_VENDOR_MISMATCH"
            )
        update = self._full_update_document(
            plan,
            seller_product_id=seller_product_id,
            readback=remote,
        )
        self._transition(
            state,
            plan.binding.synthetic_id,
            "update_inflight",
            seller_product_id=seller_product_id,
        )
        self._request(Method.PUT, _API, document=update)
        self._transition(
            state,
            plan.binding.synthetic_id,
            "updated",
            seller_product_id=seller_product_id,
        )
        self._transition(
            state,
            plan.binding.synthetic_id,
            "readback_inflight",
            seller_product_id=seller_product_id,
        )
        updated = self._request(Method.GET, f"{_API}/{seller_product_id}")
        self._verify_readback(
            updated,
            seller_product_id=seller_product_id,
            source_product_id=source_product_id,
            seller_product_name=plan.update_message,
        )
        self._transition(
            state,
            plan.binding.synthetic_id,
            "verified",
            seller_product_id=seller_product_id,
        )
        self._transition(
            state,
            plan.binding.synthetic_id,
            "delete_inflight",
            seller_product_id=seller_product_id,
        )
        self._request(Method.DELETE, f"{_API}/{seller_product_id}")
        self._transition(
            state,
            plan.binding.synthetic_id,
            "deleted",
            seller_product_id=seller_product_id,
        )
        return TestProductMutationResult(
            self._approval.scope_id,
            self._bundle.payload_digest,
            1,
            1,
            self._approval.credential_generation,
        )


class CoupangAmbiguousCreateRecovery(CoupangTestProductMutationRun):
    """Positive-only identity recovery for the one C-LIVE-MUT-1 create barrier.

    This owner performs only the documented externalVendorSku summary lookup and one
    sellerProductId read-back.  Empty, multiple, partial, or mismatching answers never prove
    absence and never change the journal.
    """

    def __init__(
        self,
        *,
        context: AuthContext,
        bundle: TestProductBundle,
        clock: UtcClock,
        clock_preflight: ClockPreflight,
        journal: MutationJournal,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if transport is None and (blocker := _ci_or_test(environ)) is not None:
            raise LiveMutationTransportRefused(
                f"the live Coupang recovery transport never exists under {blocker}"
            )
        self._context = context
        self._bundle = bundle
        self._clock = clock
        self._clock_preflight = clock_preflight
        self._journal = journal
        self._transport = transport
        self._authenticator = Authenticator(clock)
        self._used = False

    def _recovery_preflight(self, now: datetime) -> tuple[dict[str, object], TestProductPlan]:
        credentials = self._context.credentials
        proof = self._context.provider_read_proof
        if self._context.market is not Market.KR:
            raise CoupangLiveMutationError(ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_KR_ONLY")
        if (
            proof is None
            or proof.endpoint_id != PROVIDER_READ_PROOF_ID
            or proof.credential_generation != credentials.credential_generation
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_CANARY_PROOF_MISSING"
            )
        proof_age = now - proof.verified_at
        if proof_age < timedelta(0) or proof_age > PROVIDER_READ_PROOF_TTL:
            raise CoupangLiveMutationError(ErrorClass.POLICY_BLOCKED, "COUPANG_CANARY_PROOF_STALE")
        age = now - self._clock_preflight.checked_at
        if age < timedelta(0) or age > CLOCK_PREFLIGHT_TTL:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_CLOCK_PREFLIGHT_STALE"
            )
        if abs(self._clock_preflight.observed_skew_seconds) > MAX_CLOCK_SKEW_SECONDS:
            raise CoupangLiveMutationError(ErrorClass.POLICY_BLOCKED, "COUPANG_CLOCK_SKEW_UNSAFE")
        state = self._journal.load()
        if state is None:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_RECOVERY_JOURNAL_MISSING"
            )
        if (
            state.get("version") != 1
            or state.get("scope_id") != SCOPE_ID
            or state.get("payload_digest") != self._bundle.payload_digest
            or state.get("credential_generation") != credentials.credential_generation
            or not isinstance(state.get("records"), dict)
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_BINDING_MISMATCH"
            )
        records = state["records"]
        assert isinstance(records, dict)
        expected_ids = {plan.binding.synthetic_id for plan in self._bundle.plans}
        if set(records) != expected_ids or not all(
            isinstance(record, dict) for record in records.values()
        ):
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED,
                "COUPANG_MUTATION_JOURNAL_SCOPE_MISMATCH",
            )
        inflight = [
            synthetic_id
            for synthetic_id, record in records.items()
            if isinstance(record, dict) and record.get("state") == "create_inflight"
        ]
        if len(inflight) != 1:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_RECOVERY_SCOPE_INVALID"
            )
        plan = next(
            (plan for plan in self._bundle.plans if plan.binding.synthetic_id == inflight[0]),
            None,
        )
        if plan is None:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_JOURNAL_SCOPE_MISMATCH"
            )
        return state, plan

    def recover(self) -> AmbiguousCreateRecoveryResult:
        if self._used:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_RUN_ALREADY_USED"
            )
        self._used = True
        state, plan = self._recovery_preflight(_aware_utc(self._clock.now(), "clock.now"))
        source_product_id = plan.binding.source_product_id
        summary = self._request(
            Method.GET,
            f"{_API}/external-vendor-sku-codes/{quote(source_product_id, safe='')}",
        )
        data = summary.get("data")
        if not isinstance(data, list):
            raise CoupangLiveMutationError(
                ErrorClass.UNKNOWN, "COUPANG_MUTATION_RECOVERY_RESPONSE_INVALID"
            )
        expected_name = str(plan.create_document.value()["sellerProductName"])
        vendor_id = self._context.credentials.vendor_id
        if not data:
            raise CoupangLiveMutationError(
                ErrorClass.REVIEW_REQUIRED, "COUPANG_MUTATION_RECOVERY_NO_POSITIVE_MATCH"
            )
        if len(data) != 1:
            raise CoupangLiveMutationError(
                ErrorClass.REVIEW_REQUIRED, "COUPANG_MUTATION_RECOVERY_MULTIPLE_MATCHES"
            )
        candidate = data[0]
        if (
            not isinstance(candidate, dict)
            or candidate.get("vendorId") != vendor_id
            or candidate.get("sellerProductName") != expected_name
            or not isinstance(candidate.get("sellerProductId"), int)
            or isinstance(candidate.get("sellerProductId"), bool)
            or int(candidate["sellerProductId"]) < 1
        ):
            raise CoupangLiveMutationError(
                ErrorClass.REVIEW_REQUIRED, "COUPANG_MUTATION_RECOVERY_CANDIDATE_MISMATCH"
            )
        seller_product_id = int(candidate["sellerProductId"])
        readback = self._request(Method.GET, f"{_API}/{seller_product_id}")
        remote = self._verify_identity_scope(
            readback,
            seller_product_id=seller_product_id,
            source_product_id=source_product_id,
            seller_product_name=expected_name,
        )
        if remote.get("vendorId") != vendor_id:
            raise CoupangLiveMutationError(
                ErrorClass.POLICY_BLOCKED, "COUPANG_MUTATION_RECOVERY_VENDOR_MISMATCH"
            )
        # Recovery may lift the durable ambiguity barrier only when the detailed
        # read-back is complete enough for the documented full-modification path.
        # This is validation only; no update is sent here.
        self._full_update_document(
            plan,
            seller_product_id=seller_product_id,
            readback=remote,
        )
        self._transition(
            state,
            plan.binding.synthetic_id,
            "created",
            seller_product_id=seller_product_id,
        )
        return AmbiguousCreateRecoveryResult(
            SCOPE_ID,
            self._bundle.payload_digest,
            plan.binding.test_name,
            True,
            self._context.credentials.credential_generation,
        )
