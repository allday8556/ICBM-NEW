"""The ListingLinks owner (ADR-0042 S2a, §7): which common product each store listing is.

No provider is reached: the catalog source is a fake answering synthetic listings, and every
product is collected from synthetic source truth. Proven here:

- after a COMPLETE sync every listing no strong key links becomes its own common product — a
  ``ProductGroup`` with no supplier member (「도매처 미연결」) — once, and a later sync adds none;
- the evidence task of §3: the 통합DB list shows such a product (no member, no membership
  revision), its detail and registration target refuse it as a target, and no readiness derives
  for it (it has no Item, so the review producer indexes nothing);
- an owner-declared seller code links automatically, with a notice; undo ends the link with its
  history, gives the listing its own product back and is not undone by the next sync;
- an ambiguous strong key (two listings of one store) or a capacity conflict never links: the
  match is proposed instead;
- a similar name is proposed only; 「다른 상품」 hides it until revoked, and 「같은 상품」
  merges the listing's own product into the chosen one (a GroupChangeEvent, the retired product
  kept);
- registrations and adoptions are read as links and supersede a listing's own empty product;
- one listing per product and store unless a DuplicateOverride covers it (SCP-09);
- links, decisions and notices are append-only;
- the catalog duplicate source matches a listing linked to the unit's product (LISTING_LINK).
"""

import json
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.platform.core.errors import AppError
from app.stages.collect.facts import TextValue
from app.stages.operate.catalog.coverage import CatalogCoverage
from app.stages.operate.catalog.duplicates import CatalogDuplicateSource
from app.stages.operate.catalog.facts import (
    COMPLETE,
    OBSERVED,
    CatalogEntry,
    Enumeration,
    ListingFields,
    ListingRead,
)
from app.stages.operate.catalog.links import (
    ADOPTED,
    IMPORTED,
    NAME_SIMILAR,
    REGISTERED_BY_ICBM,
    STRONG_KEY,
    USER_CONFIRMED,
    ListingLinkService,
)
from app.stages.operate.catalog.service import OPERATOR, StoreCatalogService
from app.stages.products.catalog import MEMBERSHIP_REVISION_MISSING, SelectionConflictError
from app.stages.products.service import ProductsService
from app.stages.register.policy import CATALOG_MAX_AGE, DuplicateKeyKind
from app.stages.register.preparation import DUPLICATE_EVIDENCE_UNAVAILABLE, DuplicateVerdict
from app.stages.register.provider import DuplicateUnit
from tests.conftest import LOCAL
from tests.support.collect_support import confirmed
from tests.support.product_support import SUPPLIER, Collections, product

pytestmark = pytest.mark.integration

ACCOUNT = "mpa-fixture-links"
MARKET = "smartstore"
CID = "cid-store-links"
HEADERS = {"X-ICBM-Client": "pytest"}


def fields(
    name: str, *, code: str | None = None, barcode: str | None = None, price: int = 10000
) -> ListingFields:
    canonical = {
        "fields_version": "fixture-fields/v1",
        "state": {"sale_status": "SALE", "display_status": "ON"},
        "name": name,
        "price": {"sale_price": price},
        "stock": {"stock_quantity": 5},
        "seller_keys": {"seller_management_code": code, "seller_barcode": barcode},
        "options": {"group_names": [], "combinations": []},
    }
    return ListingFields(
        fields_version="fixture-fields/v1",
        sale_status="SALE",
        display_status="ON",
        name=name,
        sale_price=price,
        stock_quantity=5,
        seller_management_code=code,
        seller_barcode=barcode,
        leaf_category_id=None,
        canonical=canonical,
    )


class FakeSource:
    marketplace_key = MARKET
    contract_version = "fixture-catalog/v1"

    def __init__(self) -> None:
        self.listings: dict[str, ListingFields] = {}

    def put(self, origin: str, listing: ListingFields) -> None:
        self.listings[origin] = listing

    def available(self) -> bool:
        return True

    def enumerate(
        self, *, pause: Callable[[], None], observe: Callable[[object], None] | None = None
    ) -> Enumeration:
        entries = tuple(
            CatalogEntry(origin, f"9{origin}", listing.seller_management_code, 1)
            for origin, listing in sorted(self.listings.items())
        )
        return Enumeration(
            COMPLETE,
            self.contract_version,
            entries=entries,
            total_elements=len(entries),
            pages_read=1,
        )

    def read(
        self, origin_product_no: str, *, observe: Callable[[object], None] | None = None
    ) -> ListingRead:
        return ListingRead(OBSERVED, fields=self.listings[origin_product_no])


class Harness:
    def __init__(self, container: Container, source: FakeSource) -> None:
        self.container = container
        self.source = source
        self.registered: dict[str, str] = {}
        self.adopted: dict[str, str] = {}
        self.overrides: set[str] = set()
        self.catalog = StoreCatalogService(
            db=container.db,
            clock=container.clock,
            source=source,
            account_of=lambda key: ACCOUNT if key == MARKET else None,
            interval_s=1800,
            pause_s=0.0,
        )
        self.links = ListingLinkService(
            db=container.db,
            clock=container.clock,
            audit=container.audit,
            catalog=self.catalog,
            products=container.product_store,
            registered=lambda key, account: dict(self.registered),
            adopted=lambda key: dict(self.adopted),
            overridden=lambda key, account, group: group in self.overrides,
            conventions={SUPPLIER: "KM{source_product_id}"},
        )
        self.catalog.add_listener(self.links.after_sync)

    def sync(self) -> None:
        run = self.catalog.sync(trigger=OPERATOR, correlation_id=CID)
        assert run.outcome == COMPLETE

    def link_of(self, origin: str) -> Any:
        listing_id = self.links.listing_id_of(MARKET, ACCOUNT, origin)
        for entry in self.links.for_groups(self._all_groups()):
            for listing in entry.listings:
                if listing.listing_id == listing_id:
                    return listing
        return None

    def _all_groups(self) -> list[str]:
        found = rows(self.container, "SELECT product_group_id FROM product_groups")
        return [row[0] for row in found]


def rows(container: Container, sql: str, *args: object) -> list[tuple[Any, ...]]:
    with sqlite3.connect(container.config.database_path) as raw:
        return raw.execute(sql, args).fetchall()


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def harness(container: Container, source: FakeSource) -> Harness:
    return Harness(container, source)


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


def supplier_group(
    container: Container, sources: Collections, source_product_id: str, name: str
) -> str:
    """A collected supplier product (kmretail, so its convention code is KM<id>)."""
    run_id, _revision = sources.collect(
        product(original_name=confirmed(TextValue(text=name), ".name")),
        source_product_id=source_product_id,
    )
    container.materializer.materialize_run(run_id)
    group = container.product_store.group_of_source(SUPPLIER, source_product_id)
    assert group is not None
    return group


def test_every_imported_listing_becomes_its_own_common_product(
    container: Container, harness: Harness, source: FakeSource
) -> None:
    source.put("101", fields("국산 참기름 300ml", code="SC-0101"))
    source.put("102", fields("볶음 깨 1kg", code="SC-0102"))
    before = container.products.product_count()
    harness.sync()
    first, second = harness.link_of("101"), harness.link_of("102")
    assert (first.kind, second.kind) == (IMPORTED, IMPORTED)
    assert first.product_group_id != second.product_group_id
    assert first.unlinkable is False and first.name == "국산 참기름 300ml"
    assert container.products.product_count() == before + 2
    # A later COMPLETE sync creates nothing more.
    harness.sync()
    assert container.products.product_count() == before + 2
    assert rows(container, "SELECT COUNT(*) FROM store_listing_links") == [(2,)]
    (entry,) = harness.links.for_groups([first.product_group_id])
    assert entry.supplier_connected is False
    overview = harness.links.overview()
    assert (overview.linked, overview.unconnected) == (2, 2)
    assert overview.as_of is not None and overview.notices == ()
    # ADR-0042 §3 evidence: the 통합DB list shows the zero-member ACTIVE product as it is.
    page = container.products.page()
    shown = {row.product.product_group_id: row for row in page.products}
    row = shown[first.product_group_id]
    assert row.product.members == () and row.member_names == ()
    assert row.product.membership_revision_id is None and row.product.items == ()
    # A search finds it by its store listing's name.
    assert harness.links.search("참기름") == {first.product_group_id}
    searching = ProductsService(container.product_store, listing_search=harness.links.search)
    found = searching.page(query="참기름")
    assert [r.product.product_group_id for r in found.products] == [first.product_group_id]
    # Its detail and registration target refuse it as a target; no readiness derives for it.
    detail = container.products.detail(first.product_group_id)
    assert detail.item_selection == ()
    assert detail.selection_unavailable_reason == MEMBERSHIP_REVISION_MISSING
    with pytest.raises(SelectionConflictError) as refused:
        container.products.registration_target(first.product_group_id, "m-1", ["i-1"])
    assert refused.value.details == {"reason": MEMBERSHIP_REVISION_MISSING}
    groups = {group for group, _item in container.product_store.active_items()}
    assert first.product_group_id not in groups
    assert container.screens.product_db().products_total == before + 2


def test_a_seller_code_links_automatically_with_a_notice_and_undo(
    container: Container, harness: Harness, source: FakeSource, sources: Collections
) -> None:
    group = supplier_group(container, sources, "287", "국산 참기름 300ml")
    source.put("101", fields("참기름 300ml 스토어", code=" km287 "))
    harness.sync()
    link = harness.link_of("101")
    assert (link.kind, link.key_kind, link.product_group_id) == (STRONG_KEY, "SELLER_CODE", group)
    assert link.unlinkable
    (notice,) = harness.links.overview().notices
    assert notice.link_id == link.link_id and notice.product_name == "국산 참기름 300ml"
    (entry,) = harness.links.for_groups([group])
    assert entry.supplier_connected and [v.origin_product_no for v in entry.listings] == ["101"]
    result = harness.links.unlink(link.link_id, actor="operator", correlation_id=CID, undo=True)
    assert result.link is not None and result.link.kind == IMPORTED
    assert result.link.product_group_id != group
    assert harness.links.overview().notices == ()
    assert rows(
        container,
        "SELECT state, end_reason FROM store_listing_links WHERE link_id = ?",
        link.link_id,
    ) == [("ENDED", "UNDO")]
    # The next sync does not link it back: the undo recorded 「다른 상품」.
    harness.sync()
    assert harness.link_of("101").kind == IMPORTED
    overview = harness.links.overview()
    assert overview.proposals == ()
    (decision,) = overview.decisions
    assert decision.product_group_id == group and decision.origin_product_no == "101"
    # Revoking it offers the match again, as a proposal: nothing moves by itself.
    harness.links.revoke_decision(decision.decision_id, actor="operator", correlation_id=CID)
    (proposal,) = harness.links.overview().proposals
    assert (proposal.product_group_id, proposal.signal) == (group, "SELLER_CODE")
    assert proposal.survivors == (group,)
    assert rows(
        container,
        "SELECT COUNT(*) FROM audit_events WHERE event_type IN"
        " ('STORE_LISTINGS_LINKED', 'STORE_LISTING_UNLINKED', 'STORE_LISTING_DECISION_RECORDED')",
    ) == [(3,)]  # the pass, the undo (with its 「다른 상품」) and the revocation


def test_an_ambiguous_or_conflicting_strong_key_never_links(
    container: Container, harness: Harness, source: FakeSource, sources: Collections
) -> None:
    shared = supplier_group(container, sources, "287", "국산 참기름 300ml")
    sized = supplier_group(container, sources, "288", "생들기름 350ml")
    # Two listings of one store share KM287: ambiguous.
    source.put("101", fields("참기름 A", code="KM287"))
    source.put("102", fields("참기름 B", code="KM287"))
    # KM288 matches, but the listing states 1L against the member's 350ml.
    source.put("103", fields("생들기름 1L", code="KM288"))
    harness.sync()
    assert {harness.link_of(o).kind for o in ("101", "102", "103")} == {IMPORTED}
    proposals = {
        (p.listing.origin_product_no, p.product_group_id): p
        for p in harness.links.overview().proposals
    }
    assert proposals[("101", shared)].ambiguous and proposals[("102", shared)].ambiguous
    conflict = proposals[("103", sized)]
    assert conflict.signal == "SELLER_CODE" and conflict.conflicts == ("volume",)
    assert not conflict.ambiguous


def test_a_similar_name_is_proposed_and_the_seller_decides(
    container: Container, harness: Harness, source: FakeSource, sources: Collections
) -> None:
    group = supplier_group(container, sources, "300", "국산 참기름 500ml 2병 선물")
    source.put("101", fields("국산 참기름 500ml 2병 선물세트", code="OWN-0101"))
    harness.sync()
    own = harness.link_of("101").product_group_id
    (proposal,) = harness.links.overview().proposals
    assert (proposal.product_group_id, proposal.signal) == (group, NAME_SIMILAR)
    assert proposal.similarity is not None and proposal.supplier_keys == (SUPPLIER,)
    # 「다른 상품」 hides it; revoking brings it back.
    rejected = harness.links.reject(
        proposal.listing.listing_id, group, actor="operator", correlation_id=CID
    )
    assert harness.links.overview().proposals == ()
    assert rejected.decision_id is not None
    harness.links.revoke_decision(rejected.decision_id, actor="operator", correlation_id=CID)
    assert len(harness.links.overview().proposals) == 1
    # 「같은 상품」: the listing's own empty product merges into the supplier's.
    result = harness.links.confirm(
        proposal.listing.listing_id, group, actor="operator", correlation_id=CID
    )
    assert result.retired_group_id == own
    assert result.link is not None
    assert (result.link.kind, result.link.product_group_id) == (USER_CONFIRMED, group)
    assert rows(container, "SELECT status FROM product_groups WHERE product_group_id = ?", own) == [
        ("RETIRED",)
    ]
    ((event_type, successors),) = rows(
        container, "SELECT event_type, successor_group_ids FROM group_change_events"
    )
    assert event_type == "MERGE" and group in successors
    assert own not in {r.product.product_group_id for r in container.products.page().products}
    assert harness.links.overview().proposals == ()
    # Unlinking gives the listing its own product again; the group and history stay.
    unlinked = harness.links.unlink(result.link.link_id, actor="operator", correlation_id=CID)
    assert unlinked.link is not None and unlinked.link.kind == IMPORTED
    assert unlinked.link.product_group_id not in (own, group)


def test_two_empty_products_merge_into_the_survivor_the_seller_picks(
    container: Container, harness: Harness, source: FakeSource
) -> None:
    source.put("101", fields("국산 들기름 350ml 선물", code="OWN-0101"))
    harness.sync()
    first = harness.link_of("101")
    second_group = container.product_store.create_group(decided_by="fixture")
    # The listing's own product may survive only because the chosen one is empty too.
    result = harness.links.confirm(
        first.listing_id,
        second_group,
        survivor_group_id=first.product_group_id,
        actor="operator",
        correlation_id=CID,
    )
    assert result.retired_group_id == second_group
    assert result.link is not None and result.link.product_group_id == first.product_group_id


def test_registrations_and_adoptions_read_as_links(
    container: Container, harness: Harness, source: FakeSource, sources: Collections
) -> None:
    registered = supplier_group(container, sources, "287", "국산 참기름 300ml")
    adopted = supplier_group(container, sources, "288", "생들기름 350ml")
    source.put("101", fields("참기름 등록 상품", code="ICBM-0001"))
    source.put("102", fields("생들기름 기존 상품", code="ICBM-0002"))
    harness.sync()
    own = harness.link_of("101").product_group_id
    # Later the registration and adoption owners hold them: their links supersede the import.
    harness.registered["101"] = registered
    harness.adopted["102"] = adopted
    harness.sync()
    first, second = harness.link_of("101"), harness.link_of("102")
    assert (first.kind, first.product_group_id, first.link_id) == (
        REGISTERED_BY_ICBM,
        registered,
        None,
    )
    assert (second.kind, second.product_group_id) == (ADOPTED, adopted)
    assert not first.unlinkable and not second.unlinkable
    assert rows(container, "SELECT status FROM product_groups WHERE product_group_id = ?", own) == [
        ("RETIRED",)
    ]
    assert rows(container, "SELECT COUNT(*) FROM store_listing_links WHERE state = 'ACTIVE'") == [
        (0,)
    ]
    with pytest.raises(AppError) as owned:
        harness.links.confirm(first.listing_id, adopted, actor="operator", correlation_id=CID)
    assert owned.value.code == "STORE_LINK_LISTING_OWNED"


def test_a_derived_link_never_hides_a_stored_link_that_still_holds_evidence(
    container: Container, harness: Harness, source: FakeSource, sources: Collections
) -> None:
    """PR #370 audit: when a registration later holds a listing that is strongly linked to a
    product with a supplier of its own, ``_supersede`` leaves that stored link ACTIVE (the seller
    decides). ``linked_groups`` names both products, so the duplicate check of either still sees
    the listing."""
    stored_group = supplier_group(container, sources, "287", "국산 참기름 300ml")
    registered_group = supplier_group(container, sources, "288", "생들기름 350ml")
    source.put("101", fields("참기름", code="KM287"))
    harness.sync()
    assert harness.link_of("101").product_group_id == stored_group
    harness.registered["101"] = registered_group
    harness.sync()
    assert rows(
        container,
        "SELECT product_group_id FROM store_listing_links WHERE state = 'ACTIVE'",
    ) == [(stored_group,)]
    assert harness.links.linked_groups(MARKET, ACCOUNT)["101"] == tuple(
        sorted((stored_group, registered_group))
    )
    coverage = CatalogCoverage(catalog=harness.catalog, audit=container.audit)
    view = coverage.view()
    assert view.current is not None
    coverage.confirm(run_id=view.current.run_id, actor="operator", correlation_id=CID)
    duplicates = CatalogDuplicateSource(
        marketplace_key=MARKET,
        contract_version="fixture-duplicate/v1",
        catalog=harness.catalog,
        seller_code=lambda identity: f"ICBM-{identity}",
        convention_codes=lambda items: (),
        mutation_started_at=lambda key, account: None,
        refresh=lambda: False,
        linked=harness.links.linked_groups,
        coverage=coverage,
    )
    for group in (stored_group, registered_group):
        check = duplicates.check(
            DuplicateUnit(MARKET, ACCOUNT, f"unit-{group}", group_ids=(group,))
        )
        assert check.evidence is not None and check.evidence.verdict is DuplicateVerdict.MATCH
        assert [m.key_kind for m in check.matched] == [DuplicateKeyKind.LISTING_LINK]


def test_one_listing_per_product_and_store(
    container: Container, harness: Harness, source: FakeSource, sources: Collections
) -> None:
    group = supplier_group(container, sources, "287", "국산 참기름 300ml")
    source.put("101", fields("참기름 A", code="KM287"))
    source.put("102", fields("참기름 B", code="OTHER-102"))
    harness.sync()
    assert harness.link_of("101").kind == STRONG_KEY
    second = harness.link_of("102")
    with pytest.raises(AppError) as refused:
        harness.links.confirm(second.listing_id, group, actor="operator", correlation_id=CID)
    assert refused.value.code == "STORE_LINK_GROUP_ALREADY_IN_STORE"
    harness.overrides.add(group)
    result = harness.links.confirm(second.listing_id, group, actor="operator", correlation_id=CID)
    assert result.link is not None and result.link.product_group_id == group


def test_links_decisions_and_notices_are_append_only(
    container: Container, harness: Harness, source: FakeSource, sources: Collections
) -> None:
    supplier_group(container, sources, "287", "국산 참기름 300ml")
    source.put("101", fields("참기름", code="KM287"))
    harness.sync()
    link = harness.link_of("101")
    harness.links.unlink(link.link_id, actor="operator", correlation_id=CID)
    with sqlite3.connect(container.config.database_path) as raw:
        for statement in (
            "UPDATE store_listing_links SET kind = 'USER_CONFIRMED'",
            "UPDATE store_listing_links SET state = 'ACTIVE' WHERE state = 'ENDED'",
            "DELETE FROM store_listing_links",
            "UPDATE store_listing_decisions SET product_group_id = 'other'",
            "DELETE FROM store_listing_decisions",
            "UPDATE store_link_notices SET link_id = 'other'",
            "DELETE FROM store_link_notices",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(statement)


def test_the_duplicate_source_matches_a_listing_linked_to_the_units_product(
    container: Container, harness: Harness, source: FakeSource, sources: Collections
) -> None:
    group = supplier_group(container, sources, "287", "국산 참기름 300ml")
    source.put("101", fields("전혀 다른 이름", code="KM287"))
    harness.sync()
    # ADR-0042 §5.2 tightening: a NO_MATCH needs the seller's coverage confirmation.
    coverage = CatalogCoverage(catalog=harness.catalog, audit=container.audit)
    view = coverage.view()
    assert view.current is not None
    coverage.confirm(run_id=view.current.run_id, actor="operator", correlation_id=CID)
    duplicates = CatalogDuplicateSource(
        marketplace_key=MARKET,
        contract_version="fixture-duplicate/v1",
        catalog=harness.catalog,
        seller_code=lambda identity: f"ICBM-{identity}",
        convention_codes=lambda items: (),
        mutation_started_at=lambda key, account: None,
        refresh=lambda: False,
        linked=harness.links.linked_groups,
        coverage=coverage,
    )
    unit = DuplicateUnit(MARKET, ACCOUNT, "unit-1", group_ids=(group,))
    check = duplicates.check(unit)
    assert check.evidence is not None and check.evidence.verdict is DuplicateVerdict.MATCH
    assert [m.key_kind for m in check.matched] == [DuplicateKeyKind.LISTING_LINK]
    assert DuplicateKeyKind.LISTING_LINK in check.evidence.keys_checked
    other = duplicates.check(DuplicateUnit(MARKET, ACCOUNT, "unit-2", group_ids=("g-other",)))
    assert other.evidence is not None and other.evidence.verdict is DuplicateVerdict.NO_MATCH


def test_a_failed_links_read_is_no_duplicate_evidence(
    container: Container, harness: Harness, source: FakeSource
) -> None:
    # Fail closed (SCP-06): a links owner that does not answer is never "no link", so the unit
    # gets no evidence — never a NO_MATCH that names LISTING_LINK as checked.
    source.put("101", fields("전혀 다른 이름", code="OWN-0101"))
    harness.sync()

    def broken(key: str, account: str) -> dict[str, str]:
        raise RuntimeError("links owner unavailable")

    duplicates = CatalogDuplicateSource(
        marketplace_key=MARKET,
        contract_version="fixture-duplicate/v1",
        catalog=harness.catalog,
        seller_code=lambda identity: f"ICBM-{identity}",
        convention_codes=lambda items: (),
        mutation_started_at=lambda key, account: None,
        refresh=lambda: False,
        linked=broken,
    )
    check = duplicates.check(DuplicateUnit(MARKET, ACCOUNT, "unit-1", group_ids=("g-1",)))
    assert check.evidence is None
    assert check.problem == DUPLICATE_EVIDENCE_UNAVAILABLE
    assert check.as_of is not None and check.matched == ()
    # The truth token says the read failed, distinct from a store with no links at all.
    healthy = CatalogDuplicateSource(
        marketplace_key=MARKET,
        contract_version="fixture-duplicate/v1",
        catalog=harness.catalog,
        seller_code=lambda identity: f"ICBM-{identity}",
        convention_codes=lambda items: (),
        mutation_started_at=lambda key, account: None,
        refresh=lambda: False,
        linked=lambda key, account: {},
    )
    assert duplicates.truth_token(ACCOUNT, CATALOG_MAX_AGE) != healthy.truth_token(
        ACCOUNT, CATALOG_MAX_AGE
    )


def _seed_member_gtin(container: Container, source_product_id: str, gtin: str) -> None:
    """A CONFIRMED barcode fact on the member's current source revision. No supplier records one
    today (ADR-0042 §3 evidence note); the fact is seeded to prove the comparison is wired."""
    with sqlite3.connect(container.config.database_path) as raw:
        (revision,) = raw.execute(
            "SELECT m.revision_id FROM current_source_revision_moves m"
            " JOIN source_products s ON s.source_product_uid = m.source_product_uid"
            " WHERE s.supplier_key = ? AND s.source_product_id = ?"
            " ORDER BY m.sequence DESC LIMIT 1",
            (SUPPLIER, source_product_id),
        ).fetchone()
        raw.execute(
            "INSERT INTO product_facts_fields"
            " (revision_id, field_key, level, status, value_json, field_fingerprint)"
            " VALUES (?, 'gtin', 'COVERAGE', 'CONFIRMED', ?, ?)",
            (revision, json.dumps({"text": gtin}), "c" * 64),
        )


def test_a_valid_gtin_links_to_the_member_that_states_it(
    container: Container, harness: Harness, source: FakeSource, sources: Collections
) -> None:
    group = supplier_group(container, sources, "300", "국산 참기름 300ml")
    other = supplier_group(container, sources, "301", "볶음 참깨 1kg")
    # The member states the UPC-A; the listing carries the same GTIN as an EAN-13.
    _seed_member_gtin(container, "300", "036000291452")
    _seed_member_gtin(container, "301", "8801234567893")
    source.put("101", fields("스토어 참기름", code="OWN-0101", barcode="0036000291452"))
    # A barcode with a wrong check digit is no key: that listing becomes its own product.
    source.put("102", fields("스토어 참깨", code="OWN-0102", barcode="8801234567890"))
    harness.sync()
    first = harness.link_of("101")
    assert (first.kind, first.key_kind, first.product_group_id) == (STRONG_KEY, "GTIN", group)
    (notice,) = harness.links.overview().notices
    assert notice.key_kind == "GTIN" and notice.link_id == first.link_id
    second = harness.link_of("102")
    assert second.kind == IMPORTED and second.product_group_id not in (group, other)


def test_the_api_reads_and_acts_through_the_owner(config: AppConfig, source: FakeSource) -> None:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        app_container: Container = client.app.state.container  # type: ignore[attr-defined]
        sources = Collections.of(app_container, config)
        group = supplier_group(app_container, sources, "300", "국산 참기름 500ml 2병 선물")
        source.put("101", fields("국산 참기름 500ml 2병 선물세트", code="OWN-0101"))
        harness = Harness(app_container, source)
        harness.sync()
        app_container.store_links = harness.links
        overview = client.get("/api/v1/operate/store-links").json()
        (proposal,) = overview["proposals"]
        assert proposal["signal"] == NAME_SIMILAR and overview["as_of"] is not None
        listing_id = proposal["listing"]["listing_id"]
        products = client.get(
            "/api/v1/operate/store-links/products", params={"group_id": [group]}
        ).json()["products"]
        assert products == [{"product_group_id": group, "listings": [], "supplier_connected": True}]
        missing = client.post(
            "/api/v1/operate/store-links/confirm",
            json={"product_group_id": group, "actor": "operator"},
            headers=HEADERS,
        )
        assert missing.status_code == 422
        confirmed_ = client.post(
            "/api/v1/operate/store-links/confirm",
            json={
                "product_group_id": group,
                "marketplace_key": MARKET,
                "marketplace_account_id": ACCOUNT,
                "origin_product_no": "101",
                "actor": "operator",
            },
            headers=HEADERS,
        )
        assert confirmed_.status_code == 200, confirmed_.text
        link = confirmed_.json()["link"]
        assert link["kind"] == USER_CONFIRMED and link["listing_id"] == listing_id
        undone = client.post(
            f"/api/v1/operate/store-links/{link['link_id']}/unlink",
            json={"actor": "operator"},
            headers=HEADERS,
        )
        assert undone.status_code == 200 and undone.json()["link"]["kind"] == IMPORTED
        again = client.post(
            f"/api/v1/operate/store-links/{link['link_id']}/unlink",
            json={"actor": "operator"},
            headers=HEADERS,
        )
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "STORE_LINK_NOT_ACTIVE"
        decisions = client.get("/api/v1/operate/store-links").json()["decisions"]
        revoked = client.post(
            f"/api/v1/operate/store-links/decisions/{decisions[0]['decision_id']}/revoke",
            json={"actor": "operator"},
            headers=HEADERS,
        )
        assert revoked.json() == {"decision_id": decisions[0]["decision_id"], "revoked": True}
        acknowledged = client.post(
            "/api/v1/operate/store-links/notices/acknowledge",
            json={"actor": "operator"},
            headers=HEADERS,
        )
        assert acknowledged.json() == {"acknowledged": 0}
