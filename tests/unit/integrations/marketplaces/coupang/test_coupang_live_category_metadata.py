"""Bounded real-transport owner for exact Coupang category metadata reads."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Credentials,
    IpAllowlistDeclaration,
    Market,
)
from integrations.marketplaces.coupang.canary import ClockPreflight
from integrations.marketplaces.coupang.live_category_metadata import (
    CoupangCategoryMetadataPreflight,
    LiveCategoryMetadataTransportRefused,
)

NOW = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Clock:
    instant: datetime = NOW

    def now(self) -> datetime:
        return self.instant


def _context() -> AuthContext:
    return AuthContext(
        Credentials("access", "secret", "A0001", 1),
        Market.KR,
        IpAllowlistDeclaration.create("203.0.113.10", credential_generation=1, recorded_at=NOW),
    )


def _owner(
    transport: httpx.BaseTransport, codes: tuple[str, ...] = ("100", "200")
) -> CoupangCategoryMetadataPreflight:
    return CoupangCategoryMetadataPreflight(
        context=_context(),
        category_codes=codes,
        clock=Clock(),
        clock_preflight=ClockPreflight.create(0.1, checked_at=NOW),
        transport=transport,
        environ={},
    )


def _metadata() -> dict[str, object]:
    return {
        "code": "SUCCESS",
        "data": {
            "isAllowSingleItem": True,
            "attributes": [],
            "noticeCategories": [],
            "requiredDocumentNames": [],
            "certifications": [],
            "allowedOfferConditions": ["NEW"],
        },
    }


def test_each_exact_category_is_read_once_and_parsed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_metadata())

    result = _owner(httpx.MockTransport(handler)).run()

    assert [attempt.category_code for attempt in result.attempts] == ["100", "200"]
    assert all(attempt.succeeded for attempt in result.attempts)
    assert all(attempt.metadata is not None for attempt in result.attempts)
    assert len(requests) == 2
    assert all(request.method == "GET" and not request.content for request in requests)


def test_one_failure_does_not_stop_or_retry_later_categories() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/100"):
            return httpx.Response(429, json={"code": "ERROR", "message": "rate limited"})
        return httpx.Response(200, json=_metadata())

    result = _owner(httpx.MockTransport(handler)).run()

    assert len(requests) == 2
    assert result.attempts[0].succeeded is False
    assert result.attempts[0].http_status == 429
    assert result.attempts[1].succeeded is True


def test_each_attempt_is_reported_immediately_in_order() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/100"):
            return httpx.Response(429, json={"code": "ERROR", "message": "rate limited"})
        return httpx.Response(200, json=_metadata())

    result = _owner(httpx.MockTransport(handler)).run(
        on_attempt=lambda attempt: seen.append(attempt.category_code)
    )

    assert seen == ["100", "200"]
    assert [attempt.category_code for attempt in result.attempts] == seen


@pytest.mark.parametrize(
    "codes",
    [(), ("100", "100"), ("unsafe",), tuple(str(index) for index in range(9))],
)
def test_category_set_is_closed_and_bounded(codes: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        _owner(httpx.MockTransport(lambda _: httpx.Response(200)), codes)


def test_real_transport_is_refused_under_test_process() -> None:
    with pytest.raises(LiveCategoryMetadataTransportRefused):
        CoupangCategoryMetadataPreflight(
            context=_context(),
            category_codes=("100",),
            clock=Clock(),
            clock_preflight=ClockPreflight.create(0.1, checked_at=NOW),
        )


def test_stale_clock_preflight_refuses_before_transport() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_metadata())

    owner = CoupangCategoryMetadataPreflight(
        context=_context(),
        category_codes=("100",),
        clock=Clock(),
        clock_preflight=ClockPreflight.create(0.1, checked_at=NOW - timedelta(minutes=11)),
        transport=httpx.MockTransport(handler),
        environ={},
    )

    with pytest.raises(ValueError, match="COUPANG_CLOCK_PREFLIGHT_STALE"):
        owner.run()
    assert called is False
