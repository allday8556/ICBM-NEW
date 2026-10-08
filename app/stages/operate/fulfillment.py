"""M6.5 fulfillment (ADR-0025): the supplier order the operator placed by hand, and its tracking.

**Fulfillable order** (§3). A product order is fulfillable only when it names one canonical Item:
its ADR-0023 resolution is ``MATCHED``, or it is ``UNMATCHED`` with an ADR-0024 adoption link.
Anything else is refused and never matched by name. The supplier order copies that Item, supplier
and source product once; they never change (trigger, M65-01).

**Manual supplier order** (M65-02). ICBM never writes to a supplier: the operator orders from the
supplier and records the supplier's order number and the purchase amount here.

**Tracking** (§4). The operator types the carrier and the tracking number in. The carrier is one
of the marketplace's documented codes, injected by the composition root so OPERATE never imports a
marketplace adapter; any other code is refused before anything is stored.

**Delivery read-back** (§6, M6.5-B). Once the order read shows a tracking number the order is
``DISPATCHED``, and ``DELIVERED`` once it shows the delivery completed; the view compares that
tracking with the one captured here, and a wrong-tracking flag is shown for the operator.

**Revisions** (M65-09). Every write carries the revision it read; a stale one is refused. Every
write appends a history entry and is audited by product-order id, never by content.
"""

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, InputValidationError, NotFoundError
from app.platform.db.database import Database
from app.stages.operate.fulfillment_models import SupplierOrder, SupplierOrderEntry
from app.stages.operate.order_models import OrderAdoptionLink, ProductOrder

RESOLUTION: Final = "RESOLUTION"
ADOPTION: Final = "ADOPTION"
KRW: Final = "KRW"

# The order status a supplier order and its tracking need (ADR-0025 §3): 결제완료.
PAYED: Final = "PAYED"

# Fulfillment states, derived (ADR-0025 §8). The dispatch states join with M65-C.
NOT_FULFILLABLE: Final = "NOT_FULFILLABLE"
NOT_PAYED: Final = "NOT_PAYED"
AWAITING_SUPPLIER_ORDER: Final = "AWAITING_SUPPLIER_ORDER"
SUPPLIER_ORDERED: Final = "SUPPLIER_ORDERED"
TRACKING_CAPTURED: Final = "TRACKING_CAPTURED"
DISPATCHED: Final = "DISPATCHED"
DELIVERED: Final = "DELIVERED"
# The documented delivery state and order statuses that mean the parcel arrived (packet D).
DELIVERY_COMPLETED: Final = "DELIVERY_COMPLETION"
ARRIVED_STATUSES: Final = frozenset({"DELIVERED", "PURCHASE_DECIDED"})

RECORDED: Final = "RECORDED"
AMENDED: Final = "AMENDED"
CAPTURED: Final = "TRACKING_CAPTURED"
TRACKING_AMENDED: Final = "TRACKING_AMENDED"

# ICBM policy (ADR-0025 §4): the reference states only a size for a tracking number.
TRACKING_NUMBER: Final = re.compile(r"^[0-9A-Za-z-]{1,50}$")
REFERENCE_MAX: Final = 100
AMOUNT_MAX: Final = 100_000_000
# A supplier order placed "in the future" beyond clock skew is a typing error.
FUTURE_SKEW: Final = timedelta(minutes=5)


class FulfillmentConflict(AppError):
    error_class = ErrorClass.CONFLICT


class OrderNotFulfillable(AppError):
    error_class = ErrorClass.POLICY_BLOCKED


@dataclass(frozen=True)
class SupplierOrderEntryView:
    revision: int
    action: str
    supplier_order_ref: str
    purchase_amount: int
    ordered_at: datetime
    carrier_code: str | None
    tracking_number: str | None
    actor: str
    recorded_at: datetime


@dataclass(frozen=True)
class FulfillmentView:
    product_order_id: str
    state: str
    # Why the order cannot be fulfilled, when it cannot (``state`` NOT_FULFILLABLE / NOT_PAYED).
    reason: str | None
    basis: str | None
    item_id: str | None
    supplier_key: str | None
    source_product_id: str | None
    supplier_order_ref: str | None = None
    purchase_amount: int | None = None
    currency: str | None = None
    ordered_at: datetime | None = None
    carrier_code: str | None = None
    carrier_name: str | None = None
    tracking_number: str | None = None
    tracking_captured_at: datetime | None = None
    revision: int | None = None
    history: tuple[SupplierOrderEntryView, ...] = ()
    # ADR-0025 §6: the delivery as the order read shows it.
    delivery_company: str | None = None
    delivery_company_name: str | None = None
    delivery_tracking_number: str | None = None
    delivery_status: str | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    wrong_tracking_number: bool | None = None
    # Whether the read tracking is the one captured here; None until both exist.
    tracking_matches: bool | None = None


@dataclass(frozen=True)
class _Identity:
    basis: str
    item_id: str
    supplier_key: str
    source_product_id: str


def _identity(order: ProductOrder, link: OrderAdoptionLink | None) -> _Identity | None:
    """The one canonical Item the order names, or None (ADR-0025 §3)."""
    if order.resolution == "MATCHED" and order.item_id and order.supplier_key:
        return _Identity(
            RESOLUTION, order.item_id, order.supplier_key, order.source_product_id or ""
        )
    if order.resolution == "UNMATCHED" and link is not None:
        return _Identity(ADOPTION, link.item_id, link.supplier_key, link.source_product_id)
    return None


def _payable(order: ProductOrder) -> bool:
    return order.status == PAYED and order.claim_type is None


def _state(order: ProductOrder, identity: _Identity | None, record: SupplierOrder | None) -> str:
    if identity is not None and order.tracking_number:
        arrived = order.delivery_status == DELIVERY_COMPLETED or order.status in ARRIVED_STATUSES
        return DELIVERED if arrived else DISPATCHED
    if record is not None:
        return TRACKING_CAPTURED if record.tracking_number else SUPPLIER_ORDERED
    if identity is None:
        return NOT_FULFILLABLE
    return AWAITING_SUPPLIER_ORDER if _payable(order) else NOT_PAYED


class FulfillmentService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        carriers: Mapping[str, str],
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._carriers = dict(carriers)

    # ---------------------------------------------------------------- reads

    def carriers(self) -> Mapping[str, str]:
        return dict(self._carriers)

    def view(self, product_order_id: str) -> FulfillmentView:
        with self._db.read() as session:
            order, link, record = self._load(session, product_order_id)
            return self._view(session, order, link, record)

    def states(self, product_order_ids: Iterable[str]) -> dict[str, str]:
        """Each listed order's fulfillment state, for the 주문관리 list."""
        ids = list(product_order_ids)
        if not ids:
            return {}
        with self._db.read() as session:
            orders = {
                row.product_order_id: row
                for row in session.scalars(
                    select(ProductOrder).where(ProductOrder.product_order_id.in_(ids))
                )
            }
            links = {
                row.product_order_id: row
                for row in session.scalars(
                    select(OrderAdoptionLink).where(OrderAdoptionLink.product_order_id.in_(ids))
                )
            }
            records = {
                row.product_order_id: row
                for row in session.scalars(
                    select(SupplierOrder).where(SupplierOrder.product_order_id.in_(ids))
                )
            }
            return {
                order_id: _state(
                    order,
                    _identity(order, links.get(order_id)),
                    records.get(order_id),
                )
                for order_id, order in orders.items()
            }

    # ---------------------------------------------------------------- writes

    def record_supplier_order(
        self,
        product_order_id: str,
        *,
        supplier_order_ref: str,
        purchase_amount: int,
        ordered_at: datetime | None,
        expected_revision: int | None,
        actor: str,
        correlation_id: str,
    ) -> FulfillmentView:
        """Record, or amend, the supplier order the operator placed by hand (ADR-0025 §3)."""
        reference = supplier_order_ref.strip()
        if not reference or len(reference) > REFERENCE_MAX or not reference.isprintable():
            raise InputValidationError(
                "OPERATE_SUPPLIER_ORDER_REF_INVALID",
                f"the supplier order number is 1–{REFERENCE_MAX} printable characters",
            )
        if isinstance(purchase_amount, bool) or not 0 <= purchase_amount <= AMOUNT_MAX:
            raise InputValidationError(
                "OPERATE_SUPPLIER_ORDER_AMOUNT_INVALID",
                "the purchase amount is a whole number of won",
            )
        now = self._clock.now()
        placed = ordered_at or now
        if placed > now + FUTURE_SKEW:
            raise InputValidationError(
                "OPERATE_SUPPLIER_ORDER_TIME_INVALID", "the supplier order time is in the future"
            )
        with self._db.write() as session:
            order, link, record = self._load(session, product_order_id)
            if record is None:
                if expected_revision is not None:
                    raise FulfillmentConflict(
                        "OPERATE_SUPPLIER_ORDER_STALE",
                        "the supplier order no longer exists as read",
                    )
                identity = self._fulfillable(order, link)
                record = SupplierOrder(
                    product_order_id=product_order_id,
                    marketplace_key=order.marketplace_key,
                    basis=identity.basis,
                    item_id=identity.item_id,
                    supplier_key=identity.supplier_key,
                    source_product_id=identity.source_product_id,
                    supplier_order_ref=reference,
                    purchase_amount=purchase_amount,
                    currency=KRW,
                    ordered_at=placed,
                    revision=1,
                    created_by=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)
                session.flush()
                action = RECORDED
            else:
                self._current(record, expected_revision)
                record.supplier_order_ref = reference
                record.purchase_amount = purchase_amount
                record.ordered_at = placed
                record.revision += 1
                record.updated_at = now
                session.flush()
                action = AMENDED
            self._append(session, record, action, actor, correlation_id, now)
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.SUPPLIER_ORDER_RECORDED,
                    action=f"operate.supplier_order.{action.lower()}",
                    actor=actor,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=product_order_id,
                    correlation_id=correlation_id,
                    details={"revision": record.revision},
                ),
                session=session,
            )
            return self._view(session, order, link, record)

    def capture_tracking(
        self,
        product_order_id: str,
        *,
        carrier_code: str,
        tracking_number: str,
        expected_revision: int,
        actor: str,
        correlation_id: str,
    ) -> FulfillmentView:
        """Capture, or amend, the carrier and tracking number the operator typed in (§4)."""
        if carrier_code not in self._carriers:
            raise InputValidationError(
                "OPERATE_TRACKING_CARRIER_UNKNOWN", "the carrier is not one of the documented codes"
            )
        number = tracking_number.strip()
        if not TRACKING_NUMBER.fullmatch(number):
            raise InputValidationError(
                "OPERATE_TRACKING_NUMBER_INVALID",
                "the tracking number is 1–50 digits, Latin letters or hyphens",
            )
        now = self._clock.now()
        with self._db.write() as session:
            order, link, record = self._load(session, product_order_id)
            if record is None:
                raise FulfillmentConflict(
                    "OPERATE_TRACKING_WITHOUT_SUPPLIER_ORDER",
                    "a tracking number needs a recorded supplier order",
                )
            self._current(record, expected_revision)
            if not _payable(order):
                raise OrderNotFulfillable(
                    "OPERATE_ORDER_NOT_PAYED", "the order is not awaiting shipment (결제완료)"
                )
            action = TRACKING_AMENDED if record.tracking_number else CAPTURED
            record.carrier_code = carrier_code
            record.tracking_number = number
            record.tracking_captured_at = now
            record.revision += 1
            record.updated_at = now
            session.flush()
            self._append(session, record, action, actor, correlation_id, now)
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.ORDER_TRACKING_CAPTURED,
                    action=f"operate.tracking.{action.lower()}",
                    actor=actor,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=product_order_id,
                    correlation_id=correlation_id,
                    details={"revision": record.revision},
                ),
                session=session,
            )
            return self._view(session, order, link, record)

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _load(
        session: Session, product_order_id: str
    ) -> tuple[ProductOrder, OrderAdoptionLink | None, SupplierOrder | None]:
        order = session.get(ProductOrder, product_order_id)
        if order is None:
            raise NotFoundError("OPERATE_ORDER_NOT_FOUND", "no such product order")
        return (
            order,
            session.get(OrderAdoptionLink, product_order_id),
            session.get(SupplierOrder, product_order_id),
        )

    @staticmethod
    def _fulfillable(order: ProductOrder, link: OrderAdoptionLink | None) -> _Identity:
        identity = _identity(order, link)
        if identity is None:
            raise OrderNotFulfillable(
                "OPERATE_ORDER_NOT_FULFILLABLE",
                "the order names no single canonical Item (MATCHED, or adopted and linked)",
            )
        if not _payable(order):
            raise OrderNotFulfillable(
                "OPERATE_ORDER_NOT_PAYED", "the order is not awaiting shipment (결제완료)"
            )
        return identity

    @staticmethod
    def _current(record: SupplierOrder, expected_revision: int | None) -> None:
        if expected_revision != record.revision:
            raise FulfillmentConflict(
                "OPERATE_SUPPLIER_ORDER_STALE",
                "the supplier order changed since it was read; reload it",
                details={"revision": record.revision},
            )

    @staticmethod
    def _append(
        session: Session,
        record: SupplierOrder,
        action: str,
        actor: str,
        correlation_id: str,
        now: datetime,
    ) -> None:
        session.add(
            SupplierOrderEntry(
                entry_id=str(uuid.uuid4()),
                product_order_id=record.product_order_id,
                revision=record.revision,
                action=action,
                supplier_order_ref=record.supplier_order_ref,
                purchase_amount=record.purchase_amount,
                ordered_at=record.ordered_at,
                carrier_code=record.carrier_code,
                tracking_number=record.tracking_number,
                actor=actor,
                correlation_id=correlation_id,
                recorded_at=now,
            )
        )
        session.flush()

    def _view(
        self,
        session: Session,
        order: ProductOrder,
        link: OrderAdoptionLink | None,
        record: SupplierOrder | None,
    ) -> FulfillmentView:
        identity = _identity(order, link)
        state = _state(order, identity, record)
        reason = {
            NOT_FULFILLABLE: "OPERATE_ORDER_NOT_FULFILLABLE",
            NOT_PAYED: "OPERATE_ORDER_NOT_PAYED",
        }.get(state)
        delivery: dict[str, Any] = {
            "delivery_company": order.delivery_company,
            "delivery_company_name": None
            if order.delivery_company is None
            else self._carriers.get(order.delivery_company),
            "delivery_tracking_number": order.tracking_number,
            "delivery_status": order.delivery_status,
            "sent_at": order.sent_at,
            "delivered_at": order.delivered_at,
            "wrong_tracking_number": order.wrong_tracking_number,
        }
        if record is None:
            return FulfillmentView(
                product_order_id=order.product_order_id,
                state=state,
                reason=reason,
                basis=None if identity is None else identity.basis,
                item_id=None if identity is None else identity.item_id,
                supplier_key=None if identity is None else identity.supplier_key,
                source_product_id=None if identity is None else identity.source_product_id,
                **delivery,
            )
        history = tuple(
            SupplierOrderEntryView(
                revision=entry.revision,
                action=entry.action,
                supplier_order_ref=entry.supplier_order_ref,
                purchase_amount=entry.purchase_amount,
                ordered_at=entry.ordered_at,
                carrier_code=entry.carrier_code,
                tracking_number=entry.tracking_number,
                actor=entry.actor,
                recorded_at=entry.recorded_at,
            )
            for entry in session.scalars(
                select(SupplierOrderEntry)
                .where(SupplierOrderEntry.product_order_id == order.product_order_id)
                .order_by(SupplierOrderEntry.revision)
            )
        )
        return FulfillmentView(
            product_order_id=order.product_order_id,
            state=state,
            reason=reason,
            basis=record.basis,
            item_id=record.item_id,
            supplier_key=record.supplier_key,
            source_product_id=record.source_product_id,
            supplier_order_ref=record.supplier_order_ref,
            purchase_amount=record.purchase_amount,
            currency=record.currency,
            ordered_at=record.ordered_at,
            carrier_code=record.carrier_code,
            carrier_name=None
            if record.carrier_code is None
            else self._carriers.get(record.carrier_code),
            tracking_number=record.tracking_number,
            tracking_captured_at=record.tracking_captured_at,
            revision=record.revision,
            history=history,
            tracking_matches=None
            if not (order.tracking_number and record.tracking_number)
            else (order.tracking_number, order.delivery_company)
            == (record.tracking_number, record.carrier_code),
            **delivery,
        )
