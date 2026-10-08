"""M6-E adopted listings (ADR-0024): SmartStore listings ICBM did not create, adopted by the
owner-declared seller-code convention and proven by a read-back, then operated read-only.

No provider is reached: the listing finder and the listing reader are fakes that answer what each
test says. The products, registrations, audit log and the listing-sync, stock-recheck and order
owners are the real ones. Proven here: only the convention code, an exact single candidate and a
matching read-back adopt; a source ICBM registered, a multi-Item source, or a provider that cannot
answer adopts nothing; one adoption per source and per listing, never rewritten; a rate limit ends
the pass; the listing sync reads adopted listings back and only provider evidence ends one; stock
recheck lists the adopted source; an order of an adopted listing stays UNMATCHED and is linked to
the adoption; and REGISTER sees the adopted Item.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.operate.adoption import (
    ADOPTED,
    ALREADY_ADOPTED,
    EXTERNALLY_REMOVED,
    NOT_REACHED,
    NOT_SINGLE_ITEM,
    REGISTERED_BY_ICBM,
    AdoptionService,
    seller_code_of,
)
from app.stages.operate.adoption_facts import (
    AMBIGUOUS,
    FOUND,
    MISMATCH,
    NOT_FOUND,
    RATE_LIMITED,
    FoundListing,
)
from app.stages.operate.listing import OPERATOR, ListingSyncService
from app.stages.register.store import RegistrationStore
from integrations.marketplaces.smartstore import readback as smartstore_readback
from tests.integration.register.test_m5_registration_foundation import (  # noqa: F401 - fixtures
    MARKET,
    _confirm,
    _finish,
    _freeze,
    _intent,
    _priced,
    account,
    sources,
    store,
)
from tests.support.product_support import SUPPLIER, Collections, product

pytestmark = pytest.mark.integration

CID = "cid-adoption"


@dataclass
class FakeFinder:
    answers: dict[str, FoundListing] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    def available(self) -> bool:
        return True

    def find(self, seller_code: str) -> FoundListing:
        self.asked.append(seller_code)
        return self.answers.get(seller_code, FoundListing(NOT_FOUND))


def _service(container: Container, finder: FakeFinder) -> AdoptionService:
    def bound(supplier_key: str) -> Mapping[str, tuple[str, ...]]:
        with container.product_store.reading() as unit:
            return unit.bound_items_of_supplier(supplier_key)

    def registered() -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        for record in container.registrations.marketplace_registrations(MARKET):
            snapshot = container.registrations.snapshot(record.registration_snapshot_id)
            for item in () if snapshot is None else snapshot.items:
                with container.product_store.reading() as unit:
                    identity = unit.source_identity_of_item(item.item_id)
                if identity is not None:
                    found.add(identity)
        return found

    return AdoptionService(
        db=container.db,
        clock=container.clock,
        audit=container.audit,
        finder_factory=lambda pause: finder,
        bound_items=bound,
        registered_sources=registered,
        marketplace_key=MARKET,
        pause_s=0.0,
    )


def _collected(container: Container, sources: Collections, source_product_id: str) -> str:  # noqa: F811
    run_id, _revision = sources.collect(product(), source_product_id=source_product_id)
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None
    return result.item_id


def _found(origin: str, channel: str = "c-1") -> FoundListing:
    return FoundListing(FOUND, origin, channel, "SALE", "ON")


def _run(service: AdoptionService) -> dict[str, Any]:
    run = service.run(SUPPLIER, actor="operator", correlation_id=CID)
    return {o.source_product_id: o for o in run.outcomes}


def test_the_convention_code_names_the_source_product() -> None:
    assert seller_code_of("kmretail", "287") == "km287"
    assert seller_code_of("another-supplier", "287") is None


def test_a_proven_listing_is_adopted_once_and_audited(
    container: Container,
    sources: Collections,  # noqa: F811
) -> None:
    item_id = _collected(container, sources, "287")
    finder = FakeFinder({"km287": _found("9001")})
    service = _service(container, finder)
    first = _run(service)["287"]
    assert (first.outcome, first.marketplace_product_id) == (ADOPTED, "9001")
    (record,) = service.active()
    assert (record.item_id, record.seller_code, record.supplier_key) == (item_id, "km287", SUPPLIER)
    assert finder.asked == ["km287"]
    # A second pass finds it adopted and asks the provider nothing.
    second = _run(service)["287"]
    assert (second.outcome, second.adoption_id) == (ALREADY_ADOPTED, record.adoption_id)
    assert finder.asked == ["km287"]
    with sqlite3.connect(container.config.database_path) as raw:
        (audits,) = raw.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'LISTING_ADOPTED'"
        ).fetchone()
        assert audits == 1
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("UPDATE operate_adopted_listings SET item_id = 'other'")
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM operate_adopted_listings")


@pytest.mark.parametrize("answer", [NOT_FOUND, AMBIGUOUS, MISMATCH, "FAILED", "UNAVAILABLE"])
def test_nothing_unproven_is_adopted(
    container: Container,
    sources: Collections,  # noqa: F811
    answer: str,
) -> None:
    _collected(container, sources, "287")
    service = _service(container, FakeFinder({"km287": FoundListing(answer, "9001")}))
    assert _run(service)["287"].outcome == answer
    assert service.active() == ()


def test_a_rate_limit_ends_the_pass(
    container: Container,
    sources: Collections,  # noqa: F811
) -> None:
    for source in ("287", "288", "289"):
        _collected(container, sources, source)
    finder = FakeFinder({"km287": FoundListing(RATE_LIMITED, error_code="SMARTSTORE_RATE_LIMITED")})
    outcomes = _run(_service(container, finder))
    assert outcomes["287"].outcome == RATE_LIMITED
    assert {outcomes["288"].outcome, outcomes["289"].outcome} == {NOT_REACHED}
    assert finder.asked == ["km287"]


def test_a_source_icbm_registered_is_never_adopted(
    container: Container,
    config: AppConfig,
    sources: Collections,  # noqa: F811
    store: RegistrationStore,  # noqa: F811
) -> None:
    item = _priced(container, config, sources, "287")
    intent = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, intent, RemoteOutcome.APPLIED_PROVEN, "mp-icbm-1")
    _confirm(store, intent)
    finder = FakeFinder({"km287": _found("9001")})
    assert _run(_service(container, finder))["287"].outcome == REGISTERED_BY_ICBM
    assert finder.asked == []


def test_a_source_with_more_than_one_bound_item_is_not_adopted(
    container: Container,
    sources: Collections,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _collected(container, sources, "287")
    finder = FakeFinder({"km287": _found("9001")})
    service = _service(container, finder)
    monkeypatch.setattr(service, "_bound_items", lambda supplier: {"287": ("i-1", "i-2")})
    assert _run(service)["287"].outcome == NOT_SINGLE_ITEM
    assert finder.asked == []


class Reader:
    def __init__(self) -> None:
        self.answer: Any = None

    def available(self) -> bool:
        return True

    def read(self, *, marketplace_product_id: str) -> Mapping[str, Any]:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _listing_body(status: str = "SALE", code: str = "km287") -> dict[str, Any]:
    return {
        "originProduct": {"statusType": status, "salePrice": 25000, "stockQuantity": 3},
        "smartstoreChannelProduct": {
            "channelProductDisplayStatusType": "ON",
            "sellerManagementCode": code,
        },
    }


def _listing_sync(container: Container, reader: Reader, adoptions: AdoptionService) -> Any:
    return ListingSyncService(
        db=container.db,
        clock=container.clock,
        registrations=container.registrations,
        reader=reader,
        normalize=smartstore_readback.normalize,
        interval_s=1800,
        marketplace_key=MARKET,
        adoptions=adoptions,
    )


def test_the_listing_sync_reads_an_adopted_listing_and_only_provider_evidence_ends_it(
    container: Container,
    sources: Collections,  # noqa: F811
) -> None:
    _collected(container, sources, "287")
    adoptions = _service(container, FakeFinder({"km287": _found("9001")}))
    _run(adoptions)
    reader = Reader()
    reader.answer = _listing_body()
    sync = _listing_sync(container, reader, adoptions)
    run = sync.sync(trigger=OPERATOR, correlation_id=CID)
    assert (run.targets, run.observed) == (1, 1)
    (state,) = [s for s in sync.overview().listings if s.kind == "ADOPTED"]
    assert (state.sale_status, state.stock_quantity, state.registration_id) == ("SALE", 3, None)
    # A failed read proves nothing: still ACTIVE.
    reader.answer = AppError("SMARTSTORE_HTTP_500", "x", details={"http_status": 500})
    sync.sync(trigger=OPERATOR, correlation_id=CID)
    assert adoptions.active() != ()
    # The documented DELETE status is provider evidence.
    reader.answer = _listing_body(status="DELETE")
    sync.sync(trigger=OPERATOR, correlation_id=CID)
    assert adoptions.active() == ()
    (record,) = adoptions.all()
    assert record.state == EXTERNALLY_REMOVED


def test_stock_recheck_lists_the_adopted_source(
    container: Container,
    sources: Collections,  # noqa: F811
) -> None:
    _collected(container, sources, "287")
    adoptions = _service(container, FakeFinder({"km287": _found("9001")}))
    _run(adoptions)
    container.stock_recheck._adoptions = adoptions  # the container's owner, this test's adoptions
    container.stock_recheck._marketplace_key = MARKET
    listed = container.stock_recheck.listed_sources()
    (source,) = [s for s in listed if s.source_product_id == "287"]
    assert source.adoption_ids and source.registration_ids == ()


def test_an_order_of_an_adopted_listing_stays_unmatched_and_is_linked(
    container: Container,
    sources: Collections,  # noqa: F811
) -> None:
    """ADR-0024 §4: ICBM did not register the product, so the order's ADR-0023 resolution is
    UNMATCHED; the separate, immutable link names the adoption, its Item and its source."""
    from app.stages.operate.order_facts import ChangePage
    from app.stages.operate.orders import UNMATCHED, OrderSyncService, ShippingCipher
    from tests.integration.operate.test_m6_orders import FakeSource, _change, _facts

    item_id = _collected(container, sources, "287")
    adoptions = _service(container, FakeFinder({"km287": _found("9001")}))
    _run(adoptions)
    source = FakeSource(
        pages=[ChangePage((_change(container),))],
        facts={"po-1": _facts(original_product_id="9001", seller_product_code="km287")},
    )
    orders = OrderSyncService(
        db=container.db,
        clock=container.clock,
        registrations=container.registrations,
        source_identity=lambda item: None,
        source=source,
        cipher=ShippingCipher(container.secrets),
        audit=container.audit,
        interval_s=600,
        initial_lookback_s=86400,
        retention_days=90,
        order_read_attested=lambda: True,
        marketplace_key=MARKET,
        pause_s=0.0,
        adoptions=adoptions,
    )
    orders.sync(trigger=OPERATOR, correlation_id=CID)
    (order,) = orders.overview().orders
    assert order.resolution == UNMATCHED and order.registration_id is None
    assert (order.adopted_item_id, order.adopted_supplier_key, order.adopted_source_product_id) == (
        item_id,
        SUPPLIER,
        "287",
    )
    # GPT audit (PR #257): an order naming only the channel product is linked by it, exactly as
    # the order resolution falls back to the channel product id.
    source.pages = [ChangePage((_change(container, "po-2", minutes=1),))]
    source.facts["po-2"] = _facts(
        "po-2", original_product_id=None, channel_product_id="c-1", seller_product_code="km287"
    )
    orders.sync(trigger=OPERATOR, correlation_id=CID)
    by_id = {o.product_order_id: o for o in orders.overview().orders}
    assert (by_id["po-2"].resolution, by_id["po-2"].adopted_item_id) == (UNMATCHED, item_id)
    with sqlite3.connect(container.config.database_path) as raw:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("UPDATE operate_order_adoption_links SET item_id = 'other'")
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM operate_order_adoption_links")


def test_register_sees_the_adopted_item(
    container: Container,
    sources: Collections,  # noqa: F811
) -> None:
    """ADR-0024 §5: the source REGISTER's preflight reads for a second listing of an Item."""
    item_id = _collected(container, sources, "287")
    adoptions = _service(container, FakeFinder({"km287": _found("9001")}))
    _run(adoptions)
    (record,) = adoptions.active()
    assert adoptions.adopted_items(MARKET, [item_id, "other"]) == (record.adoption_id,)
    assert adoptions.adopted_items(MARKET, ["other"]) == ()
    assert adoptions.adopted_items("another-market", [item_id]) == ()
