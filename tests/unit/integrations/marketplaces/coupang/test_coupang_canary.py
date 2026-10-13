"""The credentialed Coupang surface is one bounded KR read and nothing else."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.platform.core import egress
from app.platform.core.errors import ErrorClass
from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Credentials,
    IpAllowlistDeclaration,
    Market,
)
from integrations.marketplaces.coupang.canary import (
    CLOCK_PREFLIGHT_TTL,
    EGRESS_OWNER,
    MAX_RESPONSE_BYTES,
    ClockPreflight,
    CoupangCanaryError,
    CoupangReadCanary,
    LiveTransportRefused,
    ReadOnlyCanaryApproval,
)

ACCESS_KEY = "fixture-access-key"
SECRET_KEY = "fixture-secret-key"
VENDOR_ID = "A00000000"
SOURCE_IP = "203.0.113.10"
AT = datetime(2026, 10, 13, 2, 0, tzinfo=UTC)
PATH = (
    "/v2/providers/seller_api/apis/api/v1/marketplace/vendors/"
    f"{VENDOR_ID}/check-auto-category-agreed"
)


@dataclass
class Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _credentials(generation: int = 3) -> Credentials:
    return Credentials(ACCESS_KEY, SECRET_KEY, VENDOR_ID, generation)


def _context(*, ip: str | None = SOURCE_IP) -> AuthContext:
    declaration = (
        None
        if ip is None
        else IpAllowlistDeclaration.create(ip, credential_generation=3, recorded_at=AT)
    )
    return AuthContext(_credentials(), Market.KR, declaration)


def _approval(**changes: object) -> ReadOnlyCanaryApproval:
    values: dict[str, object] = {
        "scope_id": "owner-approval-2026-10-13",
        "vendor_id": VENDOR_ID,
        "credential_generation": 3,
        "source_ip": SOURCE_IP,
        "approved_at": AT - timedelta(minutes=1),
        "expires_at": AT + timedelta(hours=1),
    }
    values.update(changes)
    return ReadOnlyCanaryApproval.create(**values)  # type: ignore[arg-type]


def _canary(
    handler: httpx.MockTransport,
    *,
    context: AuthContext | None = None,
    approval: ReadOnlyCanaryApproval | None = None,
    clock: Clock | None = None,
    clock_preflight: ClockPreflight | None = None,
) -> CoupangReadCanary:
    return CoupangReadCanary(
        context=context or _context(),
        approval=approval or _approval(),
        clock=clock or Clock(AT),
        clock_preflight=clock_preflight or ClockPreflight.create(0, checked_at=AT),
        transport=handler,
        environ={},
    )


def test_exact_get_is_signed_sent_once_and_retains_only_the_boolean() -> None:
    requests: list[httpx.Request] = []
    grants: list[object] = []

    def provider(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        grants.append(egress._active_grant.get())
        return httpx.Response(200, json={"code": "SUCCESS", "data": True, "extra": "drop"})

    canary = _canary(httpx.MockTransport(provider))
    result = canary.run()

    assert (result.endpoint_id, result.http_status, result.auto_category_agreed) == (
        "COUPANG_AUTO_CATEGORY_AGREEMENT_CANARY",
        200,
        True,
    )
    assert result.credential_generation == 3
    (request,) = requests
    assert (request.method, str(request.url), request.content) == (
        "GET",
        f"https://api-gateway.coupang.com{PATH}",
        b"",
    )
    assert request.url.query == b""
    assert request.headers["x-requested-by"] == VENDOR_ID
    assert request.headers["x-market"] == "KR"
    assert request.headers["authorization"].startswith(
        "CEA algorithm=HmacSHA256, access-key=fixture-access-key, "
    )
    assert request.extensions["timeout"] == {
        "connect": 5.0,
        "read": 10.0,
        "write": 5.0,
        "pool": 5.0,
    }
    (grant,) = grants
    assert isinstance(grant, egress._Grant)
    assert (grant.owner, grant.hosts) == (
        EGRESS_OWNER,
        frozenset({"api-gateway.coupang.com"}),
    )
    assert egress._active_grant.get() is None

    with pytest.raises(CoupangCanaryError) as caught:
        canary.run()
    assert caught.value.code == "COUPANG_CANARY_ALREADY_USED"
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("context", "approval", "preflight", "code"),
    [
        (
            _context(ip=None),
            _approval(),
            ClockPreflight.create(0, checked_at=AT),
            "COUPANG_IP_EVIDENCE_MISSING",
        ),
        (
            _context(),
            _approval(vendor_id="A11111111"),
            ClockPreflight.create(0, checked_at=AT),
            "COUPANG_APPROVAL_BINDING_MISMATCH",
        ),
        (
            _context(),
            _approval(credential_generation=4),
            ClockPreflight.create(0, checked_at=AT),
            "COUPANG_APPROVAL_BINDING_MISMATCH",
        ),
        (
            _context(),
            _approval(source_ip="203.0.113.11"),
            ClockPreflight.create(0, checked_at=AT),
            "COUPANG_APPROVAL_BINDING_MISMATCH",
        ),
        (
            _context(),
            _approval(approved_at=AT - timedelta(hours=2), expires_at=AT - timedelta(hours=1)),
            ClockPreflight.create(0, checked_at=AT),
            "COUPANG_APPROVAL_NOT_CURRENT",
        ),
        (
            _context(),
            _approval(),
            ClockPreflight.create(5.1, checked_at=AT),
            "COUPANG_CLOCK_SKEW_UNSAFE",
        ),
        (
            _context(),
            _approval(),
            ClockPreflight.create(0, checked_at=AT - CLOCK_PREFLIGHT_TTL - timedelta(seconds=1)),
            "COUPANG_CLOCK_PREFLIGHT_STALE",
        ),
    ],
)
def test_local_preflight_refusals_make_no_request(
    context: AuthContext,
    approval: ReadOnlyCanaryApproval,
    preflight: ClockPreflight,
    code: str,
) -> None:
    requests: list[httpx.Request] = []
    canary = _canary(
        httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200)),
        context=context,
        approval=approval,
        clock_preflight=preflight,
    )

    with pytest.raises(CoupangCanaryError) as caught:
        canary.run()

    assert (caught.value.error_class, caught.value.code) == (ErrorClass.POLICY_BLOCKED, code)
    assert requests == []


def test_live_transport_never_exists_under_ci_or_pytest() -> None:
    with pytest.raises(LiveTransportRefused, match="CI"):
        CoupangReadCanary(
            context=_context(),
            approval=_approval(),
            clock=Clock(AT),
            clock_preflight=ClockPreflight.create(0, checked_at=AT),
            environ={"CI": "1"},
        )


def test_vendor_identity_cannot_change_the_fixed_path() -> None:
    credentials = Credentials(ACCESS_KEY, SECRET_KEY, "A000/../seller-products", 3)
    context = AuthContext(
        credentials,
        Market.KR,
        IpAllowlistDeclaration.create(SOURCE_IP, credential_generation=3, recorded_at=AT),
    )
    approval = _approval(vendor_id=credentials.vendor_id)
    requests: list[httpx.Request] = []
    canary = _canary(
        httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200)),
        context=context,
        approval=approval,
    )

    with pytest.raises(CoupangCanaryError) as caught:
        canary.run()

    assert caught.value.code == "COUPANG_VENDOR_ID_UNSAFE"
    assert requests == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_never_followed(status: int) -> None:
    requests: list[httpx.Request] = []
    canary = _canary(
        httpx.MockTransport(
            lambda request: (
                requests.append(request)
                or httpx.Response(status, headers={"Location": "https://evil.example/"})
            )
        )
    )

    with pytest.raises(CoupangCanaryError) as caught:
        canary.run()

    assert (caught.value.code, caught.value.http_status) == (
        "COUPANG_UNEXPECTED_REDIRECT",
        status,
    )
    assert len(requests) == 1


def test_response_is_bounded_and_provider_echo_never_reaches_the_error() -> None:
    huge = b"x" * (MAX_RESPONSE_BYTES + 1)
    with pytest.raises(CoupangCanaryError) as oversized:
        _canary(httpx.MockTransport(lambda request: httpx.Response(200, content=huge))).run()
    assert oversized.value.code == "COUPANG_RESPONSE_TOO_LARGE"

    echo = f"{ACCESS_KEY} {SECRET_KEY} provider-detail"
    with pytest.raises(CoupangCanaryError) as rejected:
        _canary(
            httpx.MockTransport(lambda request: httpx.Response(401, json={"message": echo}))
        ).run()
    text = f"{rejected.value!s} {rejected.value!r} {rejected.value.details!r}"
    assert rejected.value.code == "COUPANG_AUTH_REJECTED"
    assert ACCESS_KEY not in text and SECRET_KEY not in text and echo not in text


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b'{"code":"SUCCESS"}',
        b'{"code":"SUCCESS","data":"true"}',
        b'{"code":"OTHER","data":true}',
    ],
)
def test_malformed_success_fails_closed(body: bytes) -> None:
    with pytest.raises(CoupangCanaryError) as caught:
        _canary(httpx.MockTransport(lambda request: httpx.Response(200, content=body))).run()
    assert caught.value.code == "COUPANG_SUCCESS_PREDICATE_FAILED"


def test_approval_is_kr_only_and_bounded_to_24_hours() -> None:
    with pytest.raises(ValueError, match="KR-only"):
        _approval(market=Market.TW)
    with pytest.raises(ValueError, match="at most 24 hours"):
        _approval(expires_at=AT + timedelta(days=2))
