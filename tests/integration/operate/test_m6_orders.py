"""M6-D order ingest (ADR-0023 §5, §7): read what changed, keep each product order once, resolve it,
and keep only the shipping record it needs — encrypted.

No provider is reached: the order source is a fake that answers what each test says, in the
values the SmartStore adapter produces. The registrations, their frozen Items, the products
owner's source binding, the audit log and the secret store are the real owners'. Every buyer
value here is synthetic (rule 07 §7.3). Proven here: an order of a registered product resolves to
the frozen Item, its canonical Item and its source; nothing is attached by name; a re-read never
creates a second order or repeats a change; the shipping record is ciphertext with masks only,
opened only by the detail read, and deleted after the retention span and never kept again; only a
window read completely moves the cursor; a rate limit, a missing session and a failure prove
nothing; and the resolution, the history and the order rows cannot be rewritten or removed.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import pytest

from app.capabilities.audit.models import AuditEventType
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError, ErrorClass, RateLimitedError
from app.platform.core.ownership import DataDirInUseError, acquire_data_dir
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.operate.order_facts import (
    ChangePage,
    OrderChange,
    ProductOrderFacts,
    ShippingRecord,
)
from app.stages.operate.orders import (
    COMPLETED,
    CONFLICT,
    CONNECTED,
    DELETED,
    FAILED,
    ITEM_UNMATCHED,
    MATCHED,
    NOT_CONNECTED,
    OPERATOR,
    OVERLAP,
    RATE_LIMITED,
    SESSION_UNAVAILABLE,
    STORED,
    UNMATCHED,
    OrderSyncBusy,
    OrderSyncService,
    ShippingCipher,
    ShippingRecordUnavailable,
    mask_name,
    mask_phone,
)
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

CID = "cid-orders"
PRODUCT = "mp-orders-1"
# Synthetic buyer values only.
SHIP = ShippingRecord(
    recipient_name="가나다",
    phone1="010-0000-1234",
    base_address="테스트시 테스트로 1",
    detail_address="101호",
    zip_code="00000",
    memo="문 앞",
)


@dataclass
class FakeSource:
    pages: list[ChangePage] = field(default_factory=list)
    facts: dict[str, ProductOrderFacts] = field(default_factory=dict)
    is_available: bool = True
    error: Exception | None = None
    windows: list[tuple[datetime, datetime, str | None]] = field(default_factory=list)
    detail_calls: list[tuple[str, ...]] = field(default_factory=list)

    def available(self) -> bool:
        return self.is_available

    def changes(
        self, *, since: datetime, until: datetime, more_sequence: str | None = None
    ) -> ChangePage:
        self.windows.append((since, until, more_sequence))
        if self.error is not None:
            raise self.error
        return self.pages.pop(0) if self.pages else ChangePage(())

    def details(self, product_order_ids: Sequence[str]) -> tuple[ProductOrderFacts, ...]:
        self.detail_calls.append(tuple(product_order_ids))
        return tuple(self.facts[i] for i in product_order_ids if i in self.facts)


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


def _service(
    container: Container, source: FakeSource, retention_days: int = 90
) -> OrderSyncService:
    def identity(item_id: str) -> tuple[str, str] | None:
        with container.product_store.reading() as unit:
            return unit.source_identity_of_item(item_id)

    return OrderSyncService(
        db=container.db,
        clock=container.clock,
        registrations=container.registrations,
        source_identity=identity,
        source=source,
        cipher=ShippingCipher(container.secrets),
        audit=container.audit,
        interval_s=600,
        initial_lookback_s=3 * 86400,
        retention_days=retention_days,
        marketplace_key=MARKET,
    )


def _facts(order_id: str = "po-1", **changes: object) -> ProductOrderFacts:
    base = ProductOrderFacts(
        product_order_id=order_id,
        order_id="o-1",
        status="PAYED",
        original_product_id=PRODUCT,
        quantity=1,
        total_payment_amount=25000,
        delivery_method="DELIVERY",
        shipping=SHIP,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _change(
    container: Container, order_id: str = "po-1", kind: str = "PAYED", minutes: int = 5
) -> OrderChange:
    return OrderChange(
        product_order_id=order_id,
        changed_at=container.clock.now() - timedelta(minutes=minutes),
        change_type=kind,
        status="PAYED",
    )


def _sync(service: OrderSyncService) -> object:
    return service.sync(trigger=OPERATOR, correlation_id=CID)


def _audits(container: Container, event: AuditEventType) -> list[str]:
    with sqlite3.connect(container.config.database_path) as raw:
        rows = raw.execute(
            "SELECT target_ref FROM audit_events WHERE event_type = ? ORDER BY seq", (event.value,)
        ).fetchall()
    return [row[0] for row in rows]


def test_an_order_of_a_registered_product_resolves_to_its_item_and_source(
    container: Container, registration: str
) -> None:
    source = FakeSource(pages=[ChangePage((_change(container),))], facts={"po-1": _facts()})
    service = _service(container, source)
    assert service.capability() == NOT_CONNECTED
    run = _sync(service)
    assert (run.outcome, run.changes, run.orders_read) == (COMPLETED, 1, 1)  # type: ignore[attr-defined]
    assert service.capability() == CONNECTED
    overview = service.overview()
    (order,) = overview.orders
    assert overview.total == 1
    assert (order.resolution, order.registration_id) == (MATCHED, registration)
    assert order.item_id and order.registration_item_key
    assert order.supplier_key and order.source_product_id == "1234"
    # Any label is ICBM's own registration name; no provider product name is kept.
    assert not hasattr(order, "product_name") and not hasattr(order, "tracking_number")
    assert (order.status, order.quantity, order.total_payment_amount) == ("PAYED", 1, 25000)
    assert (order.recipient_masked, order.phone_masked) == ("가*다", "010-****-1234")
    assert order.shipping_state == STORED
    # The database holds ciphertext only: no recipient value is stored in clear anywhere.
    with sqlite3.connect(container.config.database_path) as raw:
        (blob,) = raw.execute("SELECT shipping_ciphertext FROM operate_orders").fetchone()
        dump = "\n".join(raw.iterdump())
    for value in ("가나다", "010-0000-1234", "테스트시 테스트로 1", "101호", "문 앞"):
        assert value.encode("utf-8") not in blob
        assert value not in dump
    assert _audits(container, AuditEventType.ORDER_SHIPPING_STORED) == ["po-1"]


def test_the_shipping_record_opens_only_for_its_order_and_is_audited(
    container: Container, registration: str
) -> None:
    source = FakeSource(pages=[ChangePage((_change(container),))], facts={"po-1": _facts()})
    service = _service(container, source)
    _sync(service)
    record = service.shipping("po-1", actor="operator", correlation_id=CID)
    assert (record.recipient_name, record.zip_code, record.memo) == ("가나다", "00000", "문 앞")
    assert "가나다" not in repr(record)
    assert _audits(container, AuditEventType.ORDER_SHIPPING_OPENED) == ["po-1"]
    with pytest.raises(ShippingRecordUnavailable):
        service.shipping("po-unknown", actor="operator", correlation_id=CID)
    # A record sealed for one order cannot be opened as another's.
    cipher = ShippingCipher(container.secrets)
    assert cipher.open("po-2", cipher.seal("po-1", SHIP)) is None


def test_a_reread_never_creates_a_second_order_or_repeats_a_change(
    container: Container, registration: str
) -> None:
    first = _change(container)
    source = FakeSource(pages=[ChangePage((first,))], facts={"po-1": _facts()})
    service = _service(container, source)
    _sync(service)
    # The next pass overlaps the last one: the same change is listed again beside a new one.
    source.pages = [ChangePage((first, _change(container, kind="DISPATCHED", minutes=1)))]
    source.facts["po-1"] = _facts(status="DELIVERING")
    _sync(service)
    with sqlite3.connect(container.config.database_path) as raw:
        assert raw.execute("SELECT COUNT(*), MAX(status) FROM operate_orders").fetchone() == (
            1,
            "DELIVERING",
        )
        kinds = [r[0] for r in raw.execute("SELECT change_type FROM operate_order_status_history")]
    assert sorted(kinds) == ["DISPATCHED", "PAYED"]
    # The record was sealed once: no address change, nothing resealed.
    assert _audits(container, AuditEventType.ORDER_SHIPPING_STORED) == ["po-1"]


def test_an_address_change_reseals_the_shipping_record(
    container: Container, registration: str
) -> None:
    source = FakeSource(pages=[ChangePage((_change(container),))], facts={"po-1": _facts()})
    service = _service(container, source)
    _sync(service)
    source.pages = [ChangePage((_change(container, kind="DELIVERY_ADDRESS_CHANGED", minutes=1),))]
    moved = replace(SHIP, base_address="테스트시 새로 2")
    source.facts["po-1"] = _facts(shipping=moved)
    _sync(service)
    assert service.shipping("po-1", actor="operator", correlation_id=CID).base_address == (
        "테스트시 새로 2"
    )
    assert _audits(container, AuditEventType.ORDER_SHIPPING_STORED) == ["po-1", "po-1"]


@pytest.mark.parametrize(
    ("changes", "resolution"),
    (
        ({"original_product_id": "mp-somebody-else"}, UNMATCHED),
        ({"original_product_id": None, "channel_product_id": None}, UNMATCHED),
        ({"seller_product_code": "not-the-listing"}, CONFLICT),
        ({"option_manage_code": "no-such-option"}, ITEM_UNMATCHED),
    ),
)
def test_nothing_is_attached_without_its_provider_identities(
    container: Container, registration: str, changes: dict[str, object], resolution: str
) -> None:
    # The product name is the registered product's: a name never resolves anything.
    source = FakeSource(
        pages=[ChangePage((_change(container),))], facts={"po-1": _facts(**changes)}
    )
    service = _service(container, source)
    _sync(service)
    (order,) = service.overview().orders
    assert order.resolution == resolution
    assert order.item_id is None and order.supplier_key is None
    assert (order.registration_id is None) == (resolution == UNMATCHED)


def test_paging_reads_the_whole_window_and_the_cursor_overlaps(
    container: Container, registration: str
) -> None:
    more_from = container.clock.now() - timedelta(minutes=3)
    source = FakeSource(
        pages=[
            ChangePage((_change(container),), more_from=more_from, more_sequence="seq-2"),
            ChangePage((_change(container, "po-2", minutes=2),)),
        ],
        facts={"po-1": _facts(), "po-2": _facts("po-2")},
    )
    service = _service(container, source)
    run = _sync(service)
    assert (run.outcome, run.changes, run.orders_read) == (COMPLETED, 2, 2)  # type: ignore[attr-defined]
    # The first pass looks back the policy span, window by window; the continuation is the
    # provider's own moreFrom and moreSequence.
    first_since = source.windows[0][0]
    assert container.clock.now() - first_since == timedelta(days=3)
    assert (source.windows[1][0], source.windows[1][2]) == (more_from, "seq-2")
    assert source.detail_calls == [("po-1", "po-2")]
    # The next window of the same pass starts an overlap before the last one ended.
    window_starts = [since for since, _until, sequence in source.windows if sequence is None]
    window_ends = [until for _since, until, sequence in source.windows if sequence is None]
    assert len(window_starts) == 4  # 3 days of 24-hour windows, each overlapping the last
    for previous_end, start in zip(window_ends, window_starts[1:], strict=False):
        assert start == previous_end - OVERLAP
    assert window_ends[-1] == container.clock.now()
    synced = run.synced_until  # type: ignore[attr-defined]
    assert synced is not None
    source.windows.clear()
    _sync(service)
    assert source.windows[0][0] == synced - OVERLAP


@pytest.mark.parametrize(
    ("error", "outcome", "code"),
    (
        (RateLimitedError("SMARTSTORE_RATE_LIMITED", "slow down"), RATE_LIMITED, None),
        (AppError("SMARTSTORE_HTTP_403", "refused"), FAILED, "SMARTSTORE_HTTP_403"),
        (ValueError("unreadable"), FAILED, "OPERATE_ORDER_UNREADABLE"),
    ),
)
def test_a_failed_read_proves_nothing_and_moves_no_cursor(
    container: Container, registration: str, error: Exception, outcome: str, code: str | None
) -> None:
    source = FakeSource(error=error)
    service = _service(container, source)
    run = _sync(service)
    assert (run.outcome, run.error_code, run.synced_until) == (outcome, code, None)  # type: ignore[attr-defined]
    assert service.capability() == NOT_CONNECTED
    assert service.overview().total is None
    if isinstance(error, AppError) and error.error_class is ErrorClass.RATE_LIMITED:
        assert len(source.windows) == 1


def test_a_listed_order_the_detail_read_omits_keeps_the_cursor(
    container: Container, registration: str
) -> None:
    """GPT audit (PR #250): only a window read and recorded completely moves the cursor."""
    source = FakeSource(
        pages=[ChangePage((_change(container), _change(container, "po-ghost", minutes=4)))],
        facts={"po-1": _facts()},
    )
    service = _service(container, source)
    run = _sync(service)
    assert (run.outcome, run.error_code, run.synced_until) == (  # type: ignore[attr-defined]
        FAILED,
        "OPERATE_ORDER_DETAIL_MISSING",
        None,
    )
    # What was read is kept; nothing counts as connected until a window is complete.
    assert service.order_count() == 1
    assert service.capability() == NOT_CONNECTED


def test_the_first_pass_looks_back_the_configured_seven_days(config: AppConfig) -> None:
    assert config.operate_order_initial_lookback_s == 7 * 86400


def test_without_a_session_nothing_is_read(container: Container, registration: str) -> None:
    source = FakeSource(is_available=False)
    run = _sync(_service(container, source))
    assert run.outcome == SESSION_UNAVAILABLE  # type: ignore[attr-defined]
    assert source.windows == []


def test_a_terminal_orders_shipping_record_is_deleted_after_retention_and_never_kept_again(
    container: Container, registration: str
) -> None:
    decided = _change(container, kind="PURCHASE_DECIDED")
    source = FakeSource(
        pages=[ChangePage((decided,))], facts={"po-1": _facts(status="PURCHASE_DECIDED")}
    )
    # A zero-day retention cannot be configured; one day, then the clock moves past it.
    service = _service(container, source, retention_days=1)
    _sync(service)
    assert service.purge_shipping(correlation_id=CID) == 0
    container.clock.advance(2 * 86400)  # type: ignore[attr-defined]
    assert service.purge_shipping(correlation_id=CID) == 1
    (order,) = service.overview().orders
    assert (order.shipping_state, order.recipient_masked, order.phone_masked) == (
        DELETED,
        None,
        None,
    )
    assert _audits(container, AuditEventType.ORDER_SHIPPING_DELETED) == ["po-1"]
    # A later read of the same order never stores the record again.
    source.pages = [ChangePage((replace(decided, change_type="CLAIM_REQUESTED"),))]
    _sync(service)
    assert source.detail_calls[-1] == ("po-1",)
    (order,) = service.overview().orders
    assert order.shipping_state == DELETED
    with pytest.raises(ShippingRecordUnavailable):
        service.shipping("po-1", actor="operator", correlation_id=CID)


def test_resolution_history_and_orders_cannot_be_rewritten_or_removed(
    container: Container, registration: str
) -> None:
    source = FakeSource(pages=[ChangePage((_change(container),))], facts={"po-1": _facts()})
    _sync(_service(container, source))
    with sqlite3.connect(container.config.database_path) as raw:
        for statement in (
            "UPDATE operate_orders SET resolution = 'UNMATCHED', registration_id = NULL,"
            " registration_item_key = NULL, item_id = NULL",
            "UPDATE operate_orders SET supplier_key = 'other'",
            "DELETE FROM operate_orders",
            "UPDATE operate_order_status_history SET status = 'CANCELED'",
            "DELETE FROM operate_order_status_history",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(statement)
        # The clear fields stay the owner's to update.
        raw.execute("UPDATE operate_orders SET status = 'DELIVERING'")


def test_masks_keep_only_what_a_list_needs() -> None:
    assert [mask_name(v) for v in ("가", "가나", "가나다", "가나다라", None)] == [
        "*",
        "가*",
        "가*다",
        "가**라",
        None,
    ]
    assert [
        mask_phone(v) for v in ("010-1234-5678", "01012345678", "02-123-4567", "+82 10", None)
    ] == [
        "010-****-5678",
        "010-****-5678",
        "02-***-4567",
        "****",
        None,
    ]


def test_a_second_running_pass_is_refused_as_busy(container: Container, registration: str) -> None:
    """The one running slot is durable: a pass another process holds refuses this one as busy,
    before anything is read."""
    source = FakeSource()
    service = _service(container, source)
    with sqlite3.connect(container.config.database_path) as raw:
        raw.execute(
            "INSERT INTO operate_order_sync_runs (run_id, trigger, state, window_from, changes,"
            " orders_read, correlation_id, started_at) VALUES ('other', 'AUTO', 'RUNNING',"
            " '2026-09-12 00:00:00', 0, 0, 'cid-other', '2026-09-12 00:00:00')"
        )
    with pytest.raises(OrderSyncBusy):
        _sync(service)
    assert source.windows == []


def test_the_orders_screen_counts_only_once_an_order_read_succeeded(
    container: Container, registration: str
) -> None:
    before = container.screens.orders()
    assert (before.orders_total, before.order_read) == (None, NOT_CONNECTED)
    assert before.meta.empty_reason == "NO_CONNECTIONS"
    # The container's own owner, reading through a fake source instead of the provider.
    container.order_sync._source = FakeSource(
        pages=[ChangePage((_change(container),))], facts={"po-1": _facts()}
    )
    container.order_sync._marketplace_key = MARKET
    container.order_sync.sync(trigger=OPERATOR, correlation_id=CID)
    after = container.screens.orders()
    assert (after.orders_total, after.order_read) == (1, CONNECTED)
    assert after.meta.empty_reason is None


def test_startup_settles_only_what_no_live_process_can_hold(
    container: Container, registration: str
) -> None:
    """GPT audit (PR #250): startup finishes RUNNING passes as INTERRUPTED, which is safe only
    because no second process can own this data directory while this one runs (ADR-0006)."""
    with pytest.raises(DataDirInUseError):
        acquire_data_dir(container.config.data_dir, app_version="second-process")
    with sqlite3.connect(container.config.database_path) as raw:
        raw.execute(
            "INSERT INTO operate_order_sync_runs (run_id, trigger, state, window_from, changes,"
            " orders_read, correlation_id, started_at) VALUES ('dead', 'AUTO', 'RUNNING',"
            " '2026-09-12 00:00:00', 0, 0, 'cid-dead', '2026-09-12 00:00:00')"
        )
    service = _service(container, FakeSource())
    assert service.settle_interrupted() == 1
    assert service.overview().last_run.outcome == "INTERRUPTED"  # type: ignore[union-attr]
