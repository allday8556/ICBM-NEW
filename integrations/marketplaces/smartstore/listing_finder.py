"""Find one SmartStore listing by an exact seller code and prove it by its origin read-back
(ADR-0024 §3; M6-E).

Read only, with two adopted endpoints and nothing else:
- ``SMARTSTORE_PRODUCT_SEARCH`` by ``SELLER_CODE``, enumerated within the same bounded, consistent
  budget as the reconcile lookup (ADR-0014 §28.2). Only an exact ``STOREFARM`` entry is a candidate;
  the provider's own similar or partial match is never trusted.
- ``SMARTSTORE_ORIGIN_PRODUCT_READ_V2`` of the one candidate. Its ``sellerManagementCode`` must be
  exactly the code, and its sale status must not be ``DELETE``.

A failure never raises past this module: it becomes an outcome that adopts nothing.
"""

from collections.abc import Callable
from typing import Any, Final

from app.platform.core.errors import ErrorClass
from app.stages.operate.adoption_facts import (
    AMBIGUOUS,
    DELETED,
    FAILED,
    FOUND,
    MISMATCH,
    NOT_FOUND,
    RATE_LIMITED,
    UNAVAILABLE,
    FoundListing,
)
from integrations.marketplaces.smartstore import readback, search
from integrations.marketplaces.smartstore.caller import (
    ProductReadRequest,
    ProductSearchRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import EndpointId

DELETE_STATUS: Final = "DELETE"


class SmartStoreListingFinder:
    def __init__(
        self,
        caller: SmartStoreEndpointCaller,
        bearer: Any,
        *,
        pause: Callable[[], None] = lambda: None,
    ) -> None:
        self._caller = caller
        self._bearer = bearer
        # Called before every provider call after the first (the owner paces its pass).
        self._pause = pause

    def available(self) -> bool:
        return self._bearer() is not None

    def find(self, seller_code: str) -> FoundListing:
        bearer = self._bearer()
        if bearer is None:
            return FoundListing(UNAVAILABLE, error_code="SMARTSTORE_SESSION_UNAVAILABLE")
        pages: list[search.SearchPage] = []
        for number in range(search.FIRST_PAGE, search.FIRST_PAGE + search.MAX_PAGES_PER_CHECK):
            self._pause()
            try:
                answer = self._caller.call(
                    EndpointId.SMARTSTORE_PRODUCT_SEARCH,
                    ProductSearchRequest(
                        bearer.access_token,
                        bearer.credential_generation,
                        bearer.session_generation,
                        seller_code,
                        number,
                        search.MAX_PAGE_SIZE,
                    ),
                )
                page = search.read_page(answer.retained)
            except SmartStoreCallError as failure:
                return _failed(failure)
            except search.SearchContractError as unreadable:
                return FoundListing(FAILED, error_code=unreadable.code)
            pages.append(page)
            if page.last:
                break
        else:
            return FoundListing(UNAVAILABLE, error_code="SMARTSTORE_SEARCH_READ_BUDGET_EXCEEDED")
        try:
            search.check_enumeration(pages)
        except search.SearchContractError as inconsistent:
            return FoundListing(FAILED, error_code=inconsistent.code)
        candidates = search.exact_candidates(pages, seller_code)
        if not candidates:
            return FoundListing(NOT_FOUND)
        if len(candidates) > 1:
            return FoundListing(AMBIGUOUS)
        (candidate,) = candidates
        origin = str(candidate.origin_product_no)
        channel = str(candidate.channel_product_no)
        self._pause()
        try:
            read = self._caller.call(
                EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2,
                ProductReadRequest(
                    bearer.access_token,
                    bearer.credential_generation,
                    bearer.session_generation,
                    origin,
                ),
            )
        except SmartStoreCallError as failure:
            return _failed(failure)
        listing = readback.normalize(read.retained)
        if not listing.readable or listing.seller_management_code != seller_code:
            return FoundListing(MISMATCH, origin, channel)
        if listing.sale_status == DELETE_STATUS:
            return FoundListing(DELETED, origin, channel, listing.sale_status)
        return FoundListing(FOUND, origin, channel, listing.sale_status, listing.display_status)


def _failed(failure: SmartStoreCallError) -> FoundListing:
    if failure.error_class is ErrorClass.RATE_LIMITED:
        return FoundListing(RATE_LIMITED, error_code=failure.code)
    return FoundListing(FAILED, error_code=failure.code)


__all__ = ["SmartStoreListingFinder"]
