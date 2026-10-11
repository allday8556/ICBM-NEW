"""Provider-zero contracts for Coupang's Shipments & Orders family.

The twelve adopted endpoints are prepared, signed, and executed only through the in-memory
``FakeTransport``.  The Taiwan direct-courier pair remains out of product scope and the Korean
postal-code page remains a non-runtime reference.  Nothing in this module grants HTTP, socket,
credential, provider-call, or LIVE authority.
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

OrderId = NewType("OrderId", int)
ShipmentBoxId = NewType("ShipmentBoxId", int)
ReceiptId = NewType("ReceiptId", int)
VendorItemId = NewType("VendorItemId", int)

_V4 = "/v2/providers/openapi/apis/api/v4/vendors"
_V5 = "/v2/providers/openapi/apis/api/v5/vendors"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DAY_WITH_OFFSET = re.compile(r"^(\d{4}-\d{2}-\d{2})([+-]\d{2}:\d{2})$")
_ALLOWED_STATUSES = frozenset(
    {"ACCEPT", "INSTRUCT", "DEPARTURE", "DELIVERING", "FINAL_DELIVERY", "NONE_TRACKING"}
)
_CANCEL_REASONS = frozenset({"CCTTER", "CCPNER", "CCPRER"})

POSTAL_CODES_SOURCE_URL = (
    "https://developers.coupang.com/en/api/shipments/south-korean-cities-and-postal-codes"
)
POSTAL_CODES_RUNTIME_ADOPTED = False


class CoupangOrderContractError(ValueError):
    """A local request or retained fake response violates the official contract."""


class OrderEndpoint(StrEnum):
    CANCEL_ORDER = "CANCEL_ORDER"
    ACKNOWLEDGE = "ACKNOWLEDGE"
    COMPLETE_LONG_TERM_UNDELIVERY = "COMPLETE_LONG_TERM_UNDELIVERY"
    ALREADY_RELEASED = "ALREADY_RELEASED"
    RELEASE_STOP = "RELEASE_STOP"
    LIST_BY_MINUTE = "LIST_BY_MINUTE"
    LIST_BY_DAY = "LIST_BY_DAY"
    DELIVERY_HISTORY = "DELIVERY_HISTORY"
    READ_BY_SHIPMENT_BOX = "READ_BY_SHIPMENT_BOX"
    READ_BY_ORDER = "READ_BY_ORDER"
    UPDATE_WAYBILLS = "UPDATE_WAYBILLS"
    UPLOAD_WAYBILLS = "UPLOAD_WAYBILLS"


@dataclass(frozen=True)
class FrozenEnvelope:
    """Canonical response JSON retained without interpreting customer/order data."""

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
            raise CoupangOrderContractError("order response must be canonical JSON") from exc
        if not isinstance(json.loads(body), dict):
            raise CoupangOrderContractError("order response must be an object")
        return cls(body)

    def value(self) -> dict[str, object]:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True)
class OrderResult:
    endpoint: OrderEndpoint
    code: str
    message: str
    envelope: FrozenEnvelope
    next_token: str | None = None

    def data(self) -> object:
        return self.envelope.value().get("data")


@dataclass(frozen=True)
class Waybill:
    shipment_box_id: ShipmentBoxId
    order_id: OrderId
    vendor_item_id: VendorItemId
    delivery_company_code: str
    invoice_number: str = ""
    split_shipping: bool = False
    pre_split_shipped: bool = False
    estimated_shipping_date: str = ""

    def __post_init__(self) -> None:
        _positive(self.shipment_box_id, "shipmentBoxId")
        _positive(self.order_id, "orderId")
        _positive(self.vendor_item_id, "vendorItemId")
        _text(self.delivery_company_code, "deliveryCompanyCode")
        if self.invoice_number and not self.invoice_number.isdigit():
            raise CoupangOrderContractError("invoiceNumber must contain digits only")
        if self.estimated_shipping_date and _DATE.fullmatch(self.estimated_shipping_date) is None:
            raise CoupangOrderContractError("estimatedShippingDate must use YYYY-MM-DD")
        if self.split_shipping:
            if bool(self.invoice_number) == bool(self.estimated_shipping_date):
                raise CoupangOrderContractError(
                    "split shipping needs exactly one invoice number or estimated shipping date"
                )
        elif not self.invoice_number or self.estimated_shipping_date:
            raise CoupangOrderContractError(
                "single shipping needs an invoice number and no estimated shipping date"
            )
        if self.pre_split_shipped and not self.split_shipping:
            raise CoupangOrderContractError("preSplitShipped requires splitShipping")

    def upload_document(self) -> dict[str, object]:
        return {
            "shipmentBoxId": int(self.shipment_box_id),
            "orderId": int(self.order_id),
            "deliveryCompanyCode": self.delivery_company_code,
            "invoiceNumber": self.invoice_number,
            "vendorItemId": int(self.vendor_item_id),
            "splitShipping": self.split_shipping,
            "preSplitShipped": self.pre_split_shipped,
            "estimatedShippingDate": self.estimated_shipping_date,
        }

    def update_document(self) -> dict[str, object]:
        value = self.upload_document()
        if not self.invoice_number:
            raise CoupangOrderContractError("waybill update needs an invoice number")
        value["invoiceNumber"] = int(self.invoice_number)
        value["splitShipping"] = str(self.split_shipping).lower()
        value["preSplitShipped"] = str(self.pre_split_shipped).lower()
        return value


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CoupangOrderContractError(f"{name} must be a positive integer")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CoupangOrderContractError(f"{name} must be a non-empty trimmed string")
    return value


def _status(value: str) -> str:
    if value not in _ALLOWED_STATUSES:
        raise CoupangOrderContractError("status is not an official PO status")
    return value


def _offset(market: Market) -> timedelta:
    if market is Market.KR:
        return timedelta(hours=9)
    if market is Market.TW:
        return timedelta(hours=8)
    raise CoupangOrderContractError("unknown buyer market")


def _minute(value: str, name: str, market: Market) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CoupangOrderContractError(f"{name} must be an ISO-8601 minute") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != _offset(market) or parsed.second:
        raise CoupangOrderContractError(
            f"{name} must use the buyer-market offset and minute precision"
        )
    return parsed


def _day(value: str, name: str, market: Market) -> datetime:
    match = _DAY_WITH_OFFSET.fullmatch(value)
    if match is None:
        raise CoupangOrderContractError(f"{name} must use YYYY-MM-DD+HH:MM")
    expected = "+09:00" if market is Market.KR else "+08:00"
    if match.group(2) != expected:
        raise CoupangOrderContractError(f"{name} must use the buyer-market offset")
    return datetime.fromisoformat(value)


def _query(pairs: Sequence[tuple[str, object]]) -> str:
    return urlencode([(key, str(value)) for key, value in pairs if value is not None], safe=":")


class CoupangOrderClient:
    """All twelve adopted Shipments & Orders endpoints over ``FakeTransport`` only."""

    def __init__(
        self,
        authenticator: Authenticator,
        context: AuthContext,
        transport: FakeTransport,
    ) -> None:
        self._authenticator = authenticator
        self._context = context
        self._transport = transport

    def _call(
        self,
        endpoint: OrderEndpoint,
        method: Method,
        path: str,
        *,
        query: str = "",
        document: Mapping[str, object] | None = None,
    ) -> OrderResult:
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
            raise CoupangOrderContractError(f"Coupang HTTP {response.status_code}")
        code = response.body.get("code")
        if code not in (200, "200", "SUCCESS"):
            raise CoupangOrderContractError("Coupang order response was not successful")
        message = response.body.get("message", "")
        if not isinstance(message, str):
            raise CoupangOrderContractError("Coupang order response message is invalid")
        token = response.body.get("nextToken")
        if token is None and isinstance(response.body.get("data"), dict):
            token = response.body["data"].get("nextToken")
        if token is not None and not isinstance(token, str | int):
            raise CoupangOrderContractError("nextToken must be a string or integer")
        return OrderResult(
            endpoint,
            str(code),
            message,
            FrozenEnvelope.from_mapping(response.body),
            None if token is None else str(token),
        )

    def _path(self, version: str) -> str:
        base = _V4 if version == "v4" else _V5
        return f"{base}/{self._context.credentials.vendor_id}"

    def _kr_only(self) -> None:
        if self._context.market is not Market.KR:
            raise CoupangOrderContractError(
                "this Shipments & Orders endpoint is documented for KR only"
            )

    def cancel_order(
        self,
        order_id: OrderId,
        *,
        vendor_item_ids: Sequence[VendorItemId],
        receipt_counts: Sequence[int],
        middle_cancel_code: str,
        user_id: str,
    ) -> OrderResult:
        identity = _positive(order_id, "orderId")
        if not vendor_item_ids or len(vendor_item_ids) != len(receipt_counts):
            raise CoupangOrderContractError("vendorItemIds and receiptCounts must be paired")
        items = [_positive(value, "vendorItemId") for value in vendor_item_ids]
        counts = [_positive(value, "receiptCount") for value in receipt_counts]
        if middle_cancel_code not in _CANCEL_REASONS:
            raise CoupangOrderContractError("middleCancelCode is not an official reason")
        vendor = self._context.credentials.vendor_id
        return self._call(
            OrderEndpoint.CANCEL_ORDER,
            Method.POST,
            f"{self._path('v5')}/orders/{identity}/cancel",
            document={
                "orderId": identity,
                "vendorItemIds": items,
                "receiptCounts": counts,
                "bigCancelCode": "CANERR",
                "middleCancelCode": middle_cancel_code,
                "vendorId": vendor,
                "userId": _text(user_id, "userId"),
            },
        )

    def acknowledge(self, shipment_box_ids: Sequence[ShipmentBoxId]) -> OrderResult:
        if not 1 <= len(shipment_box_ids) <= 50:
            raise CoupangOrderContractError("shipmentBoxIds must contain 1..50 identities")
        vendor = self._context.credentials.vendor_id
        return self._call(
            OrderEndpoint.ACKNOWLEDGE,
            Method.PATCH,
            f"{self._path('v4')}/ordersheets/acknowledgement",
            document={
                "vendorId": vendor,
                "shipmentBoxIds": [_positive(value, "shipmentBoxId") for value in shipment_box_ids],
            },
        )

    def complete_long_term_undelivery(
        self, shipment_box_id: ShipmentBoxId, invoice_number: str
    ) -> OrderResult:
        self._kr_only()
        return self._call(
            OrderEndpoint.COMPLETE_LONG_TERM_UNDELIVERY,
            Method.POST,
            f"{self._path('v4')}/completeLongTermUndelivery",
            document={
                "shipmentBoxId": _positive(shipment_box_id, "shipmentBoxId"),
                "invoiceNumber": _text(invoice_number, "invoiceNumber"),
            },
        )

    def already_released(
        self,
        receipt_id: ReceiptId,
        *,
        delivery_company_code: str,
        invoice_number: str,
    ) -> OrderResult:
        self._kr_only()
        receipt = _positive(receipt_id, "receiptId")
        return self._call(
            OrderEndpoint.ALREADY_RELEASED,
            Method.PATCH,
            f"{self._path('v4')}/returnRequests/{receipt}/completedShipment",
            document={
                "vendorId": self._context.credentials.vendor_id,
                "receiptId": receipt,
                "deliveryCompanyCode": _text(delivery_company_code, "deliveryCompanyCode"),
                "invoiceNumber": _text(invoice_number, "invoiceNumber"),
            },
        )

    def release_stop(self, receipt_id: ReceiptId, *, cancel_count: int) -> OrderResult:
        self._kr_only()
        receipt = _positive(receipt_id, "receiptId")
        return self._call(
            OrderEndpoint.RELEASE_STOP,
            Method.PATCH,
            f"{self._path('v4')}/returnRequests/{receipt}/stoppedShipment",
            document={
                "vendorId": self._context.credentials.vendor_id,
                "receiptId": receipt,
                "cancelCount": _positive(cancel_count, "cancelCount"),
            },
        )

    def list_by_minute(self, created_from: str, created_to: str, *, status: str) -> OrderResult:
        start = _minute(created_from, "createdAtFrom", self._context.market)
        end = _minute(created_to, "createdAtTo", self._context.market)
        if end < start or end - start > timedelta(hours=24):
            raise CoupangOrderContractError("minute query must be ordered and at most 24 hours")
        return self._call(
            OrderEndpoint.LIST_BY_MINUTE,
            Method.GET,
            f"{self._path('v5')}/ordersheets",
            query=_query(
                [
                    ("createdAtFrom", created_from),
                    ("createdAtTo", created_to),
                    ("searchType", "timeFrame"),
                    ("status", _status(status)),
                ]
            ),
        )

    def list_by_day(
        self,
        created_from: str,
        created_to: str,
        *,
        status: str,
        next_token: str | None = None,
        max_per_page: int = 50,
    ) -> OrderResult:
        start = _day(created_from, "createdAtFrom", self._context.market)
        end = _day(created_to, "createdAtTo", self._context.market)
        if end < start or end - start > timedelta(days=31):
            raise CoupangOrderContractError("day query must be ordered and at most 31 days")
        if not 1 <= max_per_page <= 50:
            raise CoupangOrderContractError("maxPerPage must be 1..50")
        if next_token is not None:
            _text(next_token, "nextToken")
        return self._call(
            OrderEndpoint.LIST_BY_DAY,
            Method.GET,
            f"{self._path('v5')}/ordersheets",
            query=_query(
                [
                    ("createdAtFrom", created_from),
                    ("createdAtTo", created_to),
                    ("nextToken", next_token),
                    ("maxPerPage", max_per_page),
                    ("status", _status(status)),
                ]
            ),
        )

    def delivery_history(self, shipment_box_id: ShipmentBoxId) -> OrderResult:
        identity = _positive(shipment_box_id, "shipmentBoxId")
        return self._call(
            OrderEndpoint.DELIVERY_HISTORY,
            Method.GET,
            f"{self._path('v5')}/ordersheets/{identity}/history",
        )

    def by_shipment_box(self, shipment_box_id: ShipmentBoxId) -> OrderResult:
        identity = _positive(shipment_box_id, "shipmentBoxId")
        return self._call(
            OrderEndpoint.READ_BY_SHIPMENT_BOX,
            Method.GET,
            f"{self._path('v5')}/ordersheets/{identity}",
        )

    def by_order(self, order_id: OrderId) -> OrderResult:
        identity = _positive(order_id, "orderId")
        return self._call(
            OrderEndpoint.READ_BY_ORDER,
            Method.GET,
            f"{self._path('v5')}/{identity}/ordersheets",
        )

    def update_waybills(self, waybills: Sequence[Waybill]) -> OrderResult:
        self._kr_only()
        documents = self._waybills(waybills, update=True)
        return self._call(
            OrderEndpoint.UPDATE_WAYBILLS,
            Method.POST,
            f"{self._path('v4')}/orders/updateInvoices",
            document={
                "vendorId": self._context.credentials.vendor_id,
                "orderSheetInvoiceApplyDtos": documents,
            },
        )

    def upload_waybills(self, waybills: Sequence[Waybill]) -> OrderResult:
        self._kr_only()
        documents = self._waybills(waybills, update=False)
        return self._call(
            OrderEndpoint.UPLOAD_WAYBILLS,
            Method.POST,
            f"{self._path('v4')}/orders/invoices",
            document={
                "vendorId": self._context.credentials.vendor_id,
                "orderSheetInvoiceApplyDtos": documents,
            },
        )

    @staticmethod
    def _waybills(waybills: Sequence[Waybill], *, update: bool) -> list[dict[str, object]]:
        if not waybills:
            raise CoupangOrderContractError("waybill list must not be empty")
        if len({(item.shipment_box_id, item.vendor_item_id) for item in waybills}) != len(waybills):
            raise CoupangOrderContractError("waybill shipment/item identities must be unique")
        return [item.update_document() if update else item.upload_document() for item in waybills]


__all__ = [
    "POSTAL_CODES_RUNTIME_ADOPTED",
    "POSTAL_CODES_SOURCE_URL",
    "CoupangOrderClient",
    "CoupangOrderContractError",
    "FrozenEnvelope",
    "OrderEndpoint",
    "OrderId",
    "OrderResult",
    "ReceiptId",
    "ShipmentBoxId",
    "VendorItemId",
    "Waybill",
]
