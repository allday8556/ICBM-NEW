"""M6.5-A fulfillment (ADR-0025 §3, §4): the supplier order the operator placed by hand, and its
tracking.

No provider and no supplier is reached. The orders are ingested through the real Orders owner from
a fake source (the M6-D test's), the registrations, Items, adoptions and audit log are the real
owners'. Every buyer value is synthetic (rule 07 §7.3). Proven here: only an order naming one
canonical Item — MATCHED, or UNMATCHED with an adoption link — and awaiting shipment can get a
supplier order; the record copies that Item, supplier and source once and they never change; every
write is revisioned, appended to history and audited by id; a stale revision is refused; a carrier
is one of the documented codes; a tracking number exists only on a recorded supplier order; and the
history cannot be rewritten or removed.
"""

import sqlite3
from datetime import timedelta

import pytest

from app.capabilities.audit.models import AuditEventType
from app.container import Container
from app.platform.core.errors import InputValidationError, NotFoundError
from app.stages.operate.fulfillment import (
    ADOPTION,
    AWAITING_SUPPLIER_ORDER,
    NOT_FULFILLABLE,
    NOT_PAYED,
    RESOLUTION,
    SUPPLIER_ORDERED,
    TRACKING_CAPTURED,
    FulfillmentConflict,
    FulfillmentService,
    OrderNotFulfillable,
)
from app.stages.operate.order_facts import ChangePage
from app.stages.operate.orders import OPERATOR, OrderSyncService, ShippingCipher
from integrations.marketplaces.smartstore.delivery_companies import DELIVERY_COMPANIES
from tests.integration.operate.test_m6_adoption import FakeFinder, _collected, _found, _run
from tests.integration.operate.test_m6_adoption import _service as _adoptions
from tests.integration.operate.test_m6_orders import (  # noqa: F401 - fixtures
    CID,
    FakeSource,
    _audits,
    _change,
    _facts,
    _service,
    _sync,
    registration,
)
from tests.integration.register.test_m5_registration_foundation import (  # noqa: F401 - fixtures
    MARKET,
    account,
    sources,
    store,
)
from tests.support.product_support import Collections

pytestmark = pytest.mark.integration


def _fulfillment(container: Container) -> FulfillmentService:
    return FulfillmentService(
        db=container.db, clock=container.clock, audit=container.audit, carriers=DELIVERY_COMPANIES
    )


def _ingest(container: Container, *facts: object) -> None:
    source = FakeSource(
        pages=[
            ChangePage(tuple(_change(container, f.product_order_id) for f in facts))  # type: ignore[attr-defined]
        ],
        facts={f.product_order_id: f for f in facts},  # type: ignore[attr-defined]
    )
    _sync(_service(container, source))


def _record(service: FulfillmentService, order_id: str = "po-1", revision: int | None = None):  # type: ignore[no-untyped-def]
    return service.record_supplier_order(
        order_id,
        supplier_order_ref="KM-20261008-001",
        purchase_amount=18000,
        ordered_at=None,
        expected_revision=revision,
        actor="operator",
        correlation_id=CID,
    )


def test_a_matched_order_gets_a_supplier_order_and_tracking(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ingest(container, _facts())
    service = _fulfillment(container)
    awaiting = service.view("po-1")
    assert (awaiting.state, awaiting.basis, awaiting.revision) == (
        AWAITING_SUPPLIER_ORDER,
        RESOLUTION,
        None,
    )
    # The identity the record must copy: the order's own resolution.
    (matched,) = _service(container, FakeSource()).overview().orders

    recorded = _record(service)
    assert (recorded.state, recorded.revision, recorded.currency) == (SUPPLIER_ORDERED, 1, "KRW")
    assert (recorded.item_id, recorded.supplier_key, recorded.source_product_id) == (
        matched.item_id,
        matched.supplier_key,
        matched.source_product_id,
    )
    tracked = service.capture_tracking(
        "po-1",
        carrier_code="CJGLS",
        tracking_number="6123-4567-8901",
        expected_revision=1,
        actor="operator",
        correlation_id=CID,
    )
    assert (tracked.state, tracked.revision, tracked.carrier_name) == (
        TRACKING_CAPTURED,
        2,
        "CJ대한통운",
    )
    assert [entry.action for entry in tracked.history] == ["RECORDED", "TRACKING_CAPTURED"]
    assert service.states(["po-1", "po-unknown"]) == {"po-1": TRACKING_CAPTURED}
    # Audited by product-order id, never by content.
    assert _audits(container, AuditEventType.SUPPLIER_ORDER_RECORDED) == ["po-1"]
    assert _audits(container, AuditEventType.ORDER_TRACKING_CAPTURED) == ["po-1"]
    with sqlite3.connect(container.config.database_path) as raw:
        details = raw.execute(
            "SELECT details_json FROM audit_events WHERE event_type IN"
            " ('SUPPLIER_ORDER_RECORDED', 'ORDER_TRACKING_CAPTURED')"
        ).fetchall()
    assert all("KM-20261008" not in str(d) and "6123" not in str(d) for d in details)


def test_every_write_needs_the_revision_it_read(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ingest(container, _facts())
    service = _fulfillment(container)
    _record(service)
    # A second "first" record, or an amendment from a stale read, is refused.
    with pytest.raises(FulfillmentConflict):
        _record(service)
    amended = _record(service, revision=1)
    assert (amended.revision, [e.action for e in amended.history]) == (2, ["RECORDED", "AMENDED"])
    with pytest.raises(FulfillmentConflict):
        _record(service, revision=1)
    with pytest.raises(FulfillmentConflict):
        service.capture_tracking(
            "po-1",
            carrier_code="CJGLS",
            tracking_number="1",
            expected_revision=1,
            actor="operator",
            correlation_id=CID,
        )


def test_an_order_naming_no_canonical_item_is_never_fulfilled(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    # Another provider product: ICBM did not register it and nothing links it.
    _ingest(container, _facts(original_product_id="mp-other"))
    service = _fulfillment(container)
    view = service.view("po-1")
    assert (view.state, view.reason, view.item_id) == (
        NOT_FULFILLABLE,
        "OPERATE_ORDER_NOT_FULFILLABLE",
        None,
    )
    with pytest.raises(OrderNotFulfillable) as refused:
        _record(service)
    assert refused.value.code == "OPERATE_ORDER_NOT_FULFILLABLE"
    with pytest.raises(NotFoundError):
        _record(service, "po-unknown")


def test_only_an_order_awaiting_shipment_gets_a_supplier_order_or_tracking(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ingest(container, _facts(status="DELIVERING"), _facts("po-2"))
    service = _fulfillment(container)
    assert service.view("po-1").state == NOT_PAYED
    with pytest.raises(OrderNotFulfillable) as refused:
        _record(service)
    assert refused.value.code == "OPERATE_ORDER_NOT_PAYED"
    # Recorded while PAYED, then cancelled: the record can be corrected, tracking cannot be taken.
    _record(service, "po-2")
    _ingest(container, _facts("po-2", status="CANCELED"))
    assert _record(service, "po-2", revision=1).revision == 2
    with pytest.raises(OrderNotFulfillable):
        service.capture_tracking(
            "po-2",
            carrier_code="CJGLS",
            tracking_number="123",
            expected_revision=2,
            actor="operator",
            correlation_id=CID,
        )


def test_tracking_needs_a_documented_carrier_and_a_supplier_order(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ingest(container, _facts())
    service = _fulfillment(container)

    def capture(carrier: str, number: str, revision: int = 1) -> object:
        return service.capture_tracking(
            "po-1",
            carrier_code=carrier,
            tracking_number=number,
            expected_revision=revision,
            actor="operator",
            correlation_id=CID,
        )

    with pytest.raises(FulfillmentConflict) as missing:
        capture("CJGLS", "123")
    assert missing.value.code == "OPERATE_TRACKING_WITHOUT_SUPPLIER_ORDER"
    _record(service)
    for carrier, number, code in (
        ("NOT_A_CARRIER", "123", "OPERATE_TRACKING_CARRIER_UNKNOWN"),
        ("GS더프레시", "123", "OPERATE_TRACKING_CARRIER_UNKNOWN"),
        ("CJGLS", "", "OPERATE_TRACKING_NUMBER_INVALID"),
        ("CJGLS", "12 34", "OPERATE_TRACKING_NUMBER_INVALID"),
        ("CJGLS", "1" * 51, "OPERATE_TRACKING_NUMBER_INVALID"),
    ):
        with pytest.raises(InputValidationError) as invalid:
            capture(carrier, number)
        assert invalid.value.code == code
    for reference, amount in (("", 1), ("x" * 101, 1), ("ok", -1), ("ok", True)):
        with pytest.raises(InputValidationError):
            service.record_supplier_order(
                "po-1",
                supplier_order_ref=reference,
                purchase_amount=amount,  # type: ignore[arg-type]
                ordered_at=None,
                expected_revision=1,
                actor="operator",
                correlation_id=CID,
            )
    with pytest.raises(InputValidationError):
        service.record_supplier_order(
            "po-1",
            supplier_order_ref="ok",
            purchase_amount=1,
            ordered_at=container.clock.now() + timedelta(hours=1),
            expected_revision=1,
            actor="operator",
            correlation_id=CID,
        )
    assert capture("HANJIN", "A1-23").tracking_number == "A1-23"


def test_an_order_of_an_adopted_listing_is_fulfilled_through_its_link(
    container: Container,
    sources: Collections,  # noqa: F811
) -> None:
    item_id = _collected(container, sources, "287")
    adoptions = _adoptions(container, FakeFinder({"KM287": _found("9001")}))
    _run(adoptions)
    source = FakeSource(
        pages=[ChangePage((_change(container),))],
        facts={"po-1": _facts(original_product_id="9001", seller_product_code="KM287")},
    )
    OrderSyncService(
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
    ).sync(trigger=OPERATOR, correlation_id=CID)
    view = _record(_fulfillment(container))
    assert (view.basis, view.item_id, view.source_product_id) == (ADOPTION, item_id, "287")


def test_the_identity_and_the_history_cannot_be_rewritten_or_removed(
    container: Container,
    registration: str,  # noqa: F811
) -> None:
    _ingest(container, _facts())
    _record(_fulfillment(container))
    with sqlite3.connect(container.config.database_path) as raw:
        for statement in (
            "UPDATE operate_supplier_orders SET item_id = 'other', revision = revision + 1",
            "UPDATE operate_supplier_orders SET supplier_key = 'other', revision = revision + 1",
            "UPDATE operate_supplier_orders SET supplier_order_ref = 'x'",
            "DELETE FROM operate_supplier_orders",
            "UPDATE operate_supplier_order_history SET supplier_order_ref = 'x'",
            "DELETE FROM operate_supplier_order_history",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(statement)
