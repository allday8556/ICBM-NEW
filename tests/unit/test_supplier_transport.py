"""Common supplier transport: pacing, the session payload and HTTP protected reads (no network)."""

import json
import logging
from dataclasses import replace

import httpx
import pytest

from app.core.errors import RateLimitedError, TransientError
from integrations.suppliers.base import RequestKind, RequestPolicy
from integrations.suppliers.transport.gateway import PolicedSupplierGateway
from integrations.suppliers.transport.pacing import RequestPacer
from integrations.suppliers.transport.session_payload import decode_session, encode_session
from tests.suppliers import fake_definition

COOKIE_VALUE = "cookie-value-that-must-never-be-logged"


def test_pacer_spaces_request_starts_per_supplier() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    pacer = RequestPacer(monotonic=lambda: now[0], sleep=sleep)
    profile = replace(
        fake_definition().profile, request_policy=RequestPolicy(minimum_request_interval_s=2.0)
    )
    with pacer.slot(profile):
        pass
    now[0] += 0.5
    with pacer.slot(profile):
        pass
    assert slept == [1.5]


def test_session_payload_keeps_only_the_suppliers_own_cookies() -> None:
    payload = encode_session(
        [
            {"name": "ECSESSID", "value": "a", "domain": "kmretail.co.kr", "path": "/"},
            {"name": "BID", "value": "b", "domain": ".kmretail.co.kr", "path": "/"},
            {"name": "_ga", "value": "c", "domain": ".google.com", "path": "/"},
        ],
        user_agent="UA/1",
        hosts={"kmretail.co.kr", "login2.cafe24ssl.com"},
    )
    cookies, user_agent = decode_session(payload)
    assert [c["name"] for c in cookies] == ["ECSESSID", "BID"]
    assert user_agent == "UA/1"


@pytest.mark.parametrize("payload", [b"{}", b'{"v": 2}', b"not json", b'{"v":1,"cookies":{}}'])
def test_session_payload_rejects_anything_else(payload: bytes) -> None:
    with pytest.raises(ValueError):
        decode_session(payload)


def _gateway(handler: httpx.MockTransport) -> PolicedSupplierGateway:
    return PolicedSupplierGateway(browser_channel="msedge", http_transport=handler)


def test_protected_read_replays_the_session_and_the_control_read_does_not() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, text="<html>page</html>")

    gateway = _gateway(httpx.MockTransport(handler))
    session = encode_session(
        [{"name": "SID", "value": COOKIE_VALUE, "domain": "supplier.test", "path": "/"}],
        user_agent="UA/1",
        hosts={"supplier.test"},
    )
    definition = fake_definition()
    control = gateway.fetch(definition, kind=RequestKind.CONTROL_READ, session=session)
    protected = gateway.fetch(definition, kind=RequestKind.PROTECTED_READ, session=session)
    assert seen == [None, f"SID={COOKIE_VALUE}"]
    assert (control.status, protected.path, protected.body) == (200, "/member", "<html>page</html>")


def test_redirect_location_is_reduced_to_its_path() -> None:
    gateway = _gateway(
        httpx.MockTransport(
            lambda r: httpx.Response(
                302, headers={"location": "https://supplier.test/login?returnUrl=/member&x=1"}
            )
        )
    )
    response = gateway.fetch(fake_definition(), kind=RequestKind.CONTROL_READ, session=None)
    assert (response.status, response.location) == (302, "/login")


@pytest.mark.parametrize(
    ("status", "error"), [(429, RateLimitedError), (500, TransientError), (503, TransientError)]
)
def test_supplier_trouble_is_classified(status: int, error: type[Exception]) -> None:
    gateway = _gateway(httpx.MockTransport(lambda r: httpx.Response(status)))
    with pytest.raises(error):
        gateway.fetch(fake_definition(), kind=RequestKind.CONTROL_READ, session=None)


def test_network_timeouts_are_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(TransientError, match="in time"):
        _gateway(httpx.MockTransport(handler)).fetch(
            fake_definition(), kind=RequestKind.CONTROL_READ, session=None
        )


def test_every_request_is_attributed_with_safe_fields_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = _gateway(httpx.MockTransport(lambda r: httpx.Response(200, text="x")))
    session = encode_session(
        [{"name": "SID", "value": COOKIE_VALUE, "domain": "supplier.test", "path": "/"}],
        user_agent="UA/1",
        hosts={"supplier.test"},
    )
    with caplog.at_level(logging.INFO, logger="icbm.connect.transport"):
        gateway.fetch(fake_definition(), kind=RequestKind.PROTECTED_READ, session=session)
    [record] = [r for r in caplog.records if r.getMessage() == "supplier.request"]
    fields = {
        k: v
        for k, v in record.__dict__.items()
        if k
        in {
            "supplier_key",
            "request_kind",
            "transport",
            "result_class",
            "http_status",
            "retry_count",
            "target",
        }
    }
    assert fields == {
        "supplier_key": "fakesupplier",
        "request_kind": "PROTECTED_READ",
        "transport": "HTTP",
        "result_class": "HTTP_200",
        "http_status": 200,
        "retry_count": 0,
        "target": "/member",
    }
    assert {"started_at", "finished_at", "latency_ms"} <= set(record.__dict__)
    assert COOKIE_VALUE not in json.dumps(record.__dict__, default=str)
