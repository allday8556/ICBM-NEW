"""M6-A listing-state sync (ADR-0023 §3): read every ACTIVE registration back, keep what it showed.

No provider is reached: the reader is a fake that answers what each test says, through the real
SmartStore normalizer. Proven here: observations are appended and the registration's read-back
time moves; drift is shown and never repaired; only provider evidence (DELETE or a 404) makes a
registration EXTERNALLY_REMOVED; a failed read proves nothing; a rate limit ends the run; a
registration ICBM itself deleted is never a target; and one run at a time.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError, ErrorClass
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.operate.listing import (
    COMPLETED,
    COMPLETED_WITH_FAILURES,
    NOT_ON_SALE,
    OPERATOR,
    PRICE_DIFFERS,
    RATE_LIMITED,
    SESSION_UNAVAILABLE,
    STOCK_ZERO,
    ListingSyncService,
)
from app.stages.register.model import RegistrationLifecycle
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
from tests.support.product_support import Collections

pytestmark = pytest.mark.integration

CID = "cid-listing-sync"
PRODUCT = "mp-operate-1"


class FakeReader:
    def __init__(self) -> None:
        self.answer: Any = None
        self.is_available = True
        self.reads: list[str] = []

    def available(self) -> bool:
        return self.is_available

    def read(self, *, marketplace_product_id: str) -> Mapping[str, Any]:
        self.reads.append(marketplace_product_id)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _listing(status: str = "SALE", display: str = "ON", price: int = 25000, stock: int = 1) -> dict:
    return {
        "originProduct": {
            "statusType": status,
            "salePrice": price,
            "stockQuantity": stock,
        },
        "smartstoreChannelProduct": {"channelProductDisplayStatusType": display},
    }


@pytest.fixture
def registration(
    container: Container,
    config: AppConfig,
    sources: Collections,  # noqa: F811
    store: RegistrationStore,  # noqa: F811
) -> str:
    item = _priced(container, config, sources)
    intent = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, intent, RemoteOutcome.APPLIED_PROVEN, PRODUCT)
    return _confirm(store, intent)


def _service(container: Container, reader: FakeReader) -> ListingSyncService:
    return ListingSyncService(
        db=container.db,
        clock=container.clock,
        registrations=container.registrations,
        reader=reader,
        normalize=smartstore_readback.normalize,
        interval_s=1800,
        marketplace_key=MARKET,
    )


def _sync(service: ListingSyncService) -> Any:
    return service.sync(trigger=OPERATOR, correlation_id=CID)


def test_a_listing_on_sale_is_observed_and_its_readback_time_moves(
    container: Container, registration: str
) -> None:
    reader = FakeReader()
    reader.answer = _listing()
    service = _service(container, reader)
    run = _sync(service)
    assert (run.outcome, run.targets, run.observed, run.failed) == (COMPLETED, 1, 1, 0)
    assert reader.reads == [PRODUCT]
    (state,) = service.overview().listings
    assert state.last_result == "OBSERVED"
    assert (state.sale_status, state.display_status, state.stock_quantity) == ("SALE", "ON", 1)
    with sqlite3.connect(container.config.database_path) as raw:
        (seen,) = raw.execute(
            "SELECT last_readback_at FROM marketplace_registrations WHERE registration_id = ?",
            (registration,),
        ).fetchone()
    assert seen is not None


def test_drift_is_shown_and_never_repaired(container: Container, registration: str) -> None:
    reader = FakeReader()
    reader.answer = _listing(status="OUTOFSTOCK", display="SUSPENSION", price=1, stock=0)
    service = _service(container, reader)
    _sync(service)
    (state,) = service.overview().listings
    assert set(state.drift) >= {NOT_ON_SALE, STOCK_ZERO}
    if state.snapshot_sale_price is not None:
        assert PRICE_DIFFERS in state.drift
    # Shown only: the registration stays ACTIVE and nothing but a read happened.
    record = container.registrations.registration(registration)
    assert record is not None and record.lifecycle_state is RegistrationLifecycle.ACTIVE
    assert reader.reads == [PRODUCT]


@pytest.mark.parametrize(
    "answer",
    [
        _listing(status="DELETE"),
        AppError("SMARTSTORE_HTTP_404", "gone", details={"http_status": 404}),
    ],
    ids=["status-delete", "http-404"],
)
def test_only_provider_evidence_removes_a_registration(
    container: Container, registration: str, answer: Any
) -> None:
    reader = FakeReader()
    reader.answer = answer
    service = _service(container, reader)
    _sync(service)
    record = container.registrations.registration(registration)
    assert record is not None and record.lifecycle_state is RegistrationLifecycle.EXTERNALLY_REMOVED
    # An EXTERNALLY_REMOVED registration is no target any more.
    assert _sync(service).targets == 0


def test_a_failed_read_proves_nothing(container: Container, registration: str) -> None:
    reader = FakeReader()
    reader.answer = AppError("SMARTSTORE_HTTP_500", "server", details={"http_status": 500})
    service = _service(container, reader)
    run = _sync(service)
    assert (run.outcome, run.observed, run.failed) == (COMPLETED_WITH_FAILURES, 0, 1)
    (state,) = service.overview().listings
    assert state.last_result == "READ_FAILED" and state.error_code == "SMARTSTORE_HTTP_500"
    record = container.registrations.registration(registration)
    assert record is not None and record.lifecycle_state is RegistrationLifecycle.ACTIVE


def test_a_rate_limit_ends_the_run_and_no_session_reads_nothing(
    container: Container, registration: str
) -> None:
    reader = FakeReader()

    class Limited(AppError):
        error_class = ErrorClass.RATE_LIMITED

    reader.answer = Limited("SMARTSTORE_RATE_LIMITED", "slow down")
    service = _service(container, reader)
    assert _sync(service).outcome == RATE_LIMITED
    reader.is_available = False
    reader.reads.clear()
    assert _sync(service).outcome == SESSION_UNAVAILABLE
    assert reader.reads == []


def test_a_registration_icbm_deleted_is_never_a_target(
    container: Container, registration: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = FakeReader()
    reader.answer = _listing()
    service = _service(container, reader)
    # Without a deletion it is a target.
    assert _sync(service).targets == 1
    # Once ICBM's own deletion is proven (ADR-0018 §3.5), it is not: its absence is ICBM's act.
    deleted = SimpleNamespace(deleted=True, open=True)
    monkeypatch.setattr(
        container.registrations,
        "deletions",
        lambda registration_id: (deleted,) if registration_id == registration else (),
    )
    reader.reads.clear()
    run = _sync(service)
    assert (run.targets, reader.reads) == (0, [])
    (state,) = service.overview().listings
    assert state.deleted_by_icbm is True
    record = container.registrations.registration(registration)
    assert record is not None and record.lifecycle_state is RegistrationLifecycle.ACTIVE


def test_every_registration_is_a_target_whatever_the_count(
    container: Container, registration: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GPT audit (PR #247): no capped window; the enumeration is the store's uncapped read.
    original = container.registrations.marketplace_registrations
    (record,) = original(MARKET)
    many = tuple(replace(record, registration_id=f"r-{n}") for n in range(1200))
    monkeypatch.setattr(container.registrations, "marketplace_registrations", lambda key: many)
    monkeypatch.setattr(container.registrations, "deletions", lambda registration_id: ())
    service = _service(container, FakeReader())
    assert len(service._targets(include_inactive=False)) == 1200


def test_observations_are_append_only(container: Container, registration: str) -> None:
    reader = FakeReader()
    reader.answer = _listing()
    _sync(_service(container, reader))
    with sqlite3.connect(container.config.database_path) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE operate_listing_observations SET sale_price = 1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM operate_listing_observations")


def test_an_unreadable_answer_fails_only_that_registration_and_never_holds_the_run(
    container: Container, registration: str
) -> None:
    # GPT audit (PR #247): a normalizer failure after a successful read must not leave the run
    # RUNNING, or the one-running slot would stop every later sync until a restart.
    reader = FakeReader()
    reader.answer = _listing()

    def broken(_: Mapping[str, Any]) -> Any:
        raise ValueError("unreadable answer")

    service = ListingSyncService(
        db=container.db,
        clock=container.clock,
        registrations=container.registrations,
        reader=reader,
        normalize=broken,
        interval_s=1800,
        marketplace_key=MARKET,
    )
    run = _sync(service)
    assert (run.state, run.outcome, run.failed) == ("FINISHED", COMPLETED_WITH_FAILURES, 1)
    (state,) = service.overview().listings
    assert state.last_result == "READ_FAILED" and state.error_code == "OPERATE_LISTING_UNREADABLE"
    # The slot is free: the next pass runs.
    assert _sync(_service(container, reader)).outcome == COMPLETED


def test_an_escaping_failure_still_finishes_the_run(
    container: Container, registration: str
) -> None:
    reader = FakeReader()
    reader.answer = _listing()
    service = _service(container, reader)

    class Boom(BaseException):
        pass

    def explode(run_id: str, record: Any, correlation_id: str) -> str:
        raise Boom()

    service._visit = explode  # type: ignore[method-assign]
    with pytest.raises(Boom):
        _sync(service)
    last = service.overview().last_run
    assert last is not None and last.outcome == "INTERRUPTED"
    assert _sync(_service(container, reader)).outcome == COMPLETED


def test_the_seller_code_is_compared_as_the_provider_carries_it(
    container: Container, registration: str
) -> None:
    """The read-back carries the provider projection of the listing identity, never the identity
    itself: the comparison is made on the projection (M6-E fix)."""
    from integrations.marketplaces.smartstore.product import seller_management_code

    (record,) = [
        r
        for r in container.registrations.marketplace_registrations(MARKET)
        if r.registration_id == registration
    ]
    projected = seller_management_code(record.seller_product_code)
    reader = FakeReader()
    body = _listing()
    body["smartstoreChannelProduct"]["sellerManagementCode"] = projected
    reader.answer = body
    service = ListingSyncService(
        db=container.db,
        clock=container.clock,
        registrations=container.registrations,
        reader=reader,
        normalize=smartstore_readback.normalize,
        interval_s=1800,
        marketplace_key=MARKET,
        seller_code=seller_management_code,
    )
    _sync(service)
    (state,) = service.overview().listings
    assert "SELLER_CODE_MISMATCH" not in state.drift
    body["smartstoreChannelProduct"]["sellerManagementCode"] = "0" * 30
    _sync(service)
    (state,) = service.overview().listings
    assert "SELLER_CODE_MISMATCH" in state.drift
