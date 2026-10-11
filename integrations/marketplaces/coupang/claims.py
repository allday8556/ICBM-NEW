"""Provider-zero contracts for Coupang's Returns & Exchanges families.

The eleven adopted endpoints are prepared, signed, and executed only through the in-memory
``FakeTransport``. Nothing in this module grants HTTP, socket, credential, provider-call, or LIVE
authority. Provider response envelopes are retained without interpreting customer or claim data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import NewType
from urllib.parse import urlencode

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Market,
    Method,
    RequestTarget,
)
from integrations.marketplaces.coupang.fake_transport import FakeRequest, FakeTransport

ReceiptId = NewType("ReceiptId", int)
ExchangeId = NewType("ExchangeId", int)
OrderId = NewType("OrderId", int)

_V4 = "/v2/providers/openapi/apis/api/v4/vendors"
_V6 = "/v2/providers/openapi/apis/api/v6/vendors"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MINUTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
_SECOND = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_RETURN_STATUSES = frozenset({"RU", "UC", "CC", "PR"})
_CANCEL_TYPES = frozenset({"RETURN", "CANCEL"})
_EXCHANGE_STATUSES = frozenset({"RECEIPT", "PROGRESS", "SUCCESS", "REJECT", "CANCEL"})
_REJECT_CODES = frozenset({"SOLDOUT", "WITHDRAW"})
_DELIVERY_TYPES = frozenset({"RETURN", "EXCHANGE"})
_SHARED_MARKETS = frozenset({Market.KR, Market.TW})


class CoupangClaimContractError(ValueError):
    """A local request or retained fake response violates the official contract."""


class ClaimEndpoint(StrEnum):
    APPROVE_RETURN = "APPROVE_RETURN"
    READ_RETURN = "READ_RETURN"
    LIST_RETURN_WITHDRAWALS = "LIST_RETURN_WITHDRAWALS"
    READ_RETURN_WITHDRAWALS = "READ_RETURN_WITHDRAWALS"
    LIST_RETURNS = "LIST_RETURNS"
    CONFIRM_RETURN_RECEIPT = "CONFIRM_RETURN_RECEIPT"
    UPLOAD_PICKUP_WAYBILL = "UPLOAD_PICKUP_WAYBILL"
    CONFIRM_EXCHANGE_RECEIPT = "CONFIRM_EXCHANGE_RECEIPT"
    LIST_EXCHANGES = "LIST_EXCHANGES"
    REJECT_EXCHANGE = "REJECT_EXCHANGE"
    UPLOAD_EXCHANGE_WAYBILL = "UPLOAD_EXCHANGE_WAYBILL"


@dataclass(frozen=True)
class FrozenEnvelope:
    """Canonical response JSON retained without exposing claim/customer fields in repr."""

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
            raise CoupangClaimContractError("claim response must be canonical JSON") from exc
        if not isinstance(json.loads(body), dict):
            raise CoupangClaimContractError("claim response must be an object")
        return cls(body)

    def value(self) -> dict[str, object]:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True)
class ClaimResult:
    endpoint: ClaimEndpoint
    code: str
    message: str
    envelope: FrozenEnvelope
    next_token: str | None = None

    def data(self) -> object:
        return self.envelope.value().get("data")


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CoupangClaimContractError(f"{name} must be a positive integer")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CoupangClaimContractError(f"{name} must be a non-empty trimmed string")
    return value


def _temporal(value: str, name: str, pattern: re.Pattern[str], format_name: str) -> datetime:
    if pattern.fullmatch(value) is None:
        raise CoupangClaimContractError(f"{name} must use {format_name}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise CoupangClaimContractError(f"{name} must be a valid {format_name}") from exc


def _window(start: datetime, end: datetime, *, days: int, name: str) -> None:
    if end < start or end - start > timedelta(days=days):
        raise CoupangClaimContractError(f"{name} must be ordered and at most {days} days")


def _query(pairs: Sequence[tuple[str, object]]) -> str:
    return urlencode([(key, str(value)) for key, value in pairs if value is not None], safe=":")


class CoupangClaimClient:
    """All eleven adopted Returns & Exchanges endpoints over ``FakeTransport`` only."""

    def __init__(
        self,
        authenticator: Authenticator,
        context: AuthContext,
        transport: FakeTransport,
    ) -> None:
        if context.market not in _SHARED_MARKETS:
            raise CoupangClaimContractError("unknown buyer market")
        self._authenticator = authenticator
        self._context = context
        self._transport = transport

    def _call(
        self,
        endpoint: ClaimEndpoint,
        method: Method,
        path: str,
        *,
        query: str = "",
        document: Mapping[str, object] | None = None,
    ) -> ClaimResult:
        target = RequestTarget(method, path, query)
        body = (
            b""
            if document is None
            else json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        response = self._transport.send(
            FakeRequest(target, self._authenticator.headers(self._context, target), body)
        )
        if response.status_code != 200 or not isinstance(response.body, dict):
            raise CoupangClaimContractError(f"Coupang HTTP {response.status_code}")
        code = response.body.get("code")
        if code not in (200, "200", "SUCCESS"):
            raise CoupangClaimContractError("Coupang claim response was not successful")
        message = response.body.get("message", "")
        if not isinstance(message, str):
            raise CoupangClaimContractError("Coupang claim response message is invalid")
        token = response.body.get("nextToken")
        if token is None and isinstance(response.body.get("data"), dict):
            token = response.body["data"].get("nextToken")
        if token is not None and not isinstance(token, str | int):
            raise CoupangClaimContractError("nextToken must be a string or integer")
        return ClaimResult(
            endpoint,
            str(code),
            message,
            FrozenEnvelope.from_mapping(response.body),
            None if token is None else str(token),
        )

    def _path(self, version: str) -> str:
        base = _V4 if version == "v4" else _V6
        return f"{base}/{self._context.credentials.vendor_id}"

    def _kr_only(self) -> None:
        if self._context.market is not Market.KR:
            raise CoupangClaimContractError(
                "this Returns & Exchanges endpoint is documented for KR only"
            )

    def approve_return(self, receipt_id: ReceiptId, *, cancel_count: int) -> ClaimResult:
        receipt = _positive(receipt_id, "receiptId")
        return self._call(
            ClaimEndpoint.APPROVE_RETURN,
            Method.PATCH,
            f"{self._path('v4')}/returnRequests/{receipt}/approval",
            document={
                "vendorId": self._context.credentials.vendor_id,
                "receiptId": receipt,
                "cancelCount": _positive(cancel_count, "cancelCount"),
            },
        )

    def read_return(self, receipt_id: ReceiptId) -> ClaimResult:
        receipt = _positive(receipt_id, "receiptId")
        return self._call(
            ClaimEndpoint.READ_RETURN,
            Method.GET,
            f"{self._path('v6')}/returnRequests/{receipt}",
        )

    def list_return_withdrawals(
        self,
        date_from: str,
        date_to: str,
        *,
        page_index: int = 1,
        size_per_page: int = 10,
    ) -> ClaimResult:
        self._kr_only()
        start = _temporal(date_from, "dateFrom", _DAY, "YYYY-MM-DD")
        end = _temporal(date_to, "dateTo", _DAY, "YYYY-MM-DD")
        if end < start or (end.date() - start.date()).days > 6:
            raise CoupangClaimContractError(
                "withdrawal date range must be ordered and include at most 7 days"
            )
        if not 1 <= size_per_page <= 100:
            raise CoupangClaimContractError("sizePerPage must be 1..100")
        return self._call(
            ClaimEndpoint.LIST_RETURN_WITHDRAWALS,
            Method.GET,
            f"{self._path('v4')}/returnWithdrawRequests",
            query=_query(
                [
                    ("sizePerPage", size_per_page),
                    ("pageIndex", _positive(page_index, "pageIndex")),
                    ("dateFrom", date_from),
                    ("dateTo", date_to),
                ]
            ),
        )

    def read_return_withdrawals(self, receipt_ids: Sequence[ReceiptId]) -> ClaimResult:
        self._kr_only()
        if not 1 <= len(receipt_ids) <= 50:
            raise CoupangClaimContractError("cancelIds must contain 1..50 receipt identities")
        identities = [_positive(value, "cancelId") for value in receipt_ids]
        if len(set(identities)) != len(identities):
            raise CoupangClaimContractError("cancelIds must be unique")
        return self._call(
            ClaimEndpoint.READ_RETURN_WITHDRAWALS,
            Method.POST,
            f"{self._path('v4')}/returnWithdrawList",
            document={"cancelIds": identities},
        )

    def list_returns(
        self,
        created_from: str,
        created_to: str,
        *,
        search_type: str = "timeFrame",
        status: str | None = "UC",
        cancel_type: str = "RETURN",
        next_token: str | None = None,
        max_per_page: int | None = None,
        order_id: OrderId | None = None,
    ) -> ClaimResult:
        if search_type not in {"timeFrame", "day"}:
            raise CoupangClaimContractError("searchType must be timeFrame or day")
        pattern = _MINUTE if search_type == "timeFrame" else _DAY
        format_name = "YYYY-MM-DDTHH:MM" if search_type == "timeFrame" else "YYYY-MM-DD"
        start = _temporal(created_from, "createdAtFrom", pattern, format_name)
        end = _temporal(created_to, "createdAtTo", pattern, format_name)
        _window(start, end, days=31, name="return query")
        if cancel_type not in _CANCEL_TYPES:
            raise CoupangClaimContractError("cancelType must be RETURN or CANCEL")
        if status is not None and status not in _RETURN_STATUSES:
            raise CoupangClaimContractError("status is not an official return status")
        if cancel_type == "CANCEL" and (status is not None or order_id is not None):
            raise CoupangClaimContractError("CANCEL queries exclude status and orderId")
        if cancel_type == "RETURN" and status is None and order_id is None:
            raise CoupangClaimContractError("orderId is required when return status is omitted")
        if search_type == "timeFrame" and any(
            value is not None for value in (next_token, max_per_page, order_id)
        ):
            raise CoupangClaimContractError(
                "timeFrame queries exclude nextToken, maxPerPage, and orderId"
            )
        if next_token is not None:
            _text(next_token, "nextToken")
        if max_per_page is not None:
            _positive(max_per_page, "maxPerPage")
        identity = None if order_id is None else _positive(order_id, "orderId")
        return self._call(
            ClaimEndpoint.LIST_RETURNS,
            Method.GET,
            f"{self._path('v6')}/returnRequests",
            query=_query(
                [
                    ("searchType", "timeFrame" if search_type == "timeFrame" else None),
                    ("createdAtFrom", created_from),
                    ("createdAtTo", created_to),
                    ("status", status),
                    ("cancelType", cancel_type),
                    ("nextToken", next_token),
                    ("maxPerPage", max_per_page),
                    ("orderId", identity),
                ]
            ),
        )

    def confirm_return_receipt(self, receipt_id: ReceiptId) -> ClaimResult:
        receipt = _positive(receipt_id, "receiptId")
        return self._call(
            ClaimEndpoint.CONFIRM_RETURN_RECEIPT,
            Method.PATCH,
            f"{self._path('v4')}/returnRequests/{receipt}/receiveConfirmation",
            document={"vendorId": self._context.credentials.vendor_id, "receiptId": receipt},
        )

    def upload_pickup_waybill(
        self,
        delivery_type: str,
        receipt_id: ReceiptId,
        *,
        delivery_company_code: str,
        invoice_number: str,
        reg_number: str | None = None,
    ) -> ClaimResult:
        self._kr_only()
        if delivery_type not in _DELIVERY_TYPES:
            raise CoupangClaimContractError("returnExchangeDeliveryType must be RETURN or EXCHANGE")
        document = {
            "returnExchangeDeliveryType": delivery_type,
            "receiptId": _positive(receipt_id, "receiptId"),
            "deliveryCompanyCode": _text(delivery_company_code, "deliveryCompanyCode"),
            "invoiceNumber": _text(invoice_number, "invoiceNumber"),
        }
        if reg_number is not None:
            document["regNumber"] = _text(reg_number, "regNumber")
        return self._call(
            ClaimEndpoint.UPLOAD_PICKUP_WAYBILL,
            Method.POST,
            f"{self._path('v4')}/return-exchange-invoices/manual",
            document=document,
        )

    def confirm_exchange_receipt(self, exchange_id: ExchangeId) -> ClaimResult:
        identity = _positive(exchange_id, "exchangeId")
        return self._call(
            ClaimEndpoint.CONFIRM_EXCHANGE_RECEIPT,
            Method.PATCH,
            f"{self._path('v4')}/exchangeRequests/{identity}/receiveConfirmation",
            document={"exchangeId": identity, "vendorId": self._context.credentials.vendor_id},
        )

    def list_exchanges(
        self,
        created_from: str,
        created_to: str,
        *,
        status: str | None = None,
        order_id: OrderId | None = None,
        next_token: str | None = None,
        max_per_page: int = 10,
    ) -> ClaimResult:
        start = _temporal(created_from, "createdAtFrom", _SECOND, "YYYY-MM-DDTHH:MM:SS")
        end = _temporal(created_to, "createdAtTo", _SECOND, "YYYY-MM-DDTHH:MM:SS")
        _window(start, end, days=7, name="exchange query")
        if status is not None and status not in _EXCHANGE_STATUSES:
            raise CoupangClaimContractError("status is not an official exchange status")
        if next_token is not None:
            _text(next_token, "nextToken")
        identity = None if order_id is None else _positive(order_id, "orderId")
        return self._call(
            ClaimEndpoint.LIST_EXCHANGES,
            Method.GET,
            f"{self._path('v4')}/exchangeRequests",
            query=_query(
                [
                    ("createdAtFrom", created_from),
                    ("createdAtTo", created_to),
                    ("status", status),
                    ("orderId", identity),
                    ("nextToken", next_token),
                    ("maxPerPage", _positive(max_per_page, "maxPerPage")),
                ]
            ),
        )

    def reject_exchange(self, exchange_id: ExchangeId, *, reason: str) -> ClaimResult:
        identity = _positive(exchange_id, "exchangeId")
        if reason not in _REJECT_CODES:
            raise CoupangClaimContractError("exchangeRejectCode must be SOLDOUT or WITHDRAW")
        return self._call(
            ClaimEndpoint.REJECT_EXCHANGE,
            Method.PATCH,
            f"{self._path('v4')}/exchangeRequests/{identity}/rejection",
            document={
                "exchangeId": str(identity),
                "exchangeRejectCode": reason,
                "vendorId": self._context.credentials.vendor_id,
            },
        )

    def upload_exchange_waybill(self, exchange_id: ExchangeId) -> ClaimResult:
        self._kr_only()
        identity = _positive(exchange_id, "exchangeId")
        return self._call(
            ClaimEndpoint.UPLOAD_EXCHANGE_WAYBILL,
            Method.POST,
            f"{self._path('v4')}/exchangeRequests/{identity}/invoices",
        )


__all__ = [
    "ClaimEndpoint",
    "ClaimResult",
    "CoupangClaimClient",
    "CoupangClaimContractError",
    "ExchangeId",
    "FrozenEnvelope",
    "OrderId",
    "ReceiptId",
]
