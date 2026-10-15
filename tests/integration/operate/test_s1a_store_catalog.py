"""The StoreCatalog owner (ADR-0042 S1a, §5): a store's whole catalog, synced read only.

No provider is reached: the source is a fake answering what each test says, with synthetic values
only. Proven here: an empty store is a valid COMPLETE result; listings get one immutable identity
and one append-only observation per sync, and an unchanged listing stores no field text again;
only a full enumeration with every listing read is COMPLETE; a listing the enumeration did not list
is NOT_SEEN until provider evidence (DELETE or a 404) ends it, and a live one there makes the sync
INCOMPLETE; a rate limit or a lost session ends the run UNAVAILABLE; the snapshot is fresh for 60
minutes from its start and goes stale once an ICBM mutation of the store starts; the first sync is
due at once after connecting; one sync at a time; and a finished run never changes.
"""

import contextlib
import sqlite3
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.container import Container
from app.platform.db.migrate import alembic_config, upgrade_to_head
from app.stages.operate.catalog.facts import (
    COMPLETE,
    ERROR,
    HTTP_404,
    INCOMPLETE,
    OBSERVED,
    RATE_LIMITED,
    READ_FAILED,
    REMOVED,
    STATUS_DELETE,
    UNAVAILABLE,
    CatalogEntry,
    Enumeration,
    ListingFields,
    ListingRead,
)
from app.stages.operate.catalog.service import (
    AUTO,
    CONNECT,
    ENDED,
    FRESH,
    LISTED,
    NONE,
    NOT_SEEN,
    OPERATOR,
    STALE,
    StoreCatalogBusy,
    StoreCatalogScheduler,
    StoreCatalogService,
    refresh_budget,
)
from tests.support.jobs_support import FakeClock

pytestmark = pytest.mark.integration

ACCOUNT = "mpa-fixture-1"
CID = "cid-store-catalog"


def fields(name: str, price: int = 10000, status: str = "SALE") -> ListingFields:
    canonical = {
        "fields_version": "fixture-fields/v1",
        "state": {"sale_status": status, "display_status": "ON"},
        "name": name,
        "price": {"sale_price": price},
        "stock": {"stock_quantity": 5},
        "seller_keys": {"seller_management_code": f"code-{name}", "seller_barcode": None},
    }
    return ListingFields(
        fields_version="fixture-fields/v1",
        sale_status=status,
        display_status="ON",
        name=name,
        sale_price=price,
        stock_quantity=5,
        seller_management_code=f"code-{name}",
        seller_barcode=None,
        leaf_category_id=None,
        canonical=canonical,
    )


def entry(origin: str) -> CatalogEntry:
    return CatalogEntry(origin, f"9{origin}", f"code-{origin}", 1)


class FakeSource:
    marketplace_key = "smartstore"
    contract_version = "fixture-catalog/v1"

    def __init__(self) -> None:
        self.is_available = True
        self.enumeration = Enumeration(COMPLETE, self.contract_version, total_elements=0)
        self.answers: dict[str, ListingRead] = {}
        self.reads: list[str] = []
        self.paused = 0
        self.hold: threading.Event | None = None

    def listing(self, *origins: str) -> None:
        self.enumeration = Enumeration(
            COMPLETE,
            self.contract_version,
            entries=tuple(entry(origin) for origin in origins),
            total_elements=len(origins),
            pages_read=1,
        )
        for origin in origins:
            self.answers.setdefault(origin, ListingRead(OBSERVED, fields=fields(origin)))

    def available(self) -> bool:
        return self.is_available

    def enumerate(
        self, *, pause: Callable[[], None], observe: Callable[[object], None] | None = None
    ) -> Enumeration:
        pause()
        if observe is not None:
            observe({"contents": [], "totalElements": 0})
        if self.hold is not None:
            self.hold.wait(5)
        return self.enumeration

    def read(
        self, origin_product_no: str, *, observe: Callable[[object], None] | None = None
    ) -> ListingRead:
        self.reads.append(origin_product_no)
        if observe is not None:
            observe({"originProduct": {"statusType": "SALE"}})
        return self.answers[origin_product_no]


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


def _service(
    container: Container, source: FakeSource, account: str | None = ACCOUNT
) -> StoreCatalogService:
    return StoreCatalogService(
        db=container.db,
        clock=container.clock,
        source=source,
        account_of=lambda key: account if key == "smartstore" else None,
        interval_s=1800,
        pause_s=0.0,
    )


def _rows(container: Container, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(container.config.database_path) as raw:
        return raw.execute(sql).fetchall()


def test_an_empty_store_is_a_valid_complete_result(
    container: Container, source: FakeSource
) -> None:
    service = _service(container, source)
    run = service.sync(trigger=CONNECT, correlation_id=CID)
    assert (run.outcome, run.error_code, run.total_elements) == (COMPLETE, None, 0)
    status = service.status()
    assert status.last_complete is not None and status.listing_count == 0
    assert status.running is None
    snapshot = service.snapshot()
    assert (snapshot.freshness, snapshot.listings) == (FRESH, ())


def test_listings_are_observed_and_an_unchanged_listing_stores_no_field_text_again(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    source.listing("101", "102")
    service = _service(container, source)
    first = service.sync(trigger=OPERATOR, correlation_id=CID)
    assert (first.outcome, first.listings_enumerated, first.details_done) == (COMPLETE, 2, 2)
    assert first.full_read
    assert source.reads == ["101", "102"]
    views = {view.origin_product_no: view for view in service.listings()}
    assert views["101"].state == LISTED and views["101"].channel_product_no == "9101"
    assert views["101"].listed_seller_code == "code-101"
    assert views["101"].fields is not None and views["101"].fields["name"] == "101"
    assert (views["101"].sale_status, views["101"].sale_price) == ("SALE", 10000)
    assert views["101"].details_read_at == first.as_of
    clock.advance(1800)
    second = service.sync(trigger=OPERATOR, correlation_id=CID)
    assert second.outcome == COMPLETE
    # One identity per listing; one observation per read; one field set per content; one
    # membership row for an unchanged catalog.
    assert _rows(container, "SELECT COUNT(*) FROM store_catalog_listings") == [(2,)]
    assert _rows(container, "SELECT COUNT(*) FROM store_catalog_observations") == [(4,)]
    assert _rows(container, "SELECT COUNT(*) FROM store_catalog_field_sets") == [(2,)]
    assert _rows(container, "SELECT COUNT(*) FROM store_catalog_memberships") == [(1,)]
    # A changed listing is a new field set; its per-group digests show which group moved.
    source.answers["101"] = ListingRead(OBSERVED, fields=fields("101", price=9900))
    clock.advance(1800)
    service.sync(trigger=OPERATOR, correlation_id=CID)
    assert _rows(container, "SELECT COUNT(*) FROM store_catalog_field_sets") == [(3,)]
    assert service.status().listing_count == 2


def test_a_periodic_sync_reads_only_new_listings_and_a_rolling_budget(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    # ADR-0042 §5.1: only new or due listings are origin-read; 「지금 동기화」 re-reads every one.
    assert refresh_budget(1200, 1800) == 30  # N/40 at the 30-minute cadence
    assert refresh_budget(10, 1800) == 1
    assert refresh_budget(0, 1800) == 0
    origins = [str(n) for n in range(201, 211)]
    source.listing(*origins)
    service = _service(container, source)
    first = service.sync(trigger=CONNECT, correlation_id=CID)
    assert (first.outcome, first.full_read, first.details_target) == (COMPLETE, False, 10)
    assert source.reads == origins  # every listing is new
    # Each periodic sync enumerates everything but re-reads one listing, the longest unread.
    source.reads.clear()
    for _ in range(48):
        clock.advance(1800)
        run = service.sync(trigger=AUTO, correlation_id=CID)
        assert (run.outcome, run.details_target, run.listings_enumerated) == (COMPLETE, 1, 10)
    assert len(source.reads) == 48
    # Round robin: every listing was re-read, none more than once in ten syncs.
    assert set(source.reads) == set(origins)
    assert all(source.reads[i : i + 10].count(o) <= 1 for i in range(0, 48, 10) for o in origins)
    # A new listing is read at once, beside the budget.
    source.listing(*origins, "211")
    source.reads.clear()
    clock.advance(1800)
    run = service.sync(trigger=AUTO, correlation_id=CID)
    assert run.outcome == COMPLETE and source.reads[0] == "211" and len(source.reads) == 2
    # 「지금 동기화」 re-reads every listing.
    source.reads.clear()
    clock.advance(1800)
    run = service.sync(trigger=OPERATOR, correlation_id=CID)
    assert run.full_read and sorted(source.reads) == sorted([*origins, "211"])


def test_details_older_than_a_day_make_the_sync_incomplete(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    origins = [str(n) for n in range(301, 311)]
    source.listing(*origins)
    service = _service(container, source)
    assert service.sync(trigger=CONNECT, correlation_id=CID).outcome == COMPLETE
    # After a day without syncs, one periodic sync cannot refresh them all within its budget.
    clock.advance(25 * 3600)
    run = service.sync(trigger=AUTO, correlation_id=CID)
    assert (run.outcome, run.error_code, run.details_target) == (
        INCOMPLETE,
        "STORE_CATALOG_DETAILS_STALE",
        1,
    )
    # The snapshot stays the last COMPLETE one, and is stale by now.
    assert service.snapshot().freshness == STALE
    # 「지금 동기화」 re-reads every listing and completes.
    run = service.sync(trigger=OPERATOR, correlation_id=CID)
    assert run.outcome == COMPLETE
    assert service.snapshot().freshness == FRESH


def test_an_incomplete_enumeration_concludes_nothing(
    container: Container, source: FakeSource
) -> None:
    source.enumeration = Enumeration(
        INCOMPLETE, source.contract_version, error_code="SEARCH_RESPONSE_INCONSISTENT"
    )
    service = _service(container, source)
    run = service.sync(trigger=AUTO, correlation_id=CID)
    assert (run.outcome, run.error_code) == (INCOMPLETE, "SEARCH_RESPONSE_INCONSISTENT")
    assert source.reads == []
    assert service.status().last_complete is None
    assert service.snapshot().freshness == NONE


@pytest.mark.parametrize("outcome", [UNAVAILABLE, ERROR])
def test_an_unavailable_or_failed_enumeration_is_shown_as_such(
    container: Container, source: FakeSource, clock: FakeClock, outcome: str
) -> None:
    source.listing("101")
    service = _service(container, source)
    service.sync(trigger=AUTO, correlation_id=CID)
    clock.advance(1800)
    source.enumeration = Enumeration(outcome, source.contract_version, error_code="GW.RATE_LIMIT")
    run = service.sync(trigger=AUTO, correlation_id=CID)
    status = service.status()
    # The failed sync is the last one shown; the earlier success stays the last COMPLETE one.
    assert status.last_run is not None and status.last_run.outcome == outcome
    assert status.last_complete is not None and status.last_complete.run_id != run.run_id


def test_a_failed_detail_read_makes_the_sync_incomplete(
    container: Container, source: FakeSource
) -> None:
    source.listing("101", "102")
    source.answers["102"] = ListingRead(READ_FAILED, error_code="SMARTSTORE_SERVER_ERROR")
    run = _service(container, source).sync(trigger=AUTO, correlation_id=CID)
    assert (run.outcome, run.error_code) == (INCOMPLETE, "STORE_CATALOG_DETAIL_READ_FAILED")
    assert (run.details_done, run.details_failed) == (1, 1)


def test_a_rate_limit_or_a_lost_session_ends_the_sync_unavailable(
    container: Container, source: FakeSource
) -> None:
    source.listing("101", "102")
    source.answers["101"] = ListingRead(RATE_LIMITED, error_code="GW.RATE_LIMIT")
    service = _service(container, source)
    run = service.sync(trigger=AUTO, correlation_id=CID)
    assert (run.outcome, run.error_code) == (UNAVAILABLE, "GW.RATE_LIMIT")
    assert source.reads == ["101"]
    source.is_available = False
    run = service.sync(trigger=AUTO, correlation_id=CID)
    assert (run.outcome, run.error_code) == (UNAVAILABLE, "SMARTSTORE_SESSION_UNAVAILABLE")


def test_a_listing_missing_from_the_enumeration_is_not_seen_until_provider_evidence_ends_it(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    source.listing("101", "102", "103")
    service = _service(container, source)
    service.sync(trigger=AUTO, correlation_id=CID)
    # 102 is gone from the enumeration and its read says DELETE; 103 answers a 404.
    source.listing("101")
    source.answers["102"] = ListingRead(REMOVED, removal_evidence=STATUS_DELETE)
    source.answers["103"] = ListingRead(REMOVED, removal_evidence=HTTP_404)
    clock.advance(1800)
    run = service.sync(trigger=AUTO, correlation_id=CID)
    assert run.outcome == COMPLETE and run.details_target == 3
    views = {view.origin_product_no: view for view in service.listings()}
    assert (views["102"].state, views["102"].ended_evidence) == (ENDED, STATUS_DELETE)
    assert (views["103"].state, views["103"].ended_evidence) == (ENDED, HTTP_404)
    # The last fields read stay visible; nothing is deleted.
    assert views["102"].fields is not None
    # An ended listing is read no more.
    source.reads.clear()
    clock.advance(1800)
    service.sync(trigger=AUTO, correlation_id=CID)
    assert source.reads == ["101"]
    assert {v.origin_product_no for v in service.snapshot().listings} == {"101"}


def test_a_live_listing_outside_the_enumeration_makes_the_sync_incomplete(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    source.listing("101", "102")
    service = _service(container, source)
    service.sync(trigger=AUTO, correlation_id=CID)
    source.listing("101")  # 102 is still readable and not DELETE, but the search did not list it
    clock.advance(1800)
    run = service.sync(trigger=AUTO, correlation_id=CID)
    assert (run.outcome, run.error_code) == (
        INCOMPLETE,
        "STORE_CATALOG_LISTING_OUTSIDE_ENUMERATION",
    )
    # The seller sees it as not seen in the store (「스토어에서 확인 안 됨」), with what it read.
    views = {view.origin_product_no: view for view in service.listings()}
    assert (views["102"].state, views["102"].sale_status) == (NOT_SEEN, "SALE")
    # The duplicate snapshot is still the last COMPLETE one, which listed it.
    assert {v.origin_product_no for v in service.snapshot().listings} == {"101", "102"}


def test_absence_alone_never_ends_a_listing(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    source.listing("101", "102")
    service = _service(container, source)
    service.sync(trigger=AUTO, correlation_id=CID)
    # The next enumeration does not list 102 and its read fails: it is NOT_SEEN, never ENDED, and
    # the sync is not complete.
    source.listing("101")
    source.answers["102"] = ListingRead(READ_FAILED, error_code="SMARTSTORE_SERVER_ERROR")
    clock.advance(1800)
    run = service.sync(trigger=AUTO, correlation_id=CID)
    assert run.outcome == INCOMPLETE
    views = {view.origin_product_no: view for view in service.listings()}
    assert (views["102"].state, views["102"].last_result) == (NOT_SEEN, READ_FAILED)
    assert views["102"].fields is not None  # the last fields read stay visible
    assert _rows(container, "SELECT COUNT(*) FROM store_catalog_listing_endings") == [(0,)]
    # Provider evidence ends it.
    source.answers["102"] = ListingRead(REMOVED, removal_evidence=STATUS_DELETE)
    clock.advance(1800)
    assert service.sync(trigger=AUTO, correlation_id=CID).outcome == COMPLETE
    assert {v.origin_product_no: v.state for v in service.listings()}["102"] == ENDED


def test_the_snapshot_is_fresh_for_sixty_minutes_and_until_a_mutation_starts(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    source.listing("101")
    service = _service(container, source)
    run = service.sync(trigger=AUTO, correlation_id=CID)
    assert service.snapshot().freshness == FRESH
    clock.advance(59 * 60)
    assert service.snapshot().freshness == FRESH
    # An ICBM mutation of the store that started after as_of makes it stale at once.
    assert service.snapshot(mutation_started_at=run.as_of + timedelta(seconds=1)).freshness == STALE
    assert service.snapshot(mutation_started_at=run.as_of - timedelta(seconds=1)).freshness == FRESH
    clock.advance(2 * 60)
    snapshot = service.snapshot()
    assert snapshot.freshness == STALE and snapshot.run is not None
    assert snapshot.run.as_of == run.as_of


def test_the_first_sync_is_due_at_once_and_later_ones_on_the_cadence(
    container: Container, source: FakeSource, clock: FakeClock
) -> None:
    assert _service(container, source, account=None).due() is None
    source.is_available = False
    assert _service(container, source).due() is None
    source.is_available = True
    service = _service(container, source)
    assert service.due() == CONNECT
    service.sync(trigger=CONNECT, correlation_id=CID)
    assert service.due() is None
    clock.advance(1800)
    assert service.due() == AUTO
    # A sync that did not complete is retried after five minutes, not a whole interval.
    source.enumeration = Enumeration(
        UNAVAILABLE, source.contract_version, error_code="GW.RATE_LIMIT"
    )
    service.sync(trigger=AUTO, correlation_id=CID)
    clock.advance(299)
    assert service.due() is None
    clock.advance(1)
    assert service.due() == AUTO


def test_a_store_that_is_not_connected_cannot_be_synced(
    container: Container, source: FakeSource
) -> None:
    service = _service(container, source, account=None)
    with pytest.raises(Exception) as refused:
        service.sync(trigger=OPERATOR, correlation_id=CID)
    assert getattr(refused.value, "code", None) == "STORE_CATALOG_STORE_NOT_CONNECTED"
    assert service.status().marketplace_account_id is None
    assert service.listings() == () and service.snapshot().freshness == NONE


def test_one_sync_at_a_time(container: Container, source: FakeSource) -> None:
    source.hold = threading.Event()
    service = _service(container, source)
    worker = threading.Thread(target=service.sync, kwargs={"trigger": AUTO, "correlation_id": CID})
    worker.start()
    try:
        for _ in range(200):
            if service.status().running is not None:
                break
            threading.Event().wait(0.01)
        assert service.status().running is not None
        with pytest.raises(StoreCatalogBusy):
            service.sync(trigger=OPERATOR, correlation_id=CID)
    finally:
        source.hold.set()
        worker.join()
    assert service.status().running is None


def test_an_interrupted_run_is_settled_and_a_finished_run_never_changes(
    container: Container, source: FakeSource
) -> None:
    service = _service(container, source)
    run = service.sync(trigger=AUTO, correlation_id=CID)
    with sqlite3.connect(container.config.database_path) as raw:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "UPDATE store_catalog_sync_runs SET outcome = 'ERROR' WHERE run_id = ?",
                (run.run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM store_catalog_sync_runs")
        # A run an earlier process left RUNNING.
        raw.execute(
            "INSERT INTO store_catalog_sync_runs (run_id, marketplace_key, marketplace_account_id,"
            " trigger, state, contract_version, full_read, pages_read, listings_enumerated,"
            " details_target, details_done, details_failed, correlation_id, started_at)"
            " VALUES ('r-left', 'smartstore', ?, 'AUTO', 'RUNNING', 'v1', 0, 0, 0, 0, 0, 0, 'c',"
            " '2026-09-13 00:00:00.000000')",
            (ACCOUNT,),
        )
    assert service.settle_interrupted() == 1
    assert _rows(
        container, "SELECT state, outcome FROM store_catalog_sync_runs WHERE run_id = 'r-left'"
    ) == [("FINISHED", "INTERRUPTED")]


def test_identities_observations_and_endings_are_append_only(
    container: Container, source: FakeSource
) -> None:
    source.listing("101")
    service = _service(container, source)
    service.sync(trigger=AUTO, correlation_id=CID)
    with sqlite3.connect(container.config.database_path) as raw:
        for statement in (
            "UPDATE store_catalog_listings SET origin_product_no = '999'",
            "DELETE FROM store_catalog_listings",
            "UPDATE store_catalog_observations SET sale_price = 1",
            "DELETE FROM store_catalog_observations",
            "UPDATE store_catalog_field_sets SET fields_json = '{}'",
            "DELETE FROM store_catalog_field_sets",
            "UPDATE store_catalog_memberships SET entries_json = '[]'",
            "DELETE FROM store_catalog_memberships",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(statement)


def test_the_routes_show_the_status_and_refuse_a_store_that_is_not_connected(
    client: TestClient,
) -> None:
    shown = client.get("/api/v1/operate/store-catalog")
    assert shown.status_code == 200
    body = shown.json()
    assert body["status"]["marketplace_account_id"] is None and body["listings"] == []
    refused = client.post("/api/v1/operate/store-catalog/sync", headers={"X-ICBM-Client": "pytest"})
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "STORE_CATALOG_STORE_NOT_CONNECTED"


def test_waking_the_scheduler_starts_the_first_sync_by_itself(
    container: Container, source: FakeSource
) -> None:
    source.listing("101")
    service = _service(container, source)
    scheduler = StoreCatalogScheduler(service, tick_s=3600.0)
    scheduler.start()
    try:
        scheduler.wake()  # what a successful CONNECT or first binding does
        for _ in range(500):
            status = service.status()
            if status.last_run is not None and status.last_run.state == "FINISHED":
                break
            threading.Event().wait(0.01)
    finally:
        scheduler.stop()
    status = service.status()
    assert status.last_run is not None and status.last_run.trigger == CONNECT
    assert status.last_complete is not None and status.listing_count == 1


def test_0067_is_additive_and_round_trips(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)

    def schema() -> dict[str, str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            return dict(
                connection.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
            )

    command.downgrade(alembic_config(url), "0067_store_catalog")
    at_0067 = schema()
    command.downgrade(alembic_config(url), "0066_landed_cost")
    at_0066 = schema()
    added = {name for name in set(at_0067) - set(at_0066) if not name.startswith("sqlite_")}
    tables = {
        "store_catalog_sync_runs",
        "store_catalog_listings",
        "store_catalog_field_sets",
        "store_catalog_memberships",
        "store_catalog_observations",
        "store_catalog_listing_endings",
    }
    assert tables <= added
    assert all(
        name.startswith(("store_catalog_", "ux_store_catalog_", "ix_store_catalog_"))
        for name in added
    )
    # Nothing that existed at 0066 changed.
    assert {name: at_0067[name] for name in at_0066} == at_0066
    command.upgrade(alembic_config(url), "0067_store_catalog")
    assert schema() == at_0067


def test_production_wiring_reads_nothing_without_a_bound_store(container: Container) -> None:
    # The composed owner reads only with the committed CONNECT bearer, for the bound account. A
    # fresh data root has neither: nothing is due and nothing can be synced.
    assert container.store_catalog.account() is None
    assert container.store_catalog.due() is None
    assert container.store_catalog.status().last_run is None
