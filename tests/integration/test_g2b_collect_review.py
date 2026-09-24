"""The COLLECT / M3 review producer, its recovery and its coverage (Gate 2 G2-B, ADR-0016).

What this proves, over the real application and a real migrated database:
- the reviewed mapping: each REVIEW_REQUIRED condition COLLECT states on a source identity's
  current revision is one item of one kind, and nothing else is (§6);
- the fast path: a RECORDED collection is indexed at once by the real job, and a failure there never
  touches COLLECT's write and is recorded as a known failure (§4);
- derive and apply are one locked unit for a reconciliation too: a COLLECT write cannot land
  between them; a derivation that tries to write is refused at once, never left to hang;
- the safety net: a full pass at startup and a bounded periodic pass, each renewing the watermark
  only when it completed; coverage is current only in this process run, within the freshness bound
  and with no unrecovered failure (§4, §7);
- G2-19: a lost item is recreated exactly once, by the startup pass after a restart and by the
  periodic pass in a running process, with no new owner write in between.

No provider is contacted: the scripted shop knows no host and sends nothing.
"""

import contextlib
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.collect.facts import FieldStatus, ImageIssue, ImageReference, ImageRole
from app.config import AppConfig
from app.container import Container, build_container
from app.core.ownership import DataDirInUseError, acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.db.database import DatabaseWriteReentryError
from app.main import create_app
from app.review.collect_producer import COLLECT_PRODUCER, conditions_of
from app.review.coverage import (
    REVIEW_COVERAGE_NO_PASS_THIS_RUN,
    REVIEW_COVERAGE_STALE,
    REVIEW_INDEX_FAILURE_UNRECOVERED,
)
from app.review.model import ReviewCondition, ReviewKind, ReviewState
from app.review.owner import ReviewItemStore
from integrations.suppliers.collection import CollectionProfile, DocumentView, ReadKind
from integrations.suppliers.transport.collection import RequestBudget
from scripts.m3collect.fake_shop import StubSessions, document, page
from tests.collect_submit_support import (
    CLIENT,
    ScriptedShop,
    product_url,
    registered,
    served,
    settled_and_job_terminal,
)
from tests.conftest import LOCAL, make_config
from tests.product_support import Collections, product, raw, review, unknown_shipping
from tests.support import FakeClock

pytestmark = pytest.mark.integration

REVIEW = "/api/v1/review/items"
STOCK_REASON = "SOURCE_STOCK_REVIEW_REQUIRED"


class UnclearShop(ScriptedShop):
    """The scripted shop, but every product page leaves its stock unclear: REVIEW_REQUIRED."""

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView:
        budget.reserve(kind, url)
        number = [part for part in url.split("/") if part][-1]
        self.reads.append(number)
        self.document_reads += 1
        return document(page(product_id=number, stock_unclear=True))


@contextlib.contextmanager
def running(config: AppConfig, clock: FakeClock) -> Iterator[Container]:
    """One process on the data directory, composed but not serving: no lifespan runs."""
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config, ownership=lease, clock=clock, secret_store=MemorySecretStore()
        )
        try:
            yield built
        finally:
            built.db.dispose()


def open_items(config: AppConfig) -> list[tuple[str, str, str, str]]:
    with contextlib.closing(raw(config)) as connection:
        return [
            (r[0], r[1], r[2], r[3])
            for r in connection.execute(
                "SELECT kind, subject, reason_code, source_identity FROM review_items"
                " WHERE state = 'OPEN' ORDER BY subject"
            )
        ]


def revisions_count(config: AppConfig) -> int:
    with contextlib.closing(raw(config)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM product_facts_revisions").fetchone()[0])


def coverage_of(api: TestClient, supplier: str, product_id: str) -> dict[str, Any]:
    listed = api.get(
        REVIEW, params={"supplier_key": supplier, "source_product_id": product_id}, headers=CLIENT
    ).json()
    (collect,) = [c for c in listed["coverage"] if c["producer"] == COLLECT_PRODUCER]
    return dict(collect)


def collect_unclear(api: TestClient, number: str) -> dict[str, Any]:
    submitted = api.post(
        "/api/v1/collect/collections",
        json={"supplier_key": "fakeshop", "product_url": product_url(number)},
        headers=CLIENT,
    )
    assert submitted.status_code == 202, submitted.text
    return settled_and_job_terminal(api, submitted.json()["collection_run_id"])


def wait_for(predicate: Any, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never held")
        time.sleep(0.05)


# ---------------------------------------------------------------- the reviewed mapping (§6)


def test_the_mapping_indexes_what_collect_states_and_nothing_else(
    container: Container, config: AppConfig
) -> None:
    sources = Collections.of(container, config)
    failed_detail = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=3,
        host="img.fakeshop.example",
        provenance="img.detail@src",
        status=FieldStatus.REVIEW_REQUIRED,
        issue=ImageIssue.FETCH_FAILED,
    )
    _, revision = sources.collect(
        product(stock=review("#stock"), shipping=unknown_shipping()),
        source_product_id="555",
        extra_images=(failed_detail,),
    )
    found = {(c.kind, c.subject, c.reason_code) for c in conditions_of(revision)}
    assert found == {
        (ReviewKind.STOCK, "field:stock", STOCK_REASON),
        (ReviewKind.COLLECT_EVIDENCE, "field:shipping", "SOURCE_FIELD_REVIEW_REQUIRED"),
        (ReviewKind.COLLECT_EVIDENCE, "image:DETAIL:3", "SOURCE_IMAGE_FETCH_FAILED"),
        (ReviewKind.COLLECT_EVIDENCE, "images:detail", "SOURCE_IMAGE_DETAIL_MISSING"),
    }
    assert {c.source_identity for c in conditions_of(revision)} == {revision.revision_id}
    assert {tuple(sorted(c.scope.items())) for c in conditions_of(revision)} == {
        (("source_product_id", "555"), ("supplier_key", "kmretail"))
    }
    # A CONFIRMED revision states nothing to review.
    _, confirmed = sources.collect(product(), source_product_id="556")
    assert conditions_of(confirmed) == ()


def test_only_a_recorded_revision_is_current(container: Container, config: AppConfig) -> None:
    sources = Collections.of(container, config)
    _, first = sources.collect(product(stock=review("#stock")), source_product_id="600")
    # A newer revision whose run has not settled RECORDED is not current yet.
    with container.db.write() as session:
        run_id = sources._runs.open(
            session,
            job_id="job-x",
            correlation_id="cid",
            supplier_key="kmretail",
            source_url="https://kmretail.example/p/600",
        )
    from tests.collect_support import collected

    sources._revisions.append(
        collected(
            fields=product(),
            images=sources._images,
            source_product_id="600",
            collection_run_id=run_id,
        )
    )
    current = container.revisions.current_recorded("kmretail", "600")
    assert current is not None and current.revision_id == first.revision_id
    assert ("kmretail", "600") in container.revisions.recorded_sources()


def test_a_recollection_supersedes_the_old_item(container: Container, config: AppConfig) -> None:
    sources = Collections.of(container, config)
    _, first = sources.collect(product(stock=review("#stock")), source_product_id="700")
    container.review_reconciler.index_scope(COLLECT_PRODUCER, scope("700"))
    _, second = sources.collect(product(stock=review("#stock")), source_product_id="700")
    container.review_reconciler.index_scope(COLLECT_PRODUCER, scope("700"))
    items = container.review_items.items(scope=scope("700"))
    states = {i.source_identity: i.state for i in items}
    assert states == {
        first.revision_id: ReviewState.SUPERSEDED,
        second.revision_id: ReviewState.OPEN,
    }


def scope(product_id: str, supplier: str = "kmretail") -> dict[str, str]:
    return {"supplier_key": supplier, "source_product_id": product_id}


# ---------------------------------------------------------------- the fast path (§4)


def test_a_recorded_collection_is_indexed_by_the_real_job(config: AppConfig) -> None:
    with served(config, UnclearShop()) as api:
        run = collect_unclear(api, "4242")
        assert run["outcome"] == "RECORDED" and run["facts_status"] == "REVIEW_REQUIRED"
        listed = api.get(
            REVIEW, params={"supplier_key": "fakeshop", "source_product_id": "4242"}, headers=CLIENT
        ).json()
    open_ = [(i["kind"], i["subject"]) for i in listed["items"] if i["state"] == "OPEN"]
    assert ("STOCK", "field:stock") in open_
    assert {i["source_identity"] for i in listed["items"]} == {run["revision_id"]}


def test_a_fast_path_failure_never_touches_collect_and_is_recorded(config: AppConfig) -> None:
    with served(config, UnclearShop()) as api:
        container: Container = api.app.state.container  # type: ignore[attr-defined]
        assert coverage_of(api, "fakeshop", "4242")["current"] is True
        fail_next_reconcile(container)
        run = collect_unclear(api, "4242")
        # COLLECT's own work is untouched: the run is RECORDED and its job SUCCEEDED.
        assert run["outcome"] == "RECORDED"
        job = api.get(f"/api/v1/system/jobs/{run['job_id']}", headers=CLIENT).json()
        assert job["state"] == "SUCCEEDED"
        assert open_items(config) == []
        cover = coverage_of(api, "fakeshop", "4242")
        assert (cover["current"], cover["reason"]) == (False, REVIEW_INDEX_FAILURE_UNRECOVERED)


def fail_next_reconcile(container: Container) -> None:
    """The next reconciliation of this process crashes, once."""
    store = container.review_items
    original = store.reconcile
    armed = {"on": True}

    def crashing(producer: str, **kwargs: Any) -> Any:
        if armed.pop("on", False):
            raise RuntimeError("the index step crashed")
        return original(producer, **kwargs)

    store.reconcile = crashing  # type: ignore[method-assign]


# ---------------------------------------------------------------- G2-19 recovery proofs


def test_a_restart_recreates_a_lost_item_exactly_once(config: AppConfig) -> None:
    """Owner commit succeeds → the index step crashes → restart with no new owner write → the
    startup full pass recreates the missing OPEN item exactly once."""
    with served(config, UnclearShop()) as api:
        container: Container = api.app.state.container  # type: ignore[attr-defined]
        fail_next_reconcile(container)
        run = collect_unclear(api, "4242")
        assert run["outcome"] == "RECORDED"
        assert open_items(config) == []
    revisions = revisions_count(config)
    with served(config, UnclearShop()) as restarted:
        recovered = open_items(config)
        assert revisions_count(config) == revisions  # no new owner write
        assert recovered.count(("STOCK", "field:stock", STOCK_REASON, run["revision_id"])) == 1
        cover = coverage_of(restarted, "fakeshop", "4242")
        assert (cover["current"], cover["reason"]) == (True, None)
    # And exactly once: a further restart finds everything in place and changes nothing.
    with served(config, UnclearShop()):
        assert open_items(config) == recovered
    with contextlib.closing(raw(config)) as connection:
        recovered_events = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'REVIEW_COVERAGE_RECOVERED'"
        ).fetchone()[0]
    assert recovered_events == 1


def test_a_running_process_recreates_a_lost_item_by_its_periodic_pass(
    tmp_path: Path, data_dir: Path
) -> None:
    """Owner commit succeeds → the index step fails → the process keeps running with no new owner
    write and no re-evaluation → the next periodic full pass recreates the item exactly once; until
    then the coverage is not current."""
    config = make_config(data_dir, review_reconcile_interval_s=0.3, review_coverage_max_age_s=30.0)
    with served(config, UnclearShop()) as api:
        container: Container = api.app.state.container  # type: ignore[attr-defined]
        gate = threading.Event()
        periodic = container.review_reconciler.full_passes

        def gated_periodic() -> Any:
            gate.wait(10)
            return periodic()

        container.review_reconciler.full_passes = gated_periodic  # type: ignore[method-assign]
        fail_next_reconcile(container)
        run = collect_unclear(api, "4242")
        revisions = revisions_count(config)
        assert open_items(config) == []
        assert coverage_of(api, "fakeshop", "4242")["reason"] == REVIEW_INDEX_FAILURE_UNRECOVERED
        gate.set()  # the periodic loop, and nothing else, may run now
        wait_for(lambda: coverage_of(api, "fakeshop", "4242")["current"] is True)
        items = open_items(config)
        assert items.count(("STOCK", "field:stock", STOCK_REASON, run["revision_id"])) == 1
        assert revisions_count(config) == revisions
        time.sleep(0.8)  # further periodic passes change nothing
        assert open_items(config) == items


# ---------------------------------------------------------------- coverage (§7)


def test_the_watermark_moves_only_after_a_complete_pass(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    sources = Collections.of(container, config)
    sources.collect(product(stock=review("#stock")), source_product_id="801")
    sources.collect(product(stock=review("#stock")), source_product_id="802")
    reconciler = container.review_reconciler
    assert reconciler.coverage()[0].reason == REVIEW_COVERAGE_NO_PASS_THIS_RUN
    store = container.review_items
    original = store.reconcile

    def failing_on_801(producer: str, **kwargs: Any) -> Any:
        if kwargs["scope"] == scope("801"):
            raise RuntimeError("the index step crashed")
        return original(producer, **kwargs)

    store.reconcile = failing_on_801  # type: ignore[method-assign]
    result = reconciler.full_pass(COLLECT_PRODUCER)
    assert result.failed == ("REVIEW_INDEX_FAILED",) and result.scopes == 2
    # Forward progress: the other scope was indexed in its own unit; the watermark did not move.
    assert [s for _, s, _, _ in open_items(config)] == ["field:stock"]
    cover = reconciler.coverage()[0]
    assert (cover.current, cover.reason, cover.full_passes) == (
        False,
        REVIEW_INDEX_FAILURE_UNRECOVERED,
        0,
    )
    store.reconcile = original  # type: ignore[method-assign]
    clock.advance(1)
    assert reconciler.full_pass(COLLECT_PRODUCER).complete
    cover = reconciler.coverage()[0]
    assert (cover.current, cover.full_passes) == (True, 1)
    assert len(open_items(config)) == 2


def test_a_failure_during_a_pass_is_not_cleared_by_that_pass_even_at_the_same_instant(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    """Review 5807477351 B2. The clock is frozen: a failure recorded while a pass runs carries the
    pass's own start time. That pass completes, yet it must not clear the failure nor publish
    current coverage; only a later pass, one that began after the failure, recovers it."""
    Collections.of(container, config).collect(
        product(stock=review("#stock")), source_product_id="811"
    )
    reconciler = container.review_reconciler
    store = container.review_items
    original = store.reconcile

    def with_a_concurrent_failure(producer: str, **kwargs: Any) -> Any:
        # A fast-path failure lands while this pass runs, at the very same clock value.
        reconciler._record_failure(producer, "REVIEW_INDEX_FAILED", "cid-fast-path")
        return original(producer, **kwargs)

    store.reconcile = with_a_concurrent_failure  # type: ignore[method-assign]
    assert reconciler.full_pass(COLLECT_PRODUCER).complete
    cover = reconciler.coverage()[0]
    assert (cover.current, cover.reason) == (False, REVIEW_INDEX_FAILURE_UNRECOVERED)
    store.reconcile = original  # type: ignore[method-assign]
    # The clock is still frozen: a later pass recovers it because it began after the failure.
    assert reconciler.full_pass(COLLECT_PRODUCER).complete
    cover = reconciler.coverage()[0]
    assert (cover.current, cover.reason) == (True, None)


def test_shutdown_waits_for_an_in_flight_periodic_pass(data_dir: Path) -> None:
    """Review 5807477351 B1. A periodic pass is held inside its locked unit when shutdown starts.
    Shutdown must not finish — the owner lease stays held, so no other process can take the data
    root — until that pass has actually ended; then it completes cleanly."""
    config = make_config(data_dir, review_reconcile_interval_s=0.2, review_coverage_max_age_s=30.0)
    app = create_app(
        config,
        collection_gateway=UnclearShop(),
        collection_sessions=StubSessions(),
        collections=(registered(),),
    )
    client = TestClient(app, base_url=LOCAL)
    client.__enter__()
    gate, entered = threading.Event(), threading.Event()
    shutdown: threading.Thread | None = None
    try:
        collect_unclear(client, "4242")  # a scope for the periodic pass to visit
        container: Container = app.state.container
        producer = container.review_items.producer(COLLECT_PRODUCER)
        derive = producer.derive

        def held_in_the_periodic_thread(scope_: Mapping[str, str]) -> Sequence[ReviewCondition]:
            if threading.current_thread().name == "icbm-review-reconciler":
                entered.set()
                gate.wait(30)
            return derive(scope_)

        producer.derive = held_in_the_periodic_thread  # type: ignore[method-assign]
        assert entered.wait(10), "no periodic pass started"
        shutdown = threading.Thread(target=client.__exit__, args=(None, None, None))
        shutdown.start()
        shutdown.join(1.0)
        assert shutdown.is_alive(), "shutdown returned while a pass could still write"
        with pytest.raises(DataDirInUseError):
            acquire_data_dir(config.data_dir, app_version="intruder").release()
        gate.set()
        shutdown.join(30)
        assert not shutdown.is_alive()
    finally:
        gate.set()
        if shutdown is None:
            client.__exit__(None, None, None)
        elif shutdown.is_alive():
            shutdown.join(30)
    # Only now is the data root free, and nothing is left writing to it.
    acquire_data_dir(config.data_dir, app_version="next").release()


def test_coverage_is_bounded_by_freshness_and_by_the_process_run(
    config: AppConfig, clock: FakeClock
) -> None:
    with running(config, clock) as first:
        first.review_reconciler.full_passes()
        assert first.review_reconciler.coverage()[0].current is True
        clock.advance(config.review_coverage_max_age_s + 1)
        cover = first.review_reconciler.coverage()[0]
        assert (cover.current, cover.reason) == (False, REVIEW_COVERAGE_STALE)
        first.review_reconciler.full_passes()
        assert first.review_reconciler.coverage()[0].current is True
    # A new process run is never current on the previous run's watermark.
    with running(config, clock) as second:
        cover = second.review_reconciler.coverage()[0]
        assert (cover.current, cover.reason) == (False, REVIEW_COVERAGE_NO_PASS_THIS_RUN)


def test_a_config_without_a_finite_freshness_bound_is_refused(data_dir: Path) -> None:
    from app.config import ConfigError

    for interval, bound in ((0.0, 10.0), (10.0, 10.0), (10.0, float("inf"))):
        with pytest.raises(ConfigError):
            make_config(
                data_dir, review_reconcile_interval_s=interval, review_coverage_max_age_s=bound
            )


# ---------------------------------------------------------------- derive and apply, locked


def test_a_collect_write_cannot_slip_between_derive_and_apply(
    container: Container, config: AppConfig
) -> None:
    """The owner of truth writes while the producer derives; it waits, and the reconciliation
    applies exactly what it derived before that write commits."""
    sources = Collections.of(container, config)
    sources.collect(product(stock=review("#stock")), source_product_id="901")
    producer = container.review_items.producer(COLLECT_PRODUCER)
    derive = producer.derive
    committed = threading.Event()
    passed: list[bool] = []

    def owner_write() -> None:
        sources.collect(product(), source_product_id="901")  # the stock becomes clear
        committed.set()

    writer = threading.Thread(target=owner_write)

    def derive_while_collect_writes(scope_: Mapping[str, str]) -> Sequence[ReviewCondition]:
        derived = derive(scope_)
        writer.start()
        writer.join(timeout=0.5)
        passed.append(committed.is_set())
        return derived

    producer.derive = derive_while_collect_writes  # type: ignore[method-assign]
    result = container.review_items.reconcile(
        COLLECT_PRODUCER, scope=scope("901"), correlation_id="cid"
    )
    writer.join(timeout=10)
    assert passed == [False] and committed.is_set()
    assert len(result.opened) == 1  # what was derived under the lock, before the write
    producer.derive = derive  # type: ignore[method-assign]
    later = container.review_items.reconcile(
        COLLECT_PRODUCER, scope=scope("901"), correlation_id="cid"
    )
    assert later.resolved == result.opened  # the newer truth is applied on the next pass


def test_a_derivation_that_writes_fails_closed_and_never_hangs(
    container: Container, config: AppConfig
) -> None:
    Collections.of(container, config).collect(
        product(stock=review("#stock")), source_product_id="950"
    )
    producer = container.review_items.producer(COLLECT_PRODUCER)

    def writing_derive(scope_: Mapping[str, str]) -> Sequence[ReviewCondition]:
        with container.db.write():
            pass
        return ()

    producer.derive = writing_derive  # type: ignore[method-assign]
    outcome: dict[str, BaseException | None] = {}

    def attempt() -> None:
        try:
            container.review_items.reconcile(
                COLLECT_PRODUCER, scope=scope("950"), correlation_id="c"
            )
            outcome["error"] = None
        except BaseException as exc:  # the test inspects exactly what came back
            outcome["error"] = exc

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "a re-entrant write hung instead of failing closed"
    assert isinstance(outcome["error"], DatabaseWriteReentryError)
    assert outcome["error"].code == "DATABASE_WRITE_REENTRANT"
    assert open_items(config) == []
    # The coordinator was released: the next write proceeds, and a full pass records the failure.
    result = container.review_reconciler.full_pass(COLLECT_PRODUCER)
    assert result.failed == ("DATABASE_WRITE_REENTRANT",)
    assert container.review_reconciler.coverage()[0].reason == REVIEW_INDEX_FAILURE_UNRECOVERED


# ---------------------------------------------------------------- the resolve route (§5, §8)


def test_the_resolve_route_keeps_a_persisting_condition_open(config: AppConfig) -> None:
    with served(config, UnclearShop()) as api:
        collect_unclear(api, "4242")
        listed = api.get(
            REVIEW, params={"supplier_key": "fakeshop", "source_product_id": "4242"}, headers=CLIENT
        ).json()
        (item,) = [i for i in listed["items"] if i["subject"] == "field:stock"]
        body = {
            "expected_scope": item["scope"],
            "expected_generation": item["generation"],
            "disposition": "FOLLOW_UP_REQUIRED",
            "note": "재수집 예정",
            "actor": "operator",
        }
        resolved = api.post(f"{REVIEW}/{item['review_item_id']}/resolve", json=body, headers=CLIENT)
        assert resolved.status_code == 200, resolved.text
        answer = resolved.json()
        assert (answer["outcome"], answer["replayed"], answer["item"]["state"]) == (
            "CONDITION_PERSISTS",
            False,
            "OPEN",
        )
        replay = api.post(f"{REVIEW}/{item['review_item_id']}/resolve", json=body, headers=CLIENT)
        assert replay.json()["replayed"] is True
        refused = {
            "scope": {**body, "expected_scope": {**item["scope"], "source_product_id": "9"}},
            "stale": {**body, "expected_generation": 2},
            "extra": {**body, "state": "RESOLVED"},
        }
        codes = {
            name: api.post(f"{REVIEW}/{item['review_item_id']}/resolve", json=b, headers=CLIENT)
            for name, b in refused.items()
        }
        assert codes["scope"].status_code == 409
        assert codes["scope"].json()["error"]["code"] == "REVIEW_ITEM_SCOPE_MISMATCH"
        assert codes["stale"].status_code == 409
        assert codes["extra"].status_code == 422


def test_the_database_refuses_a_backward_watermark(container: Container, config: AppConfig) -> None:
    container.review_reconciler.full_passes()
    with contextlib.closing(raw(config)) as connection:
        for message, statement in {
            "never moves backwards": (
                "UPDATE review_coverage SET watermark_at = '2000-01-01 00:00:00'"
            ),
            "never fall": "UPDATE review_coverage SET full_passes = 0",
            "never deleted": "DELETE FROM review_coverage",
        }.items():
            with pytest.raises(sqlite3.IntegrityError, match=message):
                connection.execute(statement)


def test_the_review_store_is_owned_by_the_container(container: Container) -> None:
    assert isinstance(container.review_items, ReviewItemStore)
    assert container.review_items.producers == (COLLECT_PRODUCER,)
