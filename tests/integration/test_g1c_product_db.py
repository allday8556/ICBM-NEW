"""The product DB's operator read path (Gate 1 G1-C, ADR-0015 §5, Issue #89 5792525426).

Every case runs through the real application on a migrated database: Products are materialized by
the M4 owner from durably RECORDED synthetic source truth, and read back through
``/api/v1/products``. Proven here:
- a populated list of ACTIVE Products with several members and Items, paged by a cursor bound to
  its own search, with no row repeated or skipped and explicit refusals for a bad page request;
- search by product id, supplier key, source id and each member's **current** CONFIRMED name: a
  historical or unconfirmed name never matches, and a retired Product is never listed;
- the detail is exact: membership revision, each member's current source revision and facts,
  every Item's composition and binding; a missing fact stays missing;
- registration-target selection is revalidated whole against the current Product: a retired
  Product, an unbound Item, an Item of another Product and a moved membership are each refused
  with a server reason;
- none of it writes: every table and the audit log are unchanged, and no Draft appears.

No supplier, marketplace or AI provider is contacted.
"""

import base64
import contextlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.collect.facts import (
    Availability,
    FieldFact,
    FieldStatus,
    QuantityTier,
    QuantityTiersValue,
    StockValue,
    TextValue,
)
from app.collect.revisions import StoredRevision
from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.products.materialization import MaterializationStatus
from app.products.model import MoveReason
from app.screens.contracts import ScreenState
from tests.collect_support import absent, confirmed, evidence
from tests.conftest import LOCAL
from tests.product_support import SUPPLIER, Collections, product, raw, review

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
BASE = "/api/v1/products"
RETIRE = "UPDATE product_groups SET status = 'RETIRED', retired_at = '2026-09-23 00:00:00'"


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


# ---------------------------------------------------------------- synthetic source truth


def named(name: str, **overrides: FieldFact) -> dict[str, FieldFact]:
    return product(original_name=confirmed(TextValue(text=name), ".name"), **overrides)


def under_review(name: str) -> FieldFact:
    """A name the page showed but the extractor could not confirm: a value, under review."""
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        TextValue(text=name),
        (evidence(".name", FieldStatus.REVIEW_REQUIRED),),
    )


def tiers(*quantities: int) -> FieldFact:
    return confirmed(
        QuantityTiersValue(
            tiers=tuple(
                QuantityTier(quantity=q, total_price_krw=q * 9900, label=f"{q}") for q in quantities
            )
        ),
        ".tiers",
    )


def materialize(
    container: Container, sources: Collections, source_id: str, fields: dict[str, FieldFact]
) -> tuple[str, StoredRevision]:
    run_id, revision = sources.collect(fields, source_product_id=source_id)
    result = container.materializer.materialize_run(run_id)
    assert result.status is MaterializationStatus.MATERIALIZED, result
    return str(result.product_group_id), revision


def join(container: Container, sources: Collections, group: str, source_id: str, name: str) -> str:
    """A second source product CONFIRMED into ``group``: the next membership revision."""
    _, revision = sources.collect(named(name, brand=absent(".brand")), source_product_id=source_id)
    store = container.product_store
    uid = store.source_product(SUPPLIER, source_id).source_product_uid
    store.record_move(
        uid, revision.revision_id, reason=MoveReason.INITIAL, decided_by="test", correlation_id="c"
    )
    change = store.confirm_new_member(
        group, uid, reason="test-join", decided_by="test", correlation_id="c"
    )
    return change.member_id


def get(api: TestClient, path: str, params: Any = None) -> Any:
    response = api.get(path, params=params, headers=CLIENT)
    assert response.status_code == 200, response.text
    return response.json()


def refused(api: TestClient, path: str, params: Any = None) -> tuple[int, dict[str, Any]]:
    response = api.get(path, params=params, headers=CLIENT)
    assert response.status_code >= 400, response.text
    return response.status_code, response.json()["error"]


def page(api: TestClient, **params: Any) -> Any:
    return get(api, BASE, {k: v for k, v in params.items() if v is not None})


def ids(listed: Any) -> list[str]:
    return [row["product"]["product_group_id"] for row in listed["products"]]


def target(api: TestClient, group: str, membership: str | None, *items: str) -> Any:
    params = [("item_id", item) for item in items]
    if membership is not None:
        params.insert(0, ("membership_revision_id", membership))
    return api.get(f"{BASE}/{group}/registration-target", params=params, headers=CLIENT)


def everything(config: AppConfig) -> dict[str, int]:
    """Every table's row count: a read path that wrote anything would change one."""
    with contextlib.closing(raw(config)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


@pytest.fixture
def catalog(container: Container, sources: Collections) -> dict[str, str]:
    """Five ACTIVE Products and one retired one, created one clock step apart:
    - ``oil``: two members, the second stating no brand;
    - ``tiered``: one member, three quantity Items;
    - ``alpha``, ``beta``, ``gamma``: one member each;
    - ``retired``: materialized, then retired."""
    made: dict[str, str] = {}
    made["oil"], _ = materialize(container, sources, "S-OIL-1", named("들기름 350ml"))
    join(container, sources, made["oil"], "S-OIL-2", "국산 들기름 350ml")
    made["tiered"], _ = materialize(
        container, sources, "S-TIER", named("묶음 참기름", quantity_tiers=tiers(1, 2, 3))
    )
    made["alpha"], _ = materialize(container, sources, "S-ALPHA", named("Alpha Sesame Oil"))
    made["beta"], _ = materialize(container, sources, "S-BETA", named("베타 식용유"))
    made["retired"], _ = materialize(container, sources, "S-OLD", named("단종 상품"))
    made["gamma"], _ = materialize(container, sources, "S-GAMMA", named("감마 올리브유"))
    return made


def retire(config: AppConfig, group: str) -> None:
    with contextlib.closing(raw(config)) as connection:
        connection.execute(f"{RETIRE} WHERE product_group_id = ?", (group,))
        connection.commit()


# ---------------------------------------------------------------- list and pagination


def test_the_list_pages_every_active_product_once_in_one_total_order(
    api: TestClient, config: AppConfig, catalog: dict[str, str]
) -> None:
    retire(config, catalog["retired"])
    with contextlib.closing(raw(config)) as connection:
        expected = [
            row[0]
            for row in connection.execute(
                "SELECT product_group_id FROM product_groups WHERE status = 'ACTIVE'"
                " ORDER BY created_at DESC, product_group_id DESC"
            )
        ]
    assert len(expected) == 5 and catalog["retired"] not in expected

    first = page(api, limit=2)
    assert (first["limit"], first["matching_total"], first["query"]) == (2, 5, None)
    second = page(api, limit=2, cursor=first["next_cursor"])
    last = page(api, limit=2, cursor=second["next_cursor"])
    assert [len(p["products"]) for p in (first, second, last)] == [2, 2, 1]
    assert first["next_cursor"] and second["next_cursor"] and last["next_cursor"] is None
    # No Product repeated, none skipped, in the canonical order; the retired one is never listed.
    assert ids(first) + ids(second) + ids(last) == expected
    # One page holding all of them ends at once.
    whole = page(api, limit=50)
    assert ids(whole) == expected and whole["next_cursor"] is None
    # The default page is bounded.
    assert page(api)["limit"] == 20


def test_a_row_carries_the_canonical_product_and_each_members_current_name(
    api: TestClient, container: Container, catalog: dict[str, str]
) -> None:
    rows = {row["product"]["product_group_id"]: row for row in page(api)["products"]}
    oil = rows[catalog["oil"]]
    # The row's Product is the canonical read-back itself.
    assert oil["product"] == get(api, f"{BASE}/{catalog['oil']}")
    assert oil["product"]["membership_revision_no"] == 2
    names = {m["source_product_id"]: m for m in oil["member_names"]}
    assert set(names) == {"S-OIL-1", "S-OIL-2"}
    # Each member keeps its own name, named by the revision it was read from: none is chosen.
    for source_id, text in (("S-OIL-1", "들기름 350ml"), ("S-OIL-2", "국산 들기름 350ml")):
        member = names[source_id]
        uid = container.product_store.source_product(SUPPLIER, source_id).source_product_uid
        assert member["source_revision_id"] == container.product_store.current_source_revision(uid)
        assert member["facts"] == [{"key": "original_name", "status": "CONFIRMED", "value": text}]
        assert member["images"] is None
    tiered = rows[catalog["tiered"]]["product"]
    assert sorted(item["composition"]["quantity"] for item in tiered["items"]) == [1, 2, 3]


def test_invalid_page_requests_fail_explicitly(api: TestClient, catalog: dict[str, str]) -> None:
    searched = page(api, q="oil", limit=1)
    assert searched["next_cursor"]
    tampered = json.loads(base64.urlsafe_b64decode(searched["next_cursor"] + "=="))
    tampered["id"] = "x" * 40
    forged = base64.urlsafe_b64encode(json.dumps(tampered).encode()).decode().rstrip("=")
    cases: list[tuple[dict[str, Any], str]] = [
        ({"cursor": "not-a-cursor"}, "PRODUCTS_CURSOR_INVALID"),
        ({"cursor": forged, "q": "oil"}, "PRODUCTS_CURSOR_INVALID"),
        # A cursor never continues another search, nor the unfiltered list.
        ({"cursor": searched["next_cursor"], "q": "beta"}, "PRODUCTS_CURSOR_INVALID"),
        ({"cursor": searched["next_cursor"]}, "PRODUCTS_CURSOR_INVALID"),
        ({"limit": 0}, "PRODUCTS_PAGE_LIMIT_INVALID"),
        ({"limit": 51}, "PRODUCTS_PAGE_LIMIT_INVALID"),
        ({"q": "x" * 101}, "PRODUCTS_QUERY_INVALID"),
    ]
    for params, code in cases:
        status, error = refused(api, BASE, params)
        assert (status, error["code"]) == (422, code), params
    status, error = refused(api, BASE, {"limit": "many"})
    assert (status, error["class"]) == (422, "VALIDATION")


# ---------------------------------------------------------------- search


def test_search_reads_identities_and_the_current_confirmed_name(
    api: TestClient, config: AppConfig, catalog: dict[str, str]
) -> None:
    def found(q: str) -> set[str]:
        return set(ids(page(api, q=q, limit=50)))

    assert found(catalog["beta"][:8].upper()) == {catalog["beta"]}  # product id, case folded
    assert found("s-alpha") == {catalog["alpha"]}  # source id
    assert found("S-OIL-2") == {catalog["oil"]}  # a second member's source id
    assert found(SUPPLIER) == set(catalog.values())  # supplier key
    assert found("ALPHA sesame") == {catalog["alpha"]}  # current name, ASCII case folded
    assert found("국산 들기름") == {catalog["oil"]}  # the second member's own name
    assert found("참기름") == {catalog["tiered"]}
    assert found("없는 상품") == set()
    assert page(api, q="  들기름  ")["query"] == "들기름"
    assert page(api, q="   ")["query"] is None
    # A retired Product is never a search result, even by its own identifier.
    retire(config, catalog["retired"])
    assert found(catalog["retired"]) == set()
    assert found("단종") == set()


def test_a_historical_or_unconfirmed_name_never_matches_as_current(
    api: TestClient, container: Container, sources: Collections, catalog: dict[str, str]
) -> None:
    run_id, newer = sources.collect(named("리뉴얼 감마유"), source_product_id="S-GAMMA")
    assert container.materializer.materialize_run(run_id).status is (
        MaterializationStatus.MATERIALIZED
    )
    assert set(ids(page(api, q="올리브유"))) == set()  # the old revision's name
    assert set(ids(page(api, q="리뉴얼"))) == {catalog["gamma"]}
    detail = get(api, f"{BASE}/{catalog['gamma']}/detail")
    member = detail["member_sources"][0]
    assert member["source_revision_id"] == newer.revision_id
    assert member["facts"][0] == {
        "key": "original_name",
        "status": "CONFIRMED",
        "value": "리뉴얼 감마유",
    }
    # A name under review is shown as under review and matches nothing: neither one with no value
    # nor one whose observed value is still unconfirmed.
    for name in (review(".name"), under_review("검토중 베타 식용유")):
        run_id, _ = sources.collect(product(original_name=name), source_product_id="S-BETA")
        assert container.materializer.materialize_run(run_id).status is (
            MaterializationStatus.MATERIALIZED
        )
        assert set(ids(page(api, q="베타"))) == set()
        beta = get(api, f"{BASE}/{catalog['beta']}/detail")["member_sources"][0]["facts"][0]
        assert beta == {"key": "original_name", "status": "REVIEW_REQUIRED", "value": None}


# ---------------------------------------------------------------- detail


def test_the_detail_is_exact_and_every_fact_is_member_scoped(
    api: TestClient, container: Container, catalog: dict[str, str]
) -> None:
    group = catalog["oil"]
    detail = get(api, f"{BASE}/{group}/detail")
    store = container.product_store
    membership = store.current_membership_revision(group)
    assert membership is not None
    assert detail["product"] == get(api, f"{BASE}/{group}")
    assert detail["product"]["membership_revision_id"] == membership.membership_revision_id
    assert detail["product"]["membership_revision_no"] == membership.revision_no == 2
    assert detail["selection_unavailable_reason"] is None
    sources_by_id = {m["source_product_id"]: m for m in detail["member_sources"]}
    members_by_id = {m["source_product_id"]: m for m in detail["product"]["members"]}
    for source_id in ("S-OIL-1", "S-OIL-2"):
        uid = store.source_product(SUPPLIER, source_id).source_product_uid
        revision = store.current_source_revision(uid)
        assert sources_by_id[source_id]["source_revision_id"] == revision
        assert members_by_id[source_id]["current_source_revision_id"] == revision
        assert sources_by_id[source_id]["member_id"] == members_by_id[source_id]["member_id"]
    facts = {
        source_id: {fact["key"]: (fact["status"], fact["value"]) for fact in view["facts"]}
        for source_id, view in sources_by_id.items()
    }
    assert facts["S-OIL-1"] == {
        "original_name": ("CONFIRMED", "들기름 350ml"),
        "brand": ("CONFIRMED", "예시 브랜드"),
        "manufacturer": ("ABSENT", None),
        "origin": ("CONFIRMED", "국산"),
        "stock": ("CONFIRMED", Availability.ON_SALE.value),
    }
    # The second member states no brand: it stays absent, never filled from the first member.
    assert facts["S-OIL-2"]["brand"] == ("ABSENT", None)
    images = sources_by_id["S-OIL-1"]["images"]
    stored = container.revisions.get(str(sources_by_id["S-OIL-1"]["source_revision_id"]))
    assert stored is not None
    assert images == {
        "status": "CONFIRMED",
        "references": 1,
        "included": 1,
        "representative_sha256": stored.images[0].sha256,
    }
    # Every Item with its composition and current binding, and each is selectable.
    assert [s["item_id"] for s in detail["item_selection"]] == [
        i["item_id"] for i in detail["product"]["items"]
    ]
    assert all(s["selectable"] and s["reason"] is None for s in detail["item_selection"])
    tiered = get(api, f"{BASE}/{catalog['tiered']}/detail")
    bound = {
        item["composition"]["quantity"]: item["current_binding"]
        for item in tiered["product"]["items"]
    }
    assert set(bound) == {1, 2, 3}
    assert {b["binding_kind"] for b in bound.values()} == {"SOURCE_OFFER"}
    assert {b["fulfillment_quantity"] for b in bound.values()} == {1, 2, 3}


def test_an_unknown_product_is_not_found(api: TestClient) -> None:
    for path in (f"{BASE}/nope/detail", f"{BASE}/nope/registration-target"):
        status, error = refused(api, path, [("membership_revision_id", "m"), ("item_id", "i")])
        assert (status, error["code"]) == (404, "PRODUCTS_PRODUCT_UNKNOWN")


# ---------------------------------------------------------------- registration-target selection


def _selection(api: TestClient, group: str) -> tuple[str, dict[int, str]]:
    detail = get(api, f"{BASE}/{group}/detail")
    items = {i["composition"]["quantity"]: i["item_id"] for i in detail["product"]["items"]}
    return detail["product"]["membership_revision_id"], items


def test_a_valid_selection_is_revalidated_and_echoed_exactly(
    api: TestClient, catalog: dict[str, str]
) -> None:
    membership, items = _selection(api, catalog["tiered"])
    response = target(api, catalog["tiered"], membership, items[3], items[1])
    assert response.status_code == 200, response.text
    body = response.json()
    detail = get(api, f"{BASE}/{catalog['tiered']}/detail")
    by_id = {i["item_id"]: i for i in detail["product"]["items"]}
    assert body["product_group_id"] == catalog["tiered"]
    assert body["membership_revision_id"] == membership
    assert body["membership_revision_no"] == 1
    assert [i["item_id"] for i in body["items"]] == [
        i["item_id"] for i in detail["product"]["items"] if i["item_id"] in {items[1], items[3]}
    ]
    for item in body["items"]:
        assert item["binding_id"] == by_id[item["item_id"]]["current_binding"]["binding_id"]
        assert item["quantity"] == by_id[item["item_id"]]["composition"]["quantity"]


def test_a_retired_product_is_history_readable_and_never_selectable(
    api: TestClient, config: AppConfig, catalog: dict[str, str]
) -> None:
    membership, items = _selection(api, catalog["retired"])
    retire(config, catalog["retired"])
    assert catalog["retired"] not in ids(page(api, limit=50))
    detail = get(api, f"{BASE}/{catalog['retired']}/detail")
    assert detail["product"]["status"] == "RETIRED"
    assert detail["product"]["retired_at"] is not None
    assert detail["selection_unavailable_reason"] == "PRODUCTS_PRODUCT_RETIRED"
    assert detail["item_selection"] == [
        {"item_id": items[1], "selectable": False, "reason": "PRODUCTS_PRODUCT_RETIRED"}
    ]
    response = target(api, catalog["retired"], membership, items[1])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PRODUCTS_SELECTION_NOT_SELECTABLE"
    assert error["details"] == {"reason": "PRODUCTS_PRODUCT_RETIRED"}


def test_an_item_without_a_current_binding_is_not_selectable(
    api: TestClient, container: Container, sources: Collections, catalog: dict[str, str]
) -> None:
    group = catalog["tiered"]
    membership, items = _selection(api, group)
    # A newer revision drops the 3-pack tier: its Item stays, unbound.
    run_id, _ = sources.collect(
        named("묶음 참기름", quantity_tiers=tiers(1, 2)), source_product_id="S-TIER"
    )
    assert container.materializer.materialize_run(run_id).status is (
        MaterializationStatus.MATERIALIZED
    )
    detail = get(api, f"{BASE}/{group}/detail")
    selection = {s["item_id"]: s for s in detail["item_selection"]}
    assert selection[items[3]] == {
        "item_id": items[3],
        "selectable": False,
        "reason": "PRODUCTS_ITEM_BINDING_MISSING",
    }
    assert selection[items[1]]["selectable"] and selection[items[2]]["selectable"]
    # The same membership, but the chosen Item lost its binding: refused, with its reason.
    assert detail["product"]["membership_revision_id"] == membership
    response = target(api, group, membership, items[1], items[3])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PRODUCTS_SELECTION_ITEM_NOT_SELECTABLE"
    assert error["details"] == {"items": {items[3]: "PRODUCTS_ITEM_BINDING_MISSING"}}
    assert target(api, group, membership, items[1], items[2]).status_code == 200


def test_items_of_another_product_are_never_combined(
    api: TestClient, catalog: dict[str, str]
) -> None:
    membership, items = _selection(api, catalog["tiered"])
    _, other = _selection(api, catalog["alpha"])
    for foreign in (other[1], "00000000-0000-0000-0000-000000000000"):
        response = target(api, catalog["tiered"], membership, items[1], foreign)
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "PRODUCTS_SELECTION_ITEM_OUTSIDE_PRODUCT"
        assert error["details"] == {"item_ids": [foreign]}


def test_a_membership_that_moved_after_selection_is_refused(
    api: TestClient, container: Container, sources: Collections, catalog: dict[str, str]
) -> None:
    group = catalog["alpha"]
    membership, items = _selection(api, group)
    join(container, sources, group, "S-ALPHA-2", "Alpha Sesame Oil 2")
    now = get(api, f"{BASE}/{group}")["membership_revision_id"]
    assert now != membership
    response = target(api, group, membership, items[1])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PRODUCTS_SELECTION_MEMBERSHIP_STALE"
    assert error["details"] == {"membership_revision_id": now}
    # Revalidated on the current membership, the same Item is a valid target again.
    assert target(api, group, now, items[1]).status_code == 200


def test_a_malformed_selection_is_refused_whole(api: TestClient, catalog: dict[str, str]) -> None:
    membership, items = _selection(api, catalog["tiered"])
    group = catalog["tiered"]
    for membership_id, chosen in (
        (None, (items[1],)),
        ("", (items[1],)),
        (membership, ()),
        (membership, (items[1], items[1])),
        (membership, ("x" * 37,)),
        (membership, tuple(f"item-{n}" for n in range(51))),
    ):
        response = target(api, group, membership_id, *chosen)
        assert response.status_code == 422, (membership_id, chosen)
        assert response.json()["error"]["code"] == "PRODUCTS_SELECTION_INVALID"


# ---------------------------------------------------------------- nothing is written


def test_the_product_db_read_path_writes_nothing(
    api: TestClient, config: AppConfig, catalog: dict[str, str]
) -> None:
    membership, items = _selection(api, catalog["tiered"])
    before = everything(config)
    assert before["audit_events"] > 0
    page(api)
    first = page(api, q="oil", limit=1)
    page(api, q="oil", limit=1, cursor=first["next_cursor"])
    for group in catalog.values():
        get(api, f"{BASE}/{group}/detail")
    assert target(api, catalog["tiered"], membership, *items.values()).status_code == 200
    assert target(api, catalog["tiered"], "stale", items[1]).status_code == 409
    refused(api, BASE, {"cursor": "bad"})
    assert everything(config) == before
    assert before["registration_drafts"] == 0


def test_the_product_db_screen_is_ready_once_products_exist(
    api: TestClient, catalog: dict[str, str]
) -> None:
    screen = get(api, "/api/v1/screens/db")
    assert screen["meta"]["state"] == ScreenState.READY.value
    assert screen["products_total"] == 6


def test_a_sold_out_member_is_shown_as_its_revision_states_it(
    api: TestClient, container: Container, sources: Collections
) -> None:
    group, _ = materialize(
        container,
        sources,
        "S-SOLD",
        named("품절 상품", stock=confirmed(StockValue(availability=Availability.SOLD_OUT), ".b")),
    )
    facts = get(api, f"{BASE}/{group}/detail")["member_sources"][0]["facts"]
    assert {"key": "stock", "status": "CONFIRMED", "value": "SOLD_OUT"} in facts
    # Stock is readiness's to judge, not selection's: the Item is still a selectable target.
    assert get(api, f"{BASE}/{group}/detail")["item_selection"][0]["selectable"] is True
