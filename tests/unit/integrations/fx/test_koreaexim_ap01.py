"""ADR-0038 §3: the Korea Eximbank AP01 reader, offline.

Every response here is invented and every exchange goes through ``httpx.MockTransport``: no
request ever reaches ``oapi.koreaexim.go.kr``.

Proven here:
- a publication is read strictly: thousands separators, ``JPY(100)`` per 100 yen, the result
  code as number or text, the three wanted currencies only, the row kept as stated;
- an empty array is a day without a publication; the API's own result codes and anything
  malformed are typed failures that carry nothing read;
- the one request names the host, path and query exactly, inside an egress grant for that host
  alone, follows no redirect, and never lets the key reach a log record or a failure;
- the live transport never exists in a test process.
"""

import json
import logging
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.platform.core import egress
from app.platform.core.egress import EgressBlockedError
from integrations.fx.koreaexim import (
    EGRESS_OWNER,
    FX_SOURCE_AUTHKEY_INVALID,
    FX_SOURCE_CURRENCY_MISSING,
    FX_SOURCE_DAILY_LIMIT,
    FX_SOURCE_DATA_CODE_ERROR,
    FX_SOURCE_EGRESS_BLOCKED,
    FX_SOURCE_HTTP_STATUS,
    FX_SOURCE_LIVE_TRANSPORT_REFUSED,
    FX_SOURCE_MALFORMED,
    FX_SOURCE_REDIRECT,
    FX_SOURCE_TOO_LARGE,
    FX_SOURCE_TRANSPORT_FAILED,
    MAX_RESPONSE_BYTES,
    Ap01Failure,
    Ap01NotPublished,
    Ap01Publication,
    KoreaEximAp01,
    parse_ap01,
)

DAY = date(2026, 10, 14)
KEY = "TestKeyAbcdefghijklmnop0123456789"


def _row(unit: str, tts: str, deal: str, *, result: Any = 1, **extra: Any) -> dict[str, Any]:
    return {
        "result": result,
        "cur_unit": unit,
        "ttb": "1",
        "tts": tts,
        "deal_bas_r": deal,
        "bkpr": "1",
        "cur_nm": f"name {unit}",
        **extra,
    }


def _rows(**override: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any] | None] = {
        "AED": _row("AED", "381.16", "377.39"),
        "CNH": _row("CNH", "195.12", "193.18"),
        "JPY(100)": _row("JPY(100)", "938.4", "929.11"),
        "KRW": _row("KRW", "0", "1"),
        "USD": _row("USD", "1,399.9", "1,386.2"),
    }
    rows.update(override)
    return [row for row in rows.values() if row is not None]


def _body(document: Any) -> bytes:
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------- the parser


def test_a_publication_is_read_per_its_unit_and_as_stated() -> None:
    read = parse_ap01(DAY, _body(_rows()))
    assert isinstance(read, Ap01Publication) and read.day == DAY
    assert [(r.currency, r.source_code, r.unit, r.tts, r.deal_bas_r) for r in read.rates] == [
        ("CNY", "CNH", 1, "195.12", "193.18"),
        ("JPY", "JPY(100)", 100, "938.4", "929.11"),
        ("USD", "USD", 1, "1399.9", "1,386.2"),
    ]
    jpy = read.rates[1]
    assert jpy.tts_per_unit() == Decimal("9.384")
    assert read.rates[2].tts_per_unit() == Decimal("1399.9")
    # The whole row is kept exactly as fetched, separators and all.
    assert json.loads(read.rates[2].published_json) == _row("USD", "1,399.9", "1,386.2")


def test_the_result_code_reads_as_number_or_text() -> None:
    rows = [{**row, "result": "1"} for row in _rows()]
    assert isinstance(parse_ap01(DAY, _body(rows)), Ap01Publication)


def test_an_empty_array_is_a_day_without_a_publication() -> None:
    assert parse_ap01(DAY, b"[]") == Ap01NotPublished(DAY)


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (2, FX_SOURCE_DATA_CODE_ERROR),
        ("3", FX_SOURCE_AUTHKEY_INVALID),
        (4, FX_SOURCE_DAILY_LIMIT),
        (5, FX_SOURCE_MALFORMED),
        (True, FX_SOURCE_MALFORMED),
        (None, FX_SOURCE_MALFORMED),
        ("ok", FX_SOURCE_MALFORMED),
    ],
)
def test_the_api_result_codes_are_named_failures(result: Any, code: str) -> None:
    # An error answer is typically one row with the code and nothing else usable.
    lone = [{"result": result, "cur_unit": None, "tts": None, "deal_bas_r": None}]
    assert parse_ap01(DAY, _body(lone)) == Ap01Failure(code)
    # A non-1 code on any row refuses the whole answer, even beside good rows.
    mixed = _rows(AED=_row("AED", "381.16", "377.39", result=result))
    assert parse_ap01(DAY, _body(mixed)) == Ap01Failure(code)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b'{"result": 1}',
        b"[1]",
        b'["CNH"]',
        b"null",
    ],
)
def test_a_body_that_is_not_an_array_of_rows_is_malformed(body: bytes) -> None:
    assert parse_ap01(DAY, body) == Ap01Failure(FX_SOURCE_MALFORMED)


@pytest.mark.parametrize(
    "tts",
    [
        "",
        "0",
        "0.00",
        "-195.12",
        "+195.12",
        "1e3",
        "195,12",
        "1,38.5",
        "13,85.5",
        "195.",
        ".5",
        "1 385.5",
        "0195.12",
        "195.1234567",
        "12345678",
        195.12,
        None,
    ],
)
def test_a_rate_that_is_not_exact_positive_decimal_text_is_malformed(tts: Any) -> None:
    rows = _rows(CNH=_row("CNH", tts, "193.18"))
    assert parse_ap01(DAY, _body(rows)) == Ap01Failure(FX_SOURCE_MALFORMED)
    deal = _rows(USD=_row("USD", "1,399.9", tts))
    assert parse_ap01(DAY, _body(deal)) == Ap01Failure(FX_SOURCE_MALFORMED)


@pytest.mark.parametrize(
    ("tts", "read"),
    [("1,385.50", "1385.50"), ("1,234,567.5", "1234567.5"), ("0.5", "0.5"), ("938", "938")],
)
def test_grouped_and_plain_decimals_are_read_exactly(tts: str, read: str) -> None:
    result = parse_ap01(DAY, _body(_rows(CNH=_row("CNH", tts, "1"))))
    assert isinstance(result, Ap01Publication) and result.rates[0].tts == read


@pytest.mark.parametrize("missing", ["CNH", "JPY(100)", "USD"])
def test_a_wanted_currency_missing_records_nothing(missing: str) -> None:
    rows = _rows(**{missing: None})
    assert parse_ap01(DAY, _body(rows)) == Ap01Failure(FX_SOURCE_CURRENCY_MISSING)


def test_a_repeated_or_unnamed_currency_is_malformed() -> None:
    repeated = [*_rows(), _row("USD", "1,400.0", "1,386.2")]
    assert parse_ap01(DAY, _body(repeated)) == Ap01Failure(FX_SOURCE_MALFORMED)
    unnamed = [*_rows(), _row("", "1", "1")]
    assert parse_ap01(DAY, _body(unnamed)) == Ap01Failure(FX_SOURCE_MALFORMED)
    # Another currency's odd row only has to be a named, successful row: it is not read.
    odd = _rows(AED={"result": 1, "cur_unit": "AED", "tts": None})
    assert isinstance(parse_ap01(DAY, _body(odd)), Ap01Publication)


# ---------------------------------------------------------------- the one request


class _Recorder:
    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []
        self.grants: list[tuple[str, frozenset[str]] | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        grant = egress._active_grant.get()
        self.grants.append(None if grant is None else (grant.owner, grant.hosts))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _reader(recorder: _Recorder) -> KoreaEximAp01:
    return KoreaEximAp01(transport=httpx.MockTransport(recorder))


def test_the_request_is_exactly_one_get_inside_a_grant_for_the_host_alone() -> None:
    recorder = _Recorder(httpx.Response(200, content=_body(_rows())))
    read = _reader(recorder).fetch(KEY, DAY)
    assert isinstance(read, Ap01Publication)
    (request,) = recorder.requests
    assert request.method == "GET"
    assert (request.url.scheme, request.url.host, request.url.path) == (
        "https",
        "oapi.koreaexim.go.kr",
        "/site/program/financial/exchangeJSON",
    )
    assert dict(request.url.params) == {"authkey": KEY, "searchdate": "20261014", "data": "AP01"}
    assert recorder.grants == [(EGRESS_OWNER, frozenset({"oapi.koreaexim.go.kr"}))]
    # The grant ends with the call.
    assert egress._active_grant.get() is None


def test_the_key_never_reaches_a_log_record_or_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    for response in (
        httpx.Response(200, content=_body(_rows())),
        httpx.Response(500, content=f"bad key {KEY}".encode()),
        httpx.ConnectError(f"cannot reach ?authkey={KEY}"),
    ):
        read = _reader(_Recorder(response)).fetch(KEY, DAY)
        assert KEY not in repr(read)
    assert all(
        KEY not in record.getMessage() and KEY not in repr(record.__dict__)
        for record in caplog.records
    )
    # Why the guard exists, and that it mutes only the exchange: outside it, the HTTP stack
    # itself logs a request's full URL, query and all.
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as plain:
        plain.get("https://example.test/", params={"authkey": KEY})
    assert any(KEY in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("response", "failure"),
    [
        (httpx.Response(500), Ap01Failure(FX_SOURCE_HTTP_STATUS, http_status=500)),
        (httpx.Response(404), Ap01Failure(FX_SOURCE_HTTP_STATUS, http_status=404)),
        (
            httpx.Response(302, headers={"Location": "https://elsewhere.example/"}),
            Ap01Failure(FX_SOURCE_REDIRECT, http_status=302),
        ),
        (httpx.ConnectError("refused"), Ap01Failure(FX_SOURCE_TRANSPORT_FAILED)),
        (httpx.ReadTimeout("slow"), Ap01Failure(FX_SOURCE_TRANSPORT_FAILED)),
        (EgressBlockedError("blocked"), Ap01Failure(FX_SOURCE_EGRESS_BLOCKED)),
        (
            httpx.Response(200, content=b"[" + b" " * MAX_RESPONSE_BYTES + b"]"),
            Ap01Failure(FX_SOURCE_TOO_LARGE),
        ),
    ],
)
def test_every_other_answer_is_a_typed_failure(
    response: httpx.Response | Exception, failure: Ap01Failure
) -> None:
    recorder = _Recorder(response)
    assert _reader(recorder).fetch(KEY, DAY) == failure
    # One request, never a followed redirect or a retry.
    assert len(recorder.requests) == 1


def test_the_live_transport_never_exists_in_a_test_process() -> None:
    assert KoreaEximAp01().fetch(KEY, DAY) == Ap01Failure(FX_SOURCE_LIVE_TRANSPORT_REFUSED)
    ci = KoreaEximAp01(environ={"CI": "true"})
    assert ci.fetch(KEY, DAY) == Ap01Failure(FX_SOURCE_LIVE_TRANSPORT_REFUSED)
