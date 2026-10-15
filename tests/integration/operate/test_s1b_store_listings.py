"""통합DB 「스토어 상품」's read (ADR-0042 S1b, §0 b2): one page of the store's listings.

No provider is reached: the catalog is filled by the StoreCatalog owner's own sync over the fake
source of the S1a suite, with synthetic values only. Proven here: each listing on a page is what
the owner's per-listing view says of it; every listing is in exactly one status filter, so the
counts add up to 전체 and each filter lists exactly its count; the search matches the name, the
seller codes and the product numbers; the server pages the list and only the page carries read
fields; a bad filter, search or page is refused; a store that is not connected has nothing; and
the route adds the leaf category's whole name only from ICBM's recorded category catalog.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.container import Container
from app.platform.core.errors import InputValidationError
from app.stages.operate.catalog.facts import (
    HTTP_404,
    OBSERVED,
    READ_FAILED,
    REMOVED,
    STATUS_DELETE,
    ListingFields,
    ListingRead,
)
from app.stages.operate.catalog.service import (
    ALL,
    AUTO,
    ENDED,
    FILTERS,
    NOT_READ,
    NOT_SEEN,
    ON_SALE,
    OPERATOR,
    OTHER_STATUS,
    OUT_OF_STOCK,
    SALE_CLOSED,
    SUSPENDED,
    StoreCatalogService,
)
from app.stages.register.category_catalog import CategoryCatalogStore, ProviderCategory
from tests.integration.operate.test_s1a_store_catalog import ACCOUNT, CID, FakeSource, _service
from tests.support.jobs_support import FakeClock

pytestmark = pytest.mark.integration

LISTINGS = "/api/v1/operate/store-catalog/listings"


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


def listing_fields(
    name: str,
    *,
    status: str = "SALE",
    price: int = 10000,
    stock: int = 5,
    code: str | None = None,
    leaf: str | None = None,
) -> ListingFields:
    seller_code = code if code is not None else f"code-{name}"
    canonical: dict[str, Any] = {
        "fields_version": "fixture-fields/v1",
        "state": {"sale_status": status, "display_status": "ON"},
        "name": name,
        "price": {"sale_price": price},
        "stock": {"stock_quantity": stock},
        "seller_keys": {"seller_management_code": seller_code, "seller_barcode": None},
        "category": {"leaf_category_id": leaf},
        "options": {
            "group_names": ["색상", None, None, None],
            "combinations": [
                {
                    "id": "1",
                    "names": ["빨강", None, None, None],
                    "stock_quantity": 3,
                    "price": 0,
                    "usable": True,
                    "seller_manager_code": None,
                },
            ],
        },
        "shipping": {
            "delivery_type": "DELIVERY",
            "delivery_attribute_type": "NORMAL",
            "delivery_fee_type": "CONDITIONAL_FREE",
            "base_fee": 3000,
            "free_conditional_amount": 30000,
            "delivery_fee_pay_type": "PREPAID",
        },
    }
    return ListingFields(
        fields_version="fixture-fields/v1",
        sale_status=status,
        display_status="ON",
        name=name,
        sale_price=price,
        stock_quantity=stock,
        seller_management_code=seller_code,
        seller_barcode=None,
        leaf_category_id=leaf,
        canonical=canonical,
    )


def observed(name: str, **kwargs: Any) -> ListingRead:
    return ListingRead(OBSERVED, fields=listing_fields(name, **kwargs))


def _store(source: FakeSource, service: StoreCatalogService, clock: FakeClock | None) -> None:
    """A store whose listings sit in every filter: two syncs, the second missing three listings.

    - 101 on sale, 102 out of stock, 103 suspended, 104 sale closed, 105 waiting (another status);
    - 106 is not listed by the second sync and reads live: NOT_SEEN;
    - 107 is not listed by the second sync and its read says DELETE: ENDED;
    - 108 is new in the second sync and its read fails: NOT_READ.
    """
    source.listing("101", "102", "103", "104", "105", "106", "107")
    source.answers.update(
        {
            "101": observed("Red Mug", code="KM-101", leaf="50000001"),
            "102": observed("Blue Mug", status="OUTOFSTOCK", stock=0),
            "103": observed("Green Cup", status="SUSPENSION"),
            "104": observed("Old Plate", status="CLOSE"),
            "105": observed("New Bowl", status="WAIT"),
            "106": observed("Gone Spoon"),
            "107": observed("Deleted Fork"),
        }
    )
    service.sync(trigger=OPERATOR, correlation_id=CID)
    if clock is not None:
        clock.advance(1800)
    source.listing("101", "102", "103", "104", "105", "108")
    source.answers["106"] = observed("Gone Spoon")
    source.answers["107"] = ListingRead(REMOVED, removal_evidence=STATUS_DELETE)
    source.answers["108"] = ListingRead(READ_FAILED, error_code="FIXTURE_FAILED")
    service.sync(trigger=OPERATOR, correlation_id=CID)


EXPECTED = {
    "101": ON_SALE,
    "102": OUT_OF_STOCK,
    "103": SUSPENDED,
    "104": SALE_CLOSED,
    "105": OTHER_STATUS,
    "106": NOT_SEEN,
    "107": ENDED,
    "108": NOT_READ,
}


def test_each_listing_on_a_page_is_what_the_owner_says_of_it(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    service = _service(container, source)
    _store(source, service, clock)
    page = service.listing_page(limit=200)
    assert [e.listing.origin_product_no for e in page.listings] == sorted(EXPECTED)
    owner = {view.origin_product_no: view for view in service.listings()}
    for found in page.listings:
        assert found.listing == owner[found.listing.origin_product_no]
        assert found.filter == EXPECTED[found.listing.origin_product_no]
    # The failed read of 108 shows no fields; the ended 107 keeps its last read fields.
    by_no = {e.listing.origin_product_no: e.listing for e in page.listings}
    assert by_no["108"].fields is None and by_no["108"].last_result == READ_FAILED
    assert by_no["107"].fields is not None and by_no["107"].ended_evidence == STATUS_DELETE


def test_every_listing_is_in_exactly_one_filter_and_the_counts_add_up(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    service = _service(container, source)
    _store(source, service, clock)
    page = service.listing_page()
    assert page.counts[ALL] == 8
    assert sum(page.counts[key] for key in FILTERS if key != ALL) == page.counts[ALL]
    for key in FILTERS:
        filtered = service.listing_page(filter_key=key)
        assert filtered.matching == page.counts[key] == len(filtered.listings)
        assert filtered.counts == page.counts  # the counts never depend on the chosen filter
        if key != ALL:
            assert {e.filter for e in filtered.listings} <= {key}
            assert {e.listing.origin_product_no for e in filtered.listings} == {
                no for no, expected in EXPECTED.items() if expected == key
            }


def test_the_search_matches_the_name_the_seller_codes_and_the_product_numbers(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    service = _service(container, source)
    _store(source, service, clock)

    def found(query: str, filter_key: str = ALL) -> set[str]:
        page = service.listing_page(query=query, filter_key=filter_key)
        return {e.listing.origin_product_no for e in page.listings}

    assert found("mug") == {"101", "102"}  # the name, case folded
    assert found("  MUG ") == {"101", "102"}
    assert found("km-101") == {"101"}  # the seller code of the detail read
    assert found("code-108") == {"108"}  # the seller code the enumeration listed
    assert found("9103") == {"103"}  # the channel product number
    assert found("mug", OUT_OF_STOCK) == {"102"}
    counted = service.listing_page(query="mug")
    assert (counted.counts[ALL], counted.counts[ON_SALE], counted.counts[OUT_OF_STOCK]) == (2, 1, 1)
    assert service.listing_page(query="nothing like it").matching == 0
    assert service.listing_page(query="   ").query is None


def test_the_server_pages_the_list_and_only_the_page_carries_fields(
    container: Container, source: FakeSource
) -> None:
    origins = [str(1000 + n) for n in range(120)]
    source.listing(*origins)
    service = _service(container, source)
    service.sync(trigger=AUTO, correlation_id=CID)
    first = service.listing_page(limit=50)
    second = service.listing_page(offset=50, limit=50)
    third = service.listing_page(offset=100, limit=50)
    assert (first.matching, len(first.listings), len(second.listings), len(third.listings)) == (
        120,
        50,
        50,
        20,
    )
    seen = [e.listing.origin_product_no for p in (first, second, third) for e in p.listings]
    assert seen == origins
    assert all(e.listing.fields is not None for e in first.listings)
    assert service.listing_page(offset=200).listings == ()


def test_a_bad_filter_search_or_page_is_refused(container: Container, source: FakeSource) -> None:
    service = _service(container, source)
    for kwargs, code in (
        ({"filter_key": "SALE"}, "STORE_CATALOG_FILTER_INVALID"),
        ({"query": "x" * 101}, "STORE_CATALOG_QUERY_INVALID"),
        ({"offset": -1}, "STORE_CATALOG_PAGE_INVALID"),
        ({"limit": 0}, "STORE_CATALOG_PAGE_INVALID"),
        ({"limit": 201}, "STORE_CATALOG_PAGE_INVALID"),
    ):
        with pytest.raises(InputValidationError) as refused:
            service.listing_page(**kwargs)
        assert refused.value.code == code


def test_a_store_that_is_not_connected_has_nothing(
    container: Container, source: FakeSource
) -> None:
    service = _service(container, source, account=None)
    page = service.listing_page()
    assert (page.matching, page.listings) == (0, ())
    assert set(page.counts) == set(FILTERS) and not any(page.counts.values())


def _connect(client: TestClient, source: FakeSource) -> StoreCatalogService:
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    service = container.store_catalog
    service._source = source
    service._account_of = lambda key: ACCOUNT  # type: ignore[method-assign]
    service._pause_s = 0.0
    return service


def test_the_route_pages_filters_and_names_categories_from_icbms_catalog(
    client: TestClient, source: FakeSource
) -> None:
    service = _connect(client, source)
    _store(source, service, None)
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    shown = client.get(LISTINGS, params={"filter": ON_SALE})
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert body["filter"] == ON_SALE and body["matching"] == 1
    assert body["counts"][ALL] == 8 and body["counts"][NOT_SEEN] == 1
    assert body["status"]["last_complete"] is not None
    (row,) = body["listings"]
    assert (row["origin_product_no"], row["filter"], row["sale_price"]) == ("101", ON_SALE, 10000)
    # No recorded category catalog: the leaf has no name, and none is invented.
    assert row["category_name"] is None and row["fields"]["category"]["leaf_category_id"]
    CategoryCatalogStore(container.db, container.clock, container.audit).record(
        "smartstore",
        (ProviderCategory("50000001", "머그컵", "생활/건강>주방용품>컵>머그컵", True),),
        endpoint_mapping_revision="test",
        actor="operator",
        correlation_id="c",
    )
    named = client.get(LISTINGS, params={"filter": ON_SALE}).json()["listings"][0]
    assert named["category_name"] == "생활/건강>주방용품>컵>머그컵"
    status = client.get("/api/v1/operate/store-catalog/status")
    # A live listing outside the second enumeration made that sync INCOMPLETE (S1a): the last
    # complete sync is still the first one, with its seven listings.
    assert status.status_code == 200 and status.json()["status"]["listing_count"] == 7
    assert status.json()["status"]["last_run"]["outcome"] == "INCOMPLETE"
    refused = client.get(LISTINGS, params={"filter": "SALE"})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "STORE_CATALOG_FILTER_INVALID"


def test_the_route_reads_nothing_and_lists_nothing_without_a_connected_store(
    client: TestClient,
) -> None:
    shown = client.get(LISTINGS)
    assert shown.status_code == 200
    body = shown.json()
    assert body["status"]["marketplace_account_id"] is None
    assert body["listings"] == [] and body["counts"][ALL] == 0


def test_a_404_ends_a_listing_into_the_ended_filter(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    source.listing("201", "202")
    service = _service(container, source)
    service.sync(trigger=AUTO, correlation_id=CID)
    clock.advance(1800)
    source.listing("201")
    source.answers["202"] = ListingRead(REMOVED, removal_evidence=HTTP_404)
    service.sync(trigger=AUTO, correlation_id=CID)
    ended = service.listing_page(filter_key=ENDED)
    assert [(e.listing.origin_product_no, e.listing.ended_evidence) for e in ended.listings] == [
        ("202", HTTP_404)
    ]
    owner = {view.origin_product_no: view for view in service.listings()}
    assert ended.listings[0].listing == owner["202"]
