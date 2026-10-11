"""Provider-zero contracts for Coupang Customer Service endpoints.

All six official endpoints execute only against ``FakeTransport``. The module grants no HTTP,
socket, credential, provider-call, or LIVE authority and retains customer-service responses as
opaque canonical JSON.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
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

InquiryId = NewType("InquiryId", int)
AnswerId = NewType("AnswerId", int)
OrderId = NewType("OrderId", int)
VendorItemId = NewType("VendorItemId", int)

_V4 = "/v2/providers/openapi/apis/api/v4/vendors"
_V5 = "/v2/providers/openapi/apis/api/v5/vendors"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PRODUCT_ANSWER_STATUSES = frozenset({"ALL", "ANSWERED", "NOANSWER"})
_CONTACT_STATUSES = frozenset({"NONE", "ANSWER", "NO_ANSWER", "TRANSFER"})


class CoupangCustomerServiceContractError(ValueError):
    """A local request or retained fake response violates the official contract."""


class CustomerServiceEndpoint(StrEnum):
    ANSWER_PRODUCT_INQUIRY = "ANSWER_PRODUCT_INQUIRY"
    ANSWER_CONTACT_INQUIRY = "ANSWER_CONTACT_INQUIRY"
    CONFIRM_CONTACT_INQUIRY = "CONFIRM_CONTACT_INQUIRY"
    LIST_PRODUCT_INQUIRIES = "LIST_PRODUCT_INQUIRIES"
    LIST_CONTACT_INQUIRIES = "LIST_CONTACT_INQUIRIES"
    READ_CONTACT_INQUIRY = "READ_CONTACT_INQUIRY"


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
            raise CoupangCustomerServiceContractError(
                "customer-service response must be canonical JSON"
            ) from exc
        return cls(body)

    def value(self) -> dict[str, object]:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True)
class CustomerServiceResult:
    endpoint: CustomerServiceEndpoint
    envelope: FrozenEnvelope

    def data(self) -> object:
        return self.envelope.value().get("data")


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CoupangCustomerServiceContractError(f"{name} must be a positive integer")
    return value


def _text(value: object, name: str, *, minimum: int = 1, maximum: int | None = None) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) < minimum:
        raise CoupangCustomerServiceContractError(
            f"{name} must be a trimmed string with at least {minimum} characters"
        )
    if maximum is not None and len(value) > maximum:
        raise CoupangCustomerServiceContractError(f"{name} must be at most {maximum} characters")
    return value


def _day(value: str, name: str) -> date:
    if _DAY.fullmatch(value) is None:
        raise CoupangCustomerServiceContractError(f"{name} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CoupangCustomerServiceContractError(f"{name} must be a valid date") from exc


def _seven_day_window(start: str, end: str) -> tuple[str, str]:
    start_day = _day(start, "inquiryStartAt")
    end_day = _day(end, "inquiryEndAt")
    if end_day < start_day or end_day - start_day > timedelta(days=7):
        raise CoupangCustomerServiceContractError(
            "inquiry query must be ordered and include at most 7 days"
        )
    return start, end


def _query(pairs: Sequence[tuple[str, object]]) -> str:
    return urlencode([(key, str(value)) for key, value in pairs if value is not None])


class CoupangCustomerServiceClient:
    """All six Customer Service endpoints over ``FakeTransport`` only."""

    def __init__(
        self,
        authenticator: Authenticator,
        context: AuthContext,
        transport: FakeTransport,
    ) -> None:
        if context.market not in {Market.KR, Market.TW}:
            raise CoupangCustomerServiceContractError("unknown buyer market")
        self._authenticator = authenticator
        self._context = context
        self._transport = transport

    def _call(
        self,
        endpoint: CustomerServiceEndpoint,
        method: Method,
        path: str,
        *,
        query: str = "",
        document: Mapping[str, object] | None = None,
    ) -> CustomerServiceResult:
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
            raise CoupangCustomerServiceContractError(f"Coupang HTTP {response.status_code}")
        code = response.body.get("code")
        if code is not None and code not in (200, "200", "SUCCESS"):
            raise CoupangCustomerServiceContractError(
                "Coupang customer-service response was not successful"
            )
        return CustomerServiceResult(endpoint, FrozenEnvelope.from_mapping(response.body))

    def _vendor_path(self, version: str) -> str:
        base = _V4 if version == "v4" else _V5
        return f"{base}/{self._context.credentials.vendor_id}"

    def answer_product_inquiry(
        self, inquiry_id: InquiryId, *, content: str, reply_by: str
    ) -> CustomerServiceResult:
        identity = _positive(inquiry_id, "inquiryId")
        return self._call(
            CustomerServiceEndpoint.ANSWER_PRODUCT_INQUIRY,
            Method.POST,
            f"{self._vendor_path('v4')}/onlineInquiries/{identity}/replies",
            document={
                "content": _text(content, "content"),
                "vendorId": self._context.credentials.vendor_id,
                "replyBy": _text(reply_by, "replyBy"),
            },
        )

    def answer_contact_inquiry(
        self,
        inquiry_id: InquiryId,
        *,
        content: str,
        reply_by: str,
        parent_answer_id: AnswerId,
    ) -> CustomerServiceResult:
        identity = _positive(inquiry_id, "inquiryId")
        return self._call(
            CustomerServiceEndpoint.ANSWER_CONTACT_INQUIRY,
            Method.POST,
            f"{self._vendor_path('v4')}/callCenterInquiries/{identity}/replies",
            document={
                "vendorId": self._context.credentials.vendor_id,
                "inquiryId": str(identity),
                "content": _text(content, "content", minimum=2, maximum=1000),
                "replyBy": _text(reply_by, "replyBy"),
                "parentAnswerId": str(_positive(parent_answer_id, "parentAnswerId")),
            },
        )

    def confirm_contact_inquiry(
        self, inquiry_id: InquiryId, *, confirm_by: str
    ) -> CustomerServiceResult:
        identity = _positive(inquiry_id, "inquiryId")
        return self._call(
            CustomerServiceEndpoint.CONFIRM_CONTACT_INQUIRY,
            Method.POST,
            f"{self._vendor_path('v4')}/callCenterInquiries/{identity}/confirms",
            document={"confirmBy": _text(confirm_by, "confirmBy")},
        )

    def list_product_inquiries(
        self,
        start: str,
        end: str,
        *,
        answered_type: str = "ALL",
        page_num: int = 1,
        page_size: int = 10,
    ) -> CustomerServiceResult:
        start, end = _seven_day_window(start, end)
        if answered_type not in _PRODUCT_ANSWER_STATUSES:
            raise CoupangCustomerServiceContractError("answeredType is not supported")
        if not 1 <= page_size <= 50:
            raise CoupangCustomerServiceContractError("pageSize must be 1..50")
        return self._call(
            CustomerServiceEndpoint.LIST_PRODUCT_INQUIRIES,
            Method.GET,
            f"{self._vendor_path('v5')}/onlineInquiries",
            query=_query(
                (
                    ("inquiryStartAt", start),
                    ("inquiryEndAt", end),
                    ("vendorId", self._context.credentials.vendor_id),
                    ("answeredType", answered_type),
                    ("pageSize", page_size),
                    ("pageNum", _positive(page_num, "pageNum")),
                )
            ),
        )

    def list_contact_inquiries(
        self,
        *,
        partner_counseling_status: str = "NONE",
        start: str | None = None,
        end: str | None = None,
        order_id: OrderId | None = None,
        vendor_item_id: VendorItemId | None = None,
        page_num: int = 1,
        page_size: int = 10,
    ) -> CustomerServiceResult:
        if partner_counseling_status not in _CONTACT_STATUSES:
            raise CoupangCustomerServiceContractError("partnerCounselingStatus is not supported")
        if (start is None) != (end is None):
            raise CoupangCustomerServiceContractError("inquiry dates must be supplied together")
        if start is None and vendor_item_id is None:
            raise CoupangCustomerServiceContractError(
                "contact inquiry query requires dates or vendorItemId"
            )
        if start is not None and end is not None:
            start, end = _seven_day_window(start, end)
        if not 1 <= page_size <= 30:
            raise CoupangCustomerServiceContractError("pageSize must be 1..30")
        return self._call(
            CustomerServiceEndpoint.LIST_CONTACT_INQUIRIES,
            Method.GET,
            f"{self._vendor_path('v5')}/callCenterInquiries",
            query=_query(
                (
                    ("partnerCounselingStatus", partner_counseling_status),
                    ("inquiryStartAt", start),
                    ("inquiryEndAt", end),
                    ("vendorId", self._context.credentials.vendor_id),
                    ("orderId", None if order_id is None else _positive(order_id, "orderId")),
                    (
                        "vendorItemId",
                        None
                        if vendor_item_id is None
                        else _positive(vendor_item_id, "vendorItemId"),
                    ),
                    ("pageSize", page_size),
                    ("pageNum", _positive(page_num, "pageNum")),
                )
            ),
        )

    def read_contact_inquiry(self, inquiry_id: InquiryId) -> CustomerServiceResult:
        identity = _positive(inquiry_id, "inquiryId")
        return self._call(
            CustomerServiceEndpoint.READ_CONTACT_INQUIRY,
            Method.GET,
            f"{_V5}/callCenterInquiries/{identity}",
        )


__all__ = [
    "AnswerId",
    "CoupangCustomerServiceClient",
    "CoupangCustomerServiceContractError",
    "CustomerServiceEndpoint",
    "CustomerServiceResult",
    "FrozenEnvelope",
    "InquiryId",
    "OrderId",
    "VendorItemId",
]
