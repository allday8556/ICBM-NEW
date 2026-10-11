"""Customer Service contracts are exact, fail-closed, and fake-only."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Credentials,
    Market,
    Method,
)
from integrations.marketplaces.coupang.customer_service import (
    AnswerId,
    CoupangCustomerServiceClient,
    CoupangCustomerServiceContractError,
    CustomerServiceEndpoint,
    InquiryId,
    OrderId,
    VendorItemId,
)
from integrations.marketplaces.coupang.fake_transport import FakeResponse, FakeTransport


@dataclass(frozen=True)
class Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _client(count: int) -> tuple[CoupangCustomerServiceClient, FakeTransport]:
    transport = FakeTransport(
        tuple(FakeResponse(200, {"code": 200, "message": "OK", "data": {}}) for _ in range(count))
    )
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.KR)
    return CoupangCustomerServiceClient(Authenticator(Clock()), context, transport), transport


def test_family_closes_all_six_official_runtime_endpoints() -> None:
    assert len(CustomerServiceEndpoint) == 6


def test_reply_and_confirm_mutations_preserve_exact_paths_and_documents() -> None:
    client, transport = _client(3)

    product = client.answer_product_inquiry(
        InquiryId(846), content="재입고 예정입니다.", reply_by="wing-user"
    )
    contact = client.answer_contact_inquiry(
        InquiryId(1007837444),
        content="주문 확인 부탁드립니다.",
        reply_by="wing-user",
        parent_answer_id=AnswerId(1023208324),
    )
    confirmed = client.confirm_contact_inquiry(InquiryId(1007837444), confirm_by="wing-user")

    assert [result.endpoint for result in (product, contact, confirmed)] == [
        CustomerServiceEndpoint.ANSWER_PRODUCT_INQUIRY,
        CustomerServiceEndpoint.ANSWER_CONTACT_INQUIRY,
        CustomerServiceEndpoint.CONFIRM_CONTACT_INQUIRY,
    ]
    assert [request.target.method for request in transport.requests] == [
        Method.POST,
        Method.POST,
        Method.POST,
    ]
    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/openapi/apis/api/v4/vendors/A0001/onlineInquiries/846/replies",
        ("/v2/providers/openapi/apis/api/v4/vendors/A0001/callCenterInquiries/1007837444/replies"),
        ("/v2/providers/openapi/apis/api/v4/vendors/A0001/callCenterInquiries/1007837444/confirms"),
    ]
    assert json.loads(transport.requests[0].body) == {
        "content": "재입고 예정입니다.",
        "replyBy": "wing-user",
        "vendorId": "A0001",
    }
    assert json.loads(transport.requests[1].body) == {
        "content": "주문 확인 부탁드립니다.",
        "inquiryId": "1007837444",
        "parentAnswerId": "1023208324",
        "replyBy": "wing-user",
        "vendorId": "A0001",
    }
    assert json.loads(transport.requests[2].body) == {"confirmBy": "wing-user"}


def test_product_and_contact_queries_keep_distinct_paging_contracts() -> None:
    client, transport = _client(3)

    product = client.list_product_inquiries(
        "2026-01-01", "2026-01-07", answered_type="NOANSWER", page_num=2, page_size=50
    )
    contact = client.list_contact_inquiries(
        partner_counseling_status="NO_ANSWER",
        start="2026-01-01",
        end="2026-01-07",
        order_id=OrderId(19000000000001),
        vendor_item_id=VendorItemId(12345),
        page_num=3,
        page_size=30,
    )
    single = client.read_contact_inquiry(InquiryId(1007837444))

    assert [result.endpoint for result in (product, contact, single)] == [
        CustomerServiceEndpoint.LIST_PRODUCT_INQUIRIES,
        CustomerServiceEndpoint.LIST_CONTACT_INQUIRIES,
        CustomerServiceEndpoint.READ_CONTACT_INQUIRY,
    ]
    assert transport.requests[0].target.query == (
        "inquiryStartAt=2026-01-01&inquiryEndAt=2026-01-07&vendorId=A0001"
        "&answeredType=NOANSWER&pageSize=50&pageNum=2"
    )
    assert transport.requests[1].target.query == (
        "partnerCounselingStatus=NO_ANSWER&inquiryStartAt=2026-01-01"
        "&inquiryEndAt=2026-01-07&vendorId=A0001&orderId=19000000000001"
        "&vendorItemId=12345&pageSize=30&pageNum=3"
    )
    assert transport.requests[2].target.path == (
        "/v2/providers/openapi/apis/api/v5/vendors/callCenterInquiries/1007837444"
    )


def test_contact_query_accepts_vendor_item_without_dates() -> None:
    client, transport = _client(1)

    client.list_contact_inquiries(vendor_item_id=VendorItemId(99))

    assert transport.requests[0].target.query == (
        "partnerCounselingStatus=NONE&vendorId=A0001&vendorItemId=99&pageSize=10&pageNum=1"
    )


def test_validation_fails_before_fake_transport() -> None:
    client, transport = _client(1)

    with pytest.raises(CoupangCustomerServiceContractError, match="at most 7 days"):
        client.list_product_inquiries("2026-01-01", "2026-01-09")
    with pytest.raises(CoupangCustomerServiceContractError, match=r"1\.\.30"):
        client.list_contact_inquiries(start="2026-01-01", end="2026-01-02", page_size=31)
    with pytest.raises(CoupangCustomerServiceContractError, match="dates or vendorItemId"):
        client.list_contact_inquiries()
    with pytest.raises(CoupangCustomerServiceContractError, match="at most 1000"):
        client.answer_contact_inquiry(
            InquiryId(1),
            content="x" * 1001,
            reply_by="wing-user",
            parent_answer_id=AnswerId(2),
        )

    assert transport.requests == []


def test_fake_response_failure_is_not_projected_as_success() -> None:
    transport = FakeTransport((FakeResponse(200, {"code": "ERROR", "message": "no"}),))
    context = AuthContext(Credentials("access", "secret", "A0001", 1), Market.TW)
    client = CoupangCustomerServiceClient(Authenticator(Clock()), context, transport)

    with pytest.raises(CoupangCustomerServiceContractError, match="not successful"):
        client.read_contact_inquiry(InquiryId(1))
