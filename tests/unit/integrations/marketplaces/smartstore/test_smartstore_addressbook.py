"""The seller's SmartStore address book over the adopted caller (owner directive 2026-10-07).

A read only: the Settings delivery policy chooses 출고지 / 반품·교환지 from it. Only each entry's
number, label and type are retained — never an address or a contact field.
"""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.platform.core.errors import PolicyBlockedError
from integrations.marketplaces.smartstore.addressbook import MAX_PAGES, SmartStoreAddressBookSource
from integrations.marketplaces.smartstore.caller import (
    AddressBookListRequest,
    AddressBookListResponse,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import EndpointId, resolve

BOOKS = [
    {
        "addressBookNo": 200441202,
        "name": "출고지 JAPAN",
        "addressType": "RELEASE",
        "baseAddress": "secret street 1",
        "phoneNumber1": "010-0000-0000",
    },
    {"addressBookNo": 200401837, "name": "반품교환지", "addressType": "REFUND_OR_EXCHANGE"},
]


class Caller:
    def __init__(self, pages: list[object]) -> None:
        self.pages = pages
        self.calls: list[tuple[object, Any]] = []

    def call(self, endpoint_id: object, request: Any) -> AddressBookListResponse:
        self.calls.append((endpoint_id, request))
        page = self.pages[min(request.page, len(self.pages)) - 1]
        return AddressBookListResponse(retained={"addressBooks": page}, http_status=200)


def _bearer() -> Any:
    return SimpleNamespace(access_token="token", credential_generation=3, session_generation=5)


def test_the_source_reads_every_page_and_keeps_number_label_and_type() -> None:
    caller = Caller([BOOKS, []])
    entries = SmartStoreAddressBookSource(caller, _bearer).address_books()  # type: ignore[arg-type]
    assert [(e.address_book_no, e.name, e.address_type) for e in entries] == [
        (200401837, "반품교환지", "REFUND_OR_EXCHANGE"),
        (200441202, "출고지 JAPAN", "RELEASE"),
    ]
    assert [c[0] for c in caller.calls] == [EndpointId.SMARTSTORE_ADDRESSBOOK_LIST] * 2
    assert [c[1].page for c in caller.calls] == [1, 2]


def test_a_provider_that_repeats_its_page_ends_the_listing() -> None:
    caller = Caller([BOOKS])  # every page answers the same entries
    entries = SmartStoreAddressBookSource(caller, _bearer).address_books()  # type: ignore[arg-type]
    assert len(entries) == 2 and len(caller.calls) == 2 <= MAX_PAGES


@pytest.mark.parametrize(
    "page",
    (
        "not a list",
        [{"name": "no number", "addressType": "RELEASE"}],
        [{"addressBookNo": True, "addressType": "RELEASE"}],
        [{"addressBookNo": 1, "name": "no type"}],
    ),
)
def test_a_malformed_page_is_refused(page: object) -> None:
    source = SmartStoreAddressBookSource(Caller([page]), _bearer)  # type: ignore[arg-type]
    with pytest.raises(PolicyBlockedError) as refused:
        source.address_books()
    assert refused.value.code == "SMARTSTORE_ADDRESSBOOK_RESPONSE_INVALID"


def test_no_committed_session_calls_nothing() -> None:
    caller = Caller([BOOKS])
    source = SmartStoreAddressBookSource(caller, lambda: None)  # type: ignore[arg-type]
    with pytest.raises(PolicyBlockedError) as refused:
        source.address_books()
    assert refused.value.code == "SMARTSTORE_SESSION_UNAVAILABLE"
    assert caller.calls == []


def test_the_endpoint_is_a_read_that_retains_no_address_or_contact() -> None:
    contract = resolve(EndpointId.SMARTSTORE_ADDRESSBOOK_LIST)
    assert contract.mutating is False and contract.path == "/v1/seller/addressbooks-for-page"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"addressBooks": BOOKS, "totalPages": 1})

    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(handler))
    response = caller.call(
        EndpointId.SMARTSTORE_ADDRESSBOOK_LIST, AddressBookListRequest("token", 3, 5, 1)
    )
    assert seen[0].method == "GET" and seen[0].url.params["page"] == "1"
    assert seen[0].headers["authorization"] == "Bearer token"
    retained = str(response.retained)
    assert "secret street" not in retained and "010-0000-0000" not in retained
    assert response.retained == {
        "addressBooks": [
            {"addressBookNo": 200441202, "name": "출고지 JAPAN", "addressType": "RELEASE"},
            {"addressBookNo": 200401837, "name": "반품교환지", "addressType": "REFUND_OR_EXCHANGE"},
        ]
    }


def test_the_empty_last_page_the_retention_drops_ends_the_listing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        books = BOOKS if request.url.params["page"] == "1" else []
        return httpx.Response(200, json={"addressBooks": books})

    caller = SmartStoreEndpointCaller(transport=httpx.MockTransport(handler))
    empty = caller.call(
        EndpointId.SMARTSTORE_ADDRESSBOOK_LIST, AddressBookListRequest("token", 3, 5, 2)
    )
    assert empty.retained == {}
    entries = SmartStoreAddressBookSource(caller, _bearer).address_books()
    assert [e.address_book_no for e in entries] == [200401837, 200441202]
