"""Korea Eximbank 현재환율 API, ``AP01`` (ADR-0038 §3): one daily publication, read strictly.

This module owns exactly one request: ``GET exchangeJSON?authkey=…&searchdate=YYYYMMDD&data=AP01``
on ``oapi.koreaexim.go.kr``, inside an egress grant for that host alone. It accepts no URL, method,
host or query from a caller — only the operator's key and the day asked for — and it never
follows a redirect.

**The answer is read, never guessed** (ADR-0010). The response is a JSON array with one object per
currency. An empty array is a day without a publication (weekend, holiday, or before about 11:00
KST): :class:`Ap01NotPublished`. Anything else must be exactly right or nothing is returned but a
typed :class:`Ap01Failure`:

- every row is an object whose ``result`` is ``1`` (the API's own codes ``2`` data code error,
  ``3`` authkey error and ``4`` daily limit are named; any other is malformed). ``result`` is read
  as the number or its decimal text, both of which state the same code;
- every row names its ``cur_unit``, once;
- the three wanted rows — ``CNH`` (위안화, ADR-0038 §3), ``JPY(100)`` (per 100 yen) and ``USD``
  (for the USD 150 test, §4) — are present, and each states ``tts`` and ``deal_bas_r`` as positive
  decimal text, with or without thousands separators (``"1,385.5"``).

A rate is kept **per its publication unit**, exactly as published: ``JPY(100)``'s TTS is the won
price of 100 yen, recorded with ``unit = 100``; it is never divided here. The TTS is recorded as
exact decimal text with the separators removed; ``deal_bas_r`` and the whole row are kept as stated.

**The key never leaves this call.** It is sent only as the ``authkey`` query parameter. The URL that
carries it is never logged: the HTTP stack's own loggers are muted for the duration of the
exchange, every failure is a code (never an exception text, a URL or a response body), and nothing
here logs at all. The live transport never exists under CI or a test process: a test injects one.
"""

import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

import httpx

from app.platform.core.egress import EGRESS, EgressBlockedError

HOST: Final = "oapi.koreaexim.go.kr"
EXIM_BASE_URL: Final = f"https://{HOST}"
PATH: Final = "/site/program/financial/exchangeJSON"
DATA_CODE: Final = "AP01"
SOURCE: Final = "koreaexim/AP01"
EGRESS_OWNER: Final = "fx:koreaexim:ap01"
MAX_RESPONSE_BYTES: Final = 262_144
CI_MARKERS: Final = ("CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST")
_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})

# The published code of each wanted currency: (ICBM currency, publication unit), ADR-0038 §3.
PUBLISHED_CODES: Final[Mapping[str, tuple[str, int]]] = {
    "CNH": ("CNY", 1),
    "JPY(100)": ("JPY", 100),
    "USD": ("USD", 1),
}

# Positive decimal text as the API states it: properly grouped thousands or no grouping at all,
# at most seven integer digits and six decimals. Never a sign, an exponent or a bare point.
_DECIMAL: Final = re.compile(r"^(?:[1-9]\d{0,2}(?:,\d{3}){1,2}|0|[1-9]\d{0,6})(?:\.\d{1,6})?$")

FX_SOURCE_DATA_CODE_ERROR: Final = "FX_SOURCE_DATA_CODE_ERROR"
FX_SOURCE_AUTHKEY_INVALID: Final = "FX_SOURCE_AUTHKEY_INVALID"
FX_SOURCE_DAILY_LIMIT: Final = "FX_SOURCE_DAILY_LIMIT"
FX_SOURCE_MALFORMED: Final = "FX_SOURCE_MALFORMED"
FX_SOURCE_CURRENCY_MISSING: Final = "FX_SOURCE_CURRENCY_MISSING"
FX_SOURCE_HTTP_STATUS: Final = "FX_SOURCE_HTTP_STATUS"
FX_SOURCE_REDIRECT: Final = "FX_SOURCE_REDIRECT"
FX_SOURCE_TOO_LARGE: Final = "FX_SOURCE_TOO_LARGE"
FX_SOURCE_TRANSPORT_FAILED: Final = "FX_SOURCE_TRANSPORT_FAILED"
FX_SOURCE_EGRESS_BLOCKED: Final = "FX_SOURCE_EGRESS_BLOCKED"
FX_SOURCE_LIVE_TRANSPORT_REFUSED: Final = "FX_SOURCE_LIVE_TRANSPORT_REFUSED"
_RESULT_CODES: Final[Mapping[int, str]] = {
    2: FX_SOURCE_DATA_CODE_ERROR,
    3: FX_SOURCE_AUTHKEY_INVALID,
    4: FX_SOURCE_DAILY_LIMIT,
}

# The HTTP stack's loggers, exactly as httpx and httpcore name them. ``httpx`` logs each request's
# full URL at INFO, which here carries the key.
HTTP_STACK_LOGGERS: Final = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_IN_EXCHANGE: ContextVar[bool] = ContextVar("icbm_fx_exchange", default=False)


class _ExchangeLogGuard(logging.Filter):
    """Drops the HTTP stack's own records only while an AP01 exchange runs in this context."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _IN_EXCHANGE.get()


_GUARD: Final = _ExchangeLogGuard()


def install_log_guard() -> None:
    """Attach the exchange guard to the HTTP stack's loggers (idempotent; no level change)."""
    for name in HTTP_STACK_LOGGERS:
        logging.getLogger(name).addFilter(_GUARD)


@dataclass(frozen=True)
class Ap01Rate:
    """One wanted currency of one publication, as published.

    ``tts`` is the TTS (송금 보낼때) per ``unit`` of the currency, as exact decimal text without
    separators. ``deal_bas_r`` and ``published_json`` (the whole row, canonical JSON) are as
    stated."""

    currency: str
    source_code: str
    unit: int
    tts: str
    deal_bas_r: str
    published_json: str

    def tts_per_unit(self) -> Decimal:
        """The won price of one unit of the currency: exact, since the unit is 1 or 100."""
        return Decimal(self.tts) / self.unit


@dataclass(frozen=True)
class Ap01Publication:
    """The publication of one day: exactly the wanted currencies, in ``PUBLISHED_CODES`` order."""

    day: date
    rates: tuple[Ap01Rate, ...]


@dataclass(frozen=True)
class Ap01NotPublished:
    """The API answered an empty array for the day: nothing is published for it (yet)."""

    day: date


@dataclass(frozen=True)
class Ap01Failure:
    """A typed failure. Nothing was read; ``http_status`` is set only for an unexpected status."""

    code: str
    http_status: int | None = None


Ap01Result = Ap01Publication | Ap01NotPublished | Ap01Failure


def _result_code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value in {"1", "2", "3", "4"}:
        return int(value)
    return None


def _decimal_text(value: object) -> str | None:
    """The stated positive decimal without its separators, or ``None`` when it is not one."""
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        return None
    text = value.replace(",", "")
    return text if Decimal(text) > 0 else None


def parse_ap01(day: date, body: bytes) -> Ap01Result:
    """Read one AP01 response body strictly (module docstring). Pure: no I/O, no logging."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return Ap01Failure(FX_SOURCE_MALFORMED)
    if not isinstance(document, list):
        return Ap01Failure(FX_SOURCE_MALFORMED)
    if not document:
        return Ap01NotPublished(day)
    rows: dict[str, dict[str, object]] = {}
    for row in document:
        if not isinstance(row, dict):
            return Ap01Failure(FX_SOURCE_MALFORMED)
        code = _result_code(row.get("result"))
        if code != 1:
            named = None if code is None else _RESULT_CODES.get(code)
            return Ap01Failure(named or FX_SOURCE_MALFORMED)
        unit = row.get("cur_unit")
        if not isinstance(unit, str) or not unit or unit in rows:
            return Ap01Failure(FX_SOURCE_MALFORMED)
        rows[unit] = row
    rates: list[Ap01Rate] = []
    for source_code, (currency, per) in PUBLISHED_CODES.items():
        row = rows.get(source_code)
        if row is None:
            return Ap01Failure(FX_SOURCE_CURRENCY_MISSING)
        tts = _decimal_text(row.get("tts"))
        deal = row.get("deal_bas_r")
        if tts is None or not isinstance(deal, str) or _decimal_text(deal) is None:
            return Ap01Failure(FX_SOURCE_MALFORMED)
        published = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        rates.append(Ap01Rate(currency, source_code, per, tts, deal, published))
    return Ap01Publication(day, tuple(rates))


def _ci_or_test(environ: Mapping[str, str] | None) -> bool:
    env = os.environ if environ is None else environ
    return any(env.get(name) for name in CI_MARKERS) or "pytest" in sys.modules


def _caused_by_egress(error: BaseException) -> bool:
    seen: BaseException | None = error
    for _ in range(8):
        if seen is None:
            return False
        if isinstance(seen, EgressBlockedError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _read_bounded(response: httpx.Response) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class KoreaEximAp01:
    """The only Korea Eximbank caller: one AP01 read per :meth:`fetch`, no retry."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._environ = environ

    def fetch(self, authkey: str, day: date) -> Ap01Result:
        """Read the publication of ``day`` with the operator's key. Never raises for anything the
        network or the API does: every such outcome is a typed result."""
        if self._transport is None and _ci_or_test(self._environ):
            return Ap01Failure(FX_SOURCE_LIVE_TRANSPORT_REFUSED)
        install_log_guard()
        params = {"authkey": authkey, "searchdate": day.strftime("%Y%m%d"), "data": DATA_CODE}
        token = _IN_EXCHANGE.set(True)
        try:
            with (
                EGRESS.grant(EGRESS_OWNER, {HOST}),
                httpx.Client(
                    base_url=EXIM_BASE_URL,
                    timeout=_TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client,
                client.stream("GET", PATH, params=params) as response,
            ):
                status = response.status_code
                body = None if status != 200 else _read_bounded(response)
        except EgressBlockedError:
            return Ap01Failure(FX_SOURCE_EGRESS_BLOCKED)
        except httpx.HTTPError as error:
            # The HTTP stack wraps the guard's refusal in its own connect error.
            blocked = _caused_by_egress(error)
            return Ap01Failure(FX_SOURCE_EGRESS_BLOCKED if blocked else FX_SOURCE_TRANSPORT_FAILED)
        finally:
            _IN_EXCHANGE.reset(token)
        if status in _REDIRECTS:
            return Ap01Failure(FX_SOURCE_REDIRECT, http_status=status)
        if status != 200:
            return Ap01Failure(FX_SOURCE_HTTP_STATUS, http_status=status)
        if body is None:
            return Ap01Failure(FX_SOURCE_TOO_LARGE)
        return parse_ap01(day, body)


__all__ = [
    "SOURCE",
    "Ap01Failure",
    "Ap01NotPublished",
    "Ap01Publication",
    "Ap01Rate",
    "Ap01Result",
    "KoreaEximAp01",
    "parse_ap01",
]
