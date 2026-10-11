"""Provider-zero read contracts for Coupang Settlement endpoints.

Both official KR-only endpoints execute only through ``FakeTransport``. Financial responses remain
opaque canonical JSON, and this module grants no HTTP, socket, credential, provider-call, or LIVE
authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from urllib.parse import urlencode

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Market,
    Method,
    RequestTarget,
)
from integrations.marketplaces.coupang.fake_transport import FakeRequest, FakeTransport

_REVENUE_HISTORY = "/v2/providers/openapi/apis/api/v1/revenue-history"
_SETTLEMENT_HISTORIES = "/v2/providers/marketplace_openapi/apis/api/v1/settlement-histories"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")


class CoupangSettlementContractError(ValueError):
    """A local request or retained fake response violates the official contract."""


class SettlementEndpoint(StrEnum):
    LIST_SALES = "LIST_SALES"
    LIST_SETTLEMENTS = "LIST_SETTLEMENTS"


@dataclass(frozen=True)
class FrozenEnvelope:
    body: bytes = field(repr=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FrozenEnvelope:
        try:
            body = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError) as exc:
            raise CoupangSettlementContractError(
                "settlement response must be canonical JSON"
            ) from exc
        return cls(body)

    def value(self) -> dict[str, object]:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True)
class SettlementResult:
    endpoint: SettlementEndpoint
    envelope: FrozenEnvelope
    next_token: str | None = None

    def data(self) -> object:
        return self.envelope.value().get("data")


def _day(value: str, name: str) -> date:
    if _DAY.fullmatch(value) is None:
        raise CoupangSettlementContractError(f"{name} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CoupangSettlementContractError(f"{name} must be a valid date") from exc


def _month(value: str) -> str:
    if _MONTH.fullmatch(value) is None:
        raise CoupangSettlementContractError("revenueRecognitionYearMonth must use YYYY-MM")
    try:
        date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise CoupangSettlementContractError(
            "revenueRecognitionYearMonth must be a valid month"
        ) from exc
    return value


def _query(pairs: Sequence[tuple[str, object]]) -> str:
    return urlencode([(key, str(value)) for key, value in pairs])


class CoupangSettlementClient:
    """Both official Settlement reads over ``FakeTransport`` only."""

    def __init__(
        self,
        authenticator: Authenticator,
        context: AuthContext,
        transport: FakeTransport,
    ) -> None:
        if context.market is not Market.KR:
            raise CoupangSettlementContractError("Settlement endpoints are documented for KR only")
        self._authenticator = authenticator
        self._context = context
        self._transport = transport

    def _call(self, endpoint: SettlementEndpoint, path: str, *, query: str) -> SettlementResult:
        target = RequestTarget(Method.GET, path, query)
        response = self._transport.send(
            FakeRequest(target, self._authenticator.headers(self._context, target), b"")
        )
        if response.status_code != 200 or not isinstance(response.body, dict):
            raise CoupangSettlementContractError(f"Coupang HTTP {response.status_code}")
        code = response.body.get("code")
        if code is not None and code not in (200, "200", "SUCCESS"):
            raise CoupangSettlementContractError("Coupang settlement response was not successful")
        token = response.body.get("nextToken")
        if token is not None and not isinstance(token, str | int):
            raise CoupangSettlementContractError("nextToken must be a string or integer")
        return SettlementResult(
            endpoint,
            FrozenEnvelope.from_mapping(response.body),
            None if token is None else str(token),
        )

    def list_sales(
        self,
        recognition_date_from: str,
        recognition_date_to: str,
        *,
        token: str = "",
        max_per_page: int = 50,
    ) -> SettlementResult:
        start = _day(recognition_date_from, "recognitionDateFrom")
        end = _day(recognition_date_to, "recognitionDateTo")
        if end < start or end - start > timedelta(days=30):
            raise CoupangSettlementContractError(
                "sales recognition query must be ordered and include at most 31 days"
            )
        if not isinstance(token, str) or token != token.strip():
            raise CoupangSettlementContractError("token must be a trimmed string")
        if not isinstance(max_per_page, int) or isinstance(max_per_page, bool):
            raise CoupangSettlementContractError("maxPerPage must be an integer")
        if not 1 <= max_per_page <= 50:
            raise CoupangSettlementContractError("maxPerPage must be 1..50")
        return self._call(
            SettlementEndpoint.LIST_SALES,
            _REVENUE_HISTORY,
            query=_query(
                (
                    ("vendorId", self._context.credentials.vendor_id),
                    ("recognitionDateFrom", recognition_date_from),
                    ("recognitionDateTo", recognition_date_to),
                    ("token", token),
                    ("maxPerPage", max_per_page),
                )
            ),
        )

    def list_settlements(self, revenue_recognition_year_month: str) -> SettlementResult:
        return self._call(
            SettlementEndpoint.LIST_SETTLEMENTS,
            _SETTLEMENT_HISTORIES,
            query=_query(
                (("revenueRecognitionYearMonth", _month(revenue_recognition_year_month)),)
            ),
        )


__all__ = [
    "CoupangSettlementClient",
    "CoupangSettlementContractError",
    "FrozenEnvelope",
    "SettlementEndpoint",
    "SettlementResult",
]
