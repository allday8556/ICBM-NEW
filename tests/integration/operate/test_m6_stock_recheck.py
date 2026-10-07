"""M6-B supplier stock recheck (ADR-0023 §4): re-collect what is listed, judge it, show it.

No supplier is reached: the collector is a fake standing in for COLLECT's public surface
(``submit`` and ``run``), and the revisions are fakes that state the stock each test says. The
registrations, their frozen Items and the products owner's source binding are the real owners'.
Proven here: only listed source products are targets; a recheck is COLLECT's own submission with
the current revision's source URL; one pending recheck per source; a settled run states the
judged availability; a refusal is recorded and three in a row end the round; a sold-out listed
source is STOCK review work for the operator, and nothing changes a listing.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.capabilities.review.stock_producer import LISTED_SOURCE_SOLD_OUT, StockReviewProducer
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError
from app.stages.collect.facts import Availability, FieldStatus
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.operate.stock import (
    FAILED,
    OPERATOR,
    RECORDED,
    REFUSED,
    StockRecheckService,
)
from app.stages.operate.stock_models import StockRecheck
from app.stages.register.store import RegistrationStore
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
from tests.support.product_support import Collections

pytestmark = pytest.mark.integration

URL = "https://supplier.invalid/product/listed/1/"


def _revision(revision_id: str, availability: Availability, status: FieldStatus) -> Any:
    stock = SimpleNamespace(status=status, value=SimpleNamespace(availability=availability))
    return SimpleNamespace(revision_id=revision_id, source_url=URL, fields={"stock": stock})


@dataclass
class FakeRevisions:
    current: Any = None
    by_id: dict[str, Any] = field(default_factory=dict)

    def current_recorded(self, supplier_key: str, source_product_id: str) -> Any:
        return self.current

    def get(self, revision_id: str) -> Any:
        return self.by_id.get(revision_id)


@dataclass
class FakeCollector:
    submitted: list[tuple[str, str]] = field(default_factory=list)
    runs: dict[str, Any] = field(default_factory=dict)
    refuse: str | None = None

    def submit(self, supplier_key: str, product_url: str) -> Any:
        if self.refuse:
            raise AppError(self.refuse, "refused")
        run_id = f"run-{len(self.submitted) + 1}"
        self.submitted.append((supplier_key, product_url))
        self.runs[run_id] = SimpleNamespace(outcome="PENDING", revision_id=None)
        return SimpleNamespace(collection_run_id=run_id)

    def run(self, collection_run_id: str) -> Any:
        return self.runs[collection_run_id]


@pytest.fixture
def registration(
    container: Container,
    config: AppConfig,
    sources: Collections,  # noqa: F811
    store: RegistrationStore,  # noqa: F811
) -> str:
    item = _priced(container, config, sources)
    intent = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, intent, RemoteOutcome.APPLIED_PROVEN, "mp-stock-1")
    return _confirm(store, intent)


def _service(
    container: Container, collector: FakeCollector, revisions: FakeRevisions
) -> StockRecheckService:
    def identity(item_id: str) -> tuple[str, str] | None:
        with container.product_store.reading() as unit:
            return unit.source_identity_of_item(item_id)

    return StockRecheckService(
        db=container.db,
        clock=container.clock,
        registrations=container.registrations,
        source_identity=identity,
        collector=collector,
        revisions=revisions,
        interval_s=21600,
        cap=20,
        marketplace_key=MARKET,
    )


def test_a_listed_source_is_rechecked_through_collect_and_judged(
    container: Container, registration: str
) -> None:
    revisions = FakeRevisions(
        current=_revision("rev-1", Availability.ON_SALE, FieldStatus.CONFIRMED)
    )
    collector = FakeCollector()
    service = _service(container, collector, revisions)
    (listed,) = service.listed_sources()
    assert listed.registration_ids == (registration,)
    opened = service.request_round(trigger=OPERATOR)
    assert len(opened) == 1 and collector.submitted == [(listed.supplier_key, URL)]
    # One pending recheck per source: a second round opens none while it is pending.
    assert service.request_round(trigger=OPERATOR) == ()
    (state,) = service.overview().sources
    assert state.pending is True and state.needs_decision is False
    # The run records a revision that states SOLD_OUT.
    sold_out = _revision("rev-2", Availability.SOLD_OUT, FieldStatus.CONFIRMED)
    collector.runs["run-1"] = SimpleNamespace(outcome="RECORDED", revision_id="rev-2")
    revisions.by_id["rev-2"] = sold_out
    revisions.current = sold_out
    assert service.settle() == 1
    (state,) = service.overview().sources
    assert (state.pending, state.last_recheck_outcome) == (False, RECORDED)
    assert (state.availability, state.needs_decision) == ("SOLD_OUT", True)


def test_a_sold_out_listed_source_is_stock_review_work(
    container: Container, registration: str
) -> None:
    revisions = FakeRevisions(
        current=_revision("rev-9", Availability.SOLD_OUT, FieldStatus.CONFIRMED)
    )
    service = _service(container, FakeCollector(), revisions)
    producer = StockReviewProducer(service, revisions)
    (scope,) = producer.scopes()
    (condition,) = producer.derive(scope)
    assert (condition.reason_code, condition.source_identity) == (LISTED_SOURCE_SOLD_OUT, "rev-9")
    # On sale, or under review (COLLECT's own condition), it is not this producer's work.
    revisions.current = _revision("rev-10", Availability.ON_SALE, FieldStatus.CONFIRMED)
    assert producer.derive(scope) == ()
    revisions.current = _revision("rev-11", Availability.SOLD_OUT, FieldStatus.REVIEW_REQUIRED)
    assert producer.derive(scope) == ()


def test_refusals_are_recorded_and_a_failed_run_says_so(
    container: Container, registration: str
) -> None:
    revisions = FakeRevisions(
        current=_revision("rev-1", Availability.ON_SALE, FieldStatus.CONFIRMED)
    )
    collector = FakeCollector(refuse="SUPPLIER_SESSION_UNAVAILABLE")
    service = _service(container, collector, revisions)
    assert len(service.request_round(trigger=OPERATOR)) == 1
    (state,) = service.overview().sources
    assert (state.last_recheck_outcome, state.last_recheck_error) == (
        REFUSED,
        "SUPPLIER_SESSION_UNAVAILABLE",
    )
    collector.refuse = None
    service.request_round(trigger=OPERATOR)
    collector.runs["run-1"] = SimpleNamespace(outcome="FAILED", revision_id=None)
    service.settle()
    (state,) = service.overview().sources
    assert (state.last_recheck_outcome, state.last_recheck_error) == (FAILED, "COLLECT_RUN_FAILED")


def test_nothing_is_listed_without_an_active_registration(container: Container) -> None:
    service = _service(container, FakeCollector(), FakeRevisions())
    assert service.listed_sources() == ()
    assert service.request_round(trigger=OPERATOR) == ()


def test_the_pending_slot_is_reserved_before_collect_is_asked(
    container: Container, registration: str
) -> None:
    """GPT audit (PR #248): a request that does not hold the source's one pending slot never asks
    COLLECT, so two requests can never launch two re-collections of one source."""
    revisions = FakeRevisions(
        current=_revision("rev-1", Availability.ON_SALE, FieldStatus.CONFIRMED)
    )
    collector = FakeCollector()
    service = _service(container, collector, revisions)
    (listed,) = service.listed_sources()
    seen: list[str] = []

    def submit(supplier_key: str, product_url: str) -> Any:
        # While COLLECT is being asked, the slot is already held: a second request sends nothing.
        with container.db.read() as session:
            seen.extend(
                session.scalars(select(StockRecheck.state).where(StockRecheck.state != "FINISHED"))
            )
        assert service._request(listed, OPERATOR) is None
        return FakeCollector.submit(collector, supplier_key, product_url)

    collector.submit = submit  # type: ignore[method-assign]
    opened = service.request_round(trigger=OPERATOR)
    assert len(opened) == 1 and len(collector.submitted) == 1
    assert seen == ["SUBMITTING"]
    (state,) = service.overview().sources
    assert state.pending is True


def test_a_reservation_left_by_a_dead_process_is_released_at_startup(
    container: Container, registration: str
) -> None:
    revisions = FakeRevisions(
        current=_revision("rev-1", Availability.ON_SALE, FieldStatus.CONFIRMED)
    )
    collector = FakeCollector()
    service = _service(container, collector, revisions)
    (listed,) = service.listed_sources()

    def crash(supplier_key: str, product_url: str) -> Any:
        raise KeyboardInterrupt

    collector.submit = crash  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        service.request_round(trigger=OPERATOR)
    # The interrupted request finished its own reservation; a reservation a killed process left
    # behind is released at the next start.
    with container.db.write() as session:
        session.add(
            StockRecheck(
                recheck_id="left-behind",
                supplier_key=listed.supplier_key,
                source_product_id=listed.source_product_id,
                trigger=OPERATOR,
                state="SUBMITTING",
                outcome=None,
                collection_run_id=None,
                revision_id=None,
                availability=None,
                error_code=None,
                requested_at=container.clock.now(),
                finished_at=None,
            )
        )
    assert service.settle_interrupted() == 1
    (state,) = service.overview().sources
    assert state.pending is False
