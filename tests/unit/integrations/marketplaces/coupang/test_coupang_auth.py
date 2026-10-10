"""Coupang HMAC, setup and provider-zero transport contracts."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Credentials,
    IpAllowlistDeclaration,
    Market,
    Method,
    RequestTarget,
    signature,
    signed_date,
)
from integrations.marketplaces.coupang.fake_transport import (
    FakeRequest,
    FakeResponse,
    FakeTransport,
)

ACCESS_KEY = "fixture-access-key"
SECRET_KEY = "fixture-secret-key"
VENDOR_ID = "A00000000"
AT = datetime(2026, 1, 1, 1, 2, 3, tzinfo=UTC)
PATH = "/v2/providers/openapi/apis/api/v4/vendors/A00000000/returnRequests"
QUERY = "createdAtFrom=2026-01-01&createdAtTo=2026-01-01&status=UC"
EXPECTED_SIGNATURE = "47d771211fb1daaefbdfd9f0f9d8f2d67312ff7db9dbca6feb3ac9cf3c0349b5"


@dataclass
class Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _credentials(generation: int = 3) -> Credentials:
    return Credentials(ACCESS_KEY, SECRET_KEY, VENDOR_ID, generation)


def test_official_hmac_construction_is_pinned_to_the_exact_wire_query() -> None:
    target = RequestTarget(Method.GET, PATH, QUERY)

    assert signed_date(AT) == "260101T010203Z"
    assert signature(SECRET_KEY, signed_date(AT), target) == EXPECTED_SIGNATURE
    changed = RequestTarget(Method.GET, PATH, QUERY + "&x=1")
    assert signature(SECRET_KEY, signed_date(AT), changed) != EXPECTED_SIGNATURE


def test_every_request_gets_fresh_headers_and_provider_context() -> None:
    clock = Clock(AT)
    authenticator = Authenticator(clock)
    context = AuthContext(_credentials(), Market.KR)
    target = RequestTarget(Method.GET, PATH, QUERY)

    first = authenticator.headers(context, target)
    clock.value += timedelta(seconds=1)
    second = authenticator.headers(context, target)

    assert first.signed_date == "260101T010203Z"
    assert second.signed_date == "260101T010204Z"
    assert first.authorization != second.authorization
    assert first.as_http_headers() == {
        "Authorization": (
            "CEA algorithm=HmacSHA256, access-key=fixture-access-key, "
            "signed-date=260101T010203Z, signature=" + EXPECTED_SIGNATURE
        ),
        "X-Requested-By": VENDOR_ID,
        "X-MARKET": "KR",
    }


def test_credentials_and_signed_headers_do_not_render_secret_material() -> None:
    credentials = _credentials()
    headers = Authenticator(Clock(AT)).headers(
        AuthContext(credentials, Market.KR), RequestTarget(Method.GET, PATH, QUERY)
    )

    assert ACCESS_KEY not in repr(credentials)
    assert SECRET_KEY not in repr(credentials)
    assert SECRET_KEY not in repr(headers)
    assert EXPECTED_SIGNATURE not in repr(headers)


@pytest.mark.parametrize("generation", [0, -1, True, 1.5])
def test_credential_generation_is_a_strict_positive_integer(generation: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        Credentials(ACCESS_KEY, SECRET_KEY, VENDOR_ID, generation)  # type: ignore[arg-type]


def test_signing_inputs_are_runtime_validated() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        signed_date(datetime(2026, 1, 1, 1, 2, 3))
    with pytest.raises(ValueError, match="signed date"):
        signature(SECRET_KEY, "20260101T010203Z", RequestTarget(Method.GET, PATH))
    with pytest.raises(ValueError, match="officially supported"):
        AuthContext(_credentials(), "US")  # type: ignore[arg-type]


def test_ip_allowlist_declaration_is_generation_bound_and_never_provider_proof() -> None:
    declaration = IpAllowlistDeclaration.create(
        "203.0.113.10", credential_generation=3, recorded_at=AT
    )
    context = AuthContext(_credentials(), Market.KR, declaration)

    assert str(declaration.source_ip) == "203.0.113.10"
    assert context.setup_evidence_current is True
    assert AuthContext(_credentials(), Market.KR).setup_evidence_current is False
    with pytest.raises(ValueError, match="different credential generation"):
        AuthContext(_credentials(4), Market.KR, declaration)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RequestTarget(Method.GET, "https://api-gateway.coupang.com/v2/x"),
        lambda: RequestTarget(Method.GET, "/v2/x?y=1"),
        lambda: RequestTarget(Method.GET, "/v2/x", "?y=1"),
        lambda: RequestTarget(Method.GET, "/v2/x", "y=1#fragment"),
    ],
)
def test_target_validation_prevents_authority_or_query_ambiguity(
    factory: Callable[[], RequestTarget],
) -> None:
    with pytest.raises(ValueError):
        factory()


def test_c_auth_1_transport_is_an_explicit_in_memory_fake() -> None:
    target = RequestTarget(Method.GET, PATH, QUERY)
    headers = Authenticator(Clock(AT)).headers(AuthContext(_credentials(), Market.KR), target)
    transport = FakeTransport((FakeResponse(200, {"fixture": True}),))
    request = FakeRequest(target, headers)

    assert transport.send(request) == FakeResponse(200, {"fixture": True})
    assert transport.requests == [request]
    assert request.url == f"https://api-gateway.coupang.com{PATH}?{QUERY}"
    with pytest.raises(RuntimeError, match="no queued response"):
        transport.send(request)
