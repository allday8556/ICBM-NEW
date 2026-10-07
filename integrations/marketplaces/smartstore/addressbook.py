"""Read the seller's SmartStore address book (출고지 / 반품·교환지) through the adopted caller.

A read only, never mutation authority (owner directive 2026-10-07): the Settings delivery policy
chooses its shipping and return address book numbers from this list instead of typing them. Only
each entry's number, operator label and type are kept — never the address or a contact field.
Nothing is stored: the list is read when the operator asks for it.
"""

from dataclasses import dataclass
from typing import Any, Final

from app.platform.core.errors import PolicyBlockedError
from integrations.marketplaces.smartstore.caller import (
    AddressBookListRequest,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import EndpointId

# The pages read at most: an address book holds a handful of entries, and a provider that never
# ends a listing is refused rather than read forever.
MAX_PAGES: Final = 20


@dataclass(frozen=True)
class AddressBookEntry:
    address_book_no: int
    name: str
    # RELEASE (출고지), REFUND_OR_EXCHANGE (반품·교환지) or GENERAL, as the provider names it.
    address_type: str


class SmartStoreAddressBookSource:
    def __init__(self, caller: SmartStoreEndpointCaller, bearer: Any) -> None:
        self._caller = caller
        self._bearer = bearer

    def address_books(self) -> tuple[AddressBookEntry, ...]:
        bearer = self._bearer()
        if bearer is None:
            raise PolicyBlockedError(
                "SMARTSTORE_SESSION_UNAVAILABLE",
                "a current committed SmartStore session is required to read the address book",
            )
        found: dict[int, AddressBookEntry] = {}
        for page in range(1, MAX_PAGES + 1):
            response = self._caller.call(
                EndpointId.SMARTSTORE_ADDRESSBOOK_LIST,
                AddressBookListRequest(
                    bearer.access_token,
                    bearer.credential_generation,
                    bearer.session_generation,
                    page,
                ),
            )
            # The caller answers only a body its contract predicate accepted (a documented
            # ``addressBooks`` array); retention drops an empty array, so an absent key is an
            # empty page.
            books = response.retained.get("addressBooks", [])
            if not isinstance(books, list):
                raise PolicyBlockedError(
                    "SMARTSTORE_ADDRESSBOOK_RESPONSE_INVALID",
                    "the address book response did not retain the documented array",
                )
            fresh = 0
            for book in books:
                number = book.get("addressBookNo") if isinstance(book, dict) else None
                kind = book.get("addressType") if isinstance(book, dict) else None
                name = book.get("name") if isinstance(book, dict) else None
                if (
                    not isinstance(number, int)
                    or isinstance(number, bool)
                    or not isinstance(kind, str)
                ):
                    raise PolicyBlockedError(
                        "SMARTSTORE_ADDRESSBOOK_RESPONSE_INVALID",
                        "an address book entry is missing a documented field",
                    )
                if number not in found:
                    fresh += 1
                    found[number] = AddressBookEntry(
                        number, name.strip() if isinstance(name, str) else "", kind
                    )
            # A page that adds nothing new ends the listing (the last page, or a provider that
            # repeats its last page).
            if fresh == 0:
                break
        return tuple(found[number] for number in sorted(found))


__all__ = ["AddressBookEntry", "SmartStoreAddressBookSource"]
