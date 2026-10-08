"""M6.5-C dispatch (ADR-0025 §5): the shipment of one fulfillable product order, a protected write.

**What may be dispatched.** Exactly one product order the Fulfillment owner finds dispatchable now:
fulfillable (one canonical Item), awaiting shipment (``PAYED``, no claim), delivery method
``DELIVERY``, a recorded supplier order with captured tracking, not already shown dispatched by the
order read, and no attempt that blocks it (``dispatch_models.BLOCKING``). The DISPATCH grant binds
that order and its supplier order revision's carrier and tracking number.

**Send-time.** The attempt is opened ``STARTED``, with its exact grant spent, in the one unit the
send-time safety stack admits — execution mode, brake, grant, adopted endpoint, evidence retention
and the attested 주문 판매자 group — before any byte is sent. A refusal is audited and leaves
nothing behind.

**Outcomes.** ``APPLIED_PROVEN`` only when the documented answer lists the order as a success;
``REJECTED`` when it lists it as a failure (its documented code kept); ``NOT_APPLIED_PROVEN`` only
when transmission was provably precluded; ``UNKNOWN`` otherwise. **An UNKNOWN dispatch is never
resent and opens no new grant** (GPT audit, PR #262): nothing observed after it can prove it was
not applied.

**Verification.** After a sent attempt the product order is read back through the adopted detail
read. The attempt's carrier and tracking number confirm it; another tracking is ``CONFLICT``,
shown and never repaired; no tracking with the order still ``PAYED`` shows a ``REJECTED`` attempt
``UNDISPATCHED`` — the only way to a new grant — and proves nothing after any other outcome. A
failed or unreadable read-back records nothing.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.capabilities.live_safety.authority import DispatchUnit
from app.capabilities.live_safety.model import MutationRefused, MutationStage
from app.capabilities.live_safety.store import DispatchBinding, GrantRecord
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, NotFoundError
from app.platform.db.database import Database
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.operate.dispatch_models import DispatchAttempt, blocking_attempt, latest_attempt
from app.stages.operate.fulfillment import awaiting_shipment, fulfillable_identity
from app.stages.operate.fulfillment_models import SupplierOrder
from app.stages.operate.order_facts import ProductOrderFacts
from app.stages.operate.order_models import OrderAdoptionLink, ProductOrder

STARTED: Final = "STARTED"
APPLIED_PROVEN: Final = "APPLIED_PROVEN"
REJECTED: Final = "REJECTED"
NOT_APPLIED_PROVEN: Final = "NOT_APPLIED_PROVEN"
UNKNOWN: Final = "UNKNOWN"

DISPATCH_CONFIRMED: Final = "DISPATCH_CONFIRMED"
UNDISPATCHED: Final = "UNDISPATCHED"
CONFLICT: Final = "CONFLICT"

# How the documented answer listed the order (the caller's ``OrderDispatchResponse.listed``).
LISTED_SUCCESS: Final = "SUCCESS"
LISTED_FAIL: Final = "FAIL"

# The one delivery method a first-vertical dispatch sends (ADR-0025 §5).
DELIVERY: Final = "DELIVERY"
PAYED: Final = "PAYED"
ACTOR: Final = "operate.dispatch"


class DispatchRefused(AppError):
    """The order is not dispatchable now; nothing was granted, opened or sent."""

    error_class = ErrorClass.POLICY_BLOCKED


@dataclass(frozen=True)
class DispatchHandoff:
    """What one dispatch handoff proved. ``remote_outcome`` is never inferred from an exception
    type: only the transmission-precluded whitelist is ``NOT_APPLIED_PROVEN``. ``listed`` is how
    a documented answer listed the order (``SUCCESS``/``FAIL``/``NONE``), when one came back."""

    remote_outcome: RemoteOutcome
    listed: str | None = None
    fail_code: str | None = None
    response_status: int | None = None
    error_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class DispatchSender(Protocol):
    def endpoint_adopted(self) -> bool: ...

    def send(
        self,
        *,
        product_order_id: str,
        carrier_code: str,
        tracking_number: str,
        dispatch_date: datetime,
    ) -> DispatchHandoff: ...


class DispatchAuthority(Protocol):
    """The send-time safety stack's DISPATCH admission (``SafetyStack``)."""

    def admit_dispatch(
        self,
        session: Session,
        *,
        marketplace_key: str,
        marketplace_account_id: str,
        binding: DispatchBinding,
        endpoint_adopted: bool,
        order_group_attested: bool,
        actor: str,
        correlation_id: str,
    ) -> GrantRecord: ...

    def record_refusal(
        self,
        refusal: MutationRefused,
        *,
        stage: MutationStage,
        target_ref: str,
        actor: str,
        correlation_id: str,
    ) -> None: ...


class OrderReader(Protocol):
    """The adopted product-order detail read (``SmartStoreOrderSource``)."""

    def details(self, product_order_ids: Sequence[str]) -> tuple[ProductOrderFacts, ...]: ...


@dataclass(frozen=True)
class DispatchAttemptView:
    attempt_id: str
    product_order_id: str
    attempt_no: int
    grant_id: str
    supplier_order_revision: int
    carrier_code: str
    tracking_number: str
    dispatch_date: datetime
    state: str
    fail_code: str | None
    error_code: str | None
    response_status: int | None
    verification: str | None
    verified_at: datetime | None
    started_at: datetime
    ended_at: datetime | None


def _view(row: DispatchAttempt) -> DispatchAttemptView:
    return DispatchAttemptView(
        attempt_id=row.attempt_id,
        product_order_id=row.product_order_id,
        attempt_no=row.attempt_no,
        grant_id=row.grant_id,
        supplier_order_revision=row.supplier_order_revision,
        carrier_code=row.carrier_code,
        tracking_number=row.tracking_number,
        dispatch_date=row.dispatch_date,
        state=row.state,
        fail_code=row.fail_code,
        error_code=row.error_code,
        response_status=row.response_status,
        verification=row.verification,
        verified_at=row.verified_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


def _not_dispatchable(code: str, message: str) -> DispatchRefused:
    return DispatchRefused(code, message)


def dispatch_unit(
    session: Session,
    product_order_id: str,
    *,
    account_of: Callable[[str], str | None],
) -> DispatchUnit:
    """The exact unit a DISPATCH grant may bind now, or a refusal that names why not."""
    order = session.get(ProductOrder, product_order_id)
    if order is None:
        raise NotFoundError("OPERATE_ORDER_NOT_FOUND", "no such product order")
    link = session.get(OrderAdoptionLink, product_order_id)
    if fulfillable_identity(order, link) is None:
        raise _not_dispatchable(
            "OPERATE_ORDER_NOT_FULFILLABLE", "the order names no single canonical Item"
        )
    if not awaiting_shipment(order):
        raise _not_dispatchable(
            "OPERATE_ORDER_NOT_PAYED", "the order is not awaiting shipment (결제완료)"
        )
    if order.delivery_method != DELIVERY:
        raise _not_dispatchable(
            "OPERATE_DISPATCH_METHOD_UNSUPPORTED", "only a DELIVERY (택배) order is dispatched"
        )
    if order.tracking_number:
        raise _not_dispatchable(
            "OPERATE_ORDER_ALREADY_DISPATCHED", "the order read already shows a tracking number"
        )
    record = session.get(SupplierOrder, product_order_id)
    if record is None or record.carrier_code is None or record.tracking_number is None:
        raise _not_dispatchable(
            "OPERATE_DISPATCH_TRACKING_MISSING",
            "a dispatch sends a recorded supplier order's captured tracking",
        )
    if blocking_attempt(session, product_order_id):
        raise _not_dispatchable(
            "OPERATE_DISPATCH_BLOCKED",
            "a dispatch of this order is in flight, applied, unknown, confirmed or in conflict",
        )
    account = account_of(order.marketplace_key)
    if account is None:
        raise _not_dispatchable(
            "OPERATE_DISPATCH_ACCOUNT_UNBOUND", "no bound marketplace account names the order"
        )
    return DispatchUnit(
        marketplace_key=order.marketplace_key,
        marketplace_account_id=account,
        binding=DispatchBinding(
            product_order_id=product_order_id,
            supplier_order_revision=record.revision,
            carrier_code=record.carrier_code,
            tracking_number=record.tracking_number,
        ),
    )


_STATES: Final = {
    RemoteOutcome.APPLIED_PROVEN: APPLIED_PROVEN,
    RemoteOutcome.NOT_APPLIED_PROVEN: NOT_APPLIED_PROVEN,
    RemoteOutcome.UNKNOWN: UNKNOWN,
}


def _state(handoff: DispatchHandoff) -> str:
    """ADR-0025 §5: a documented answer decides by how it listed the order, nothing else."""
    if handoff.remote_outcome is RemoteOutcome.APPLIED_PROVEN:
        if handoff.listed == LISTED_SUCCESS:
            return APPLIED_PROVEN
        if handoff.listed == LISTED_FAIL:
            return REJECTED
        return UNKNOWN
    return _STATES.get(handoff.remote_outcome, UNKNOWN)


class DispatchService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        sender: DispatchSender,
        authority: DispatchAuthority,
        reader: OrderReader,
        order_group_attested: Callable[[], bool],
        account_of: Callable[[str], str | None],
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._sender = sender
        self._authority = authority
        self._reader = reader
        self._attested = order_group_attested
        self._account_of = account_of

    def unit(self, product_order_id: str) -> DispatchUnit:
        with self._db.read() as session:
            return dispatch_unit(session, product_order_id, account_of=self._account_of)

    def attempts(self, product_order_id: str) -> tuple[DispatchAttemptView, ...]:
        with self._db.read() as session:
            rows = session.scalars(
                select(DispatchAttempt)
                .where(DispatchAttempt.product_order_id == product_order_id)
                .order_by(DispatchAttempt.attempt_no)
            )
            return tuple(_view(row) for row in rows)

    def dispatch(
        self, product_order_id: str, *, actor: str, correlation_id: str
    ) -> DispatchAttemptView:
        """Send one dispatch through the safety stack, then verify it by read-back."""
        now = self._clock.now()
        try:
            with self._db.write() as session:
                unit = dispatch_unit(session, product_order_id, account_of=self._account_of)
                grant = self._authority.admit_dispatch(
                    session,
                    marketplace_key=unit.marketplace_key,
                    marketplace_account_id=unit.marketplace_account_id,
                    binding=unit.binding,
                    endpoint_adopted=self._sender.endpoint_adopted(),
                    order_group_attested=self._attested(),
                    actor=actor,
                    correlation_id=correlation_id,
                )
                number = (
                    session.scalar(
                        select(func.max(DispatchAttempt.attempt_no)).where(
                            DispatchAttempt.product_order_id == product_order_id
                        )
                    )
                    or 0
                ) + 1
                row = DispatchAttempt(
                    attempt_id=str(uuid.uuid4()),
                    product_order_id=product_order_id,
                    attempt_no=number,
                    grant_id=grant.grant_id,
                    marketplace_key=unit.marketplace_key,
                    marketplace_account_id=unit.marketplace_account_id,
                    supplier_order_revision=unit.binding.supplier_order_revision,
                    carrier_code=unit.binding.carrier_code,
                    tracking_number=unit.binding.tracking_number,
                    dispatch_date=now,
                    state=STARTED,
                    actor=actor,
                    correlation_id=correlation_id,
                    started_at=now,
                )
                session.add(row)
                session.flush()
                self._event(session, row, "start", actor, correlation_id)
                attempt_id = row.attempt_id
        except MutationRefused as refusal:
            self._authority.record_refusal(
                refusal,
                stage=MutationStage.DISPATCH,
                target_ref=f"product_order:{product_order_id}",
                actor=actor,
                correlation_id=correlation_id,
            )
            raise
        handoff = self._sender.send(
            product_order_id=product_order_id,
            carrier_code=unit.binding.carrier_code,
            tracking_number=unit.binding.tracking_number,
            dispatch_date=now,
        )
        with self._db.write() as session:
            ended = session.get(DispatchAttempt, attempt_id)
            assert ended is not None
            row = ended
            row.state = _state(handoff)
            row.fail_code = handoff.fail_code if row.state == REJECTED else None
            row.error_code = handoff.error_code
            row.response_status = handoff.response_status
            row.ended_at = self._clock.now()
            session.flush()
            self._event(session, row, "finish", actor, correlation_id)
            state = row.state
        if state != NOT_APPLIED_PROVEN:
            self.verify(product_order_id, actor=actor, correlation_id=correlation_id)
        with self._db.read() as session:
            final = session.get(DispatchAttempt, attempt_id)
            assert final is not None
            return _view(final)

    def verify(
        self, product_order_id: str, *, actor: str, correlation_id: str
    ) -> DispatchAttemptView | None:
        """Read the order back and record what it shows about the latest sent attempt, once."""
        with self._db.read() as session:
            latest = latest_attempt(session, product_order_id)
            if latest is None or latest.verification is not None:
                return None if latest is None else _view(latest)
            if latest.state not in (APPLIED_PROVEN, REJECTED, UNKNOWN):
                return _view(latest)
            attempt_id = latest.attempt_id
        try:
            facts = self._reader.details([product_order_id])
        except Exception:  # a failed or unreadable read-back records nothing (ADR-0025 §5)
            return self._current(attempt_id)
        fact = next((f for f in facts if f.product_order_id == product_order_id), None)
        if fact is None:
            return self._current(attempt_id)
        with self._db.write() as session:
            row = session.get(DispatchAttempt, attempt_id)
            assert row is not None
            if row.verification is not None:
                return _view(row)
            verdict = self._verdict(row, fact)
            if verdict is None:
                return _view(row)
            row.verification = verdict
            row.verified_at = self._clock.now()
            session.flush()
            self._event(session, row, "verify", actor, correlation_id)
            return _view(row)

    @staticmethod
    def _verdict(row: DispatchAttempt, fact: ProductOrderFacts) -> str | None:
        if fact.tracking_number:
            same = (fact.tracking_number, fact.delivery_company) == (
                row.tracking_number,
                row.carrier_code,
            )
            return DISPATCH_CONFIRMED if same else CONFLICT
        # No tracking read: only a provider's per-order failure, with the order still awaiting
        # shipment, shows the order undispatched (ADR-0025 §5, GPT audit PR #262).
        if row.state == REJECTED and fact.status == PAYED:
            return UNDISPATCHED
        return None

    def _current(self, attempt_id: str) -> DispatchAttemptView:
        with self._db.read() as session:
            row = session.get(DispatchAttempt, attempt_id)
            assert row is not None
            return _view(row)

    def _event(
        self, session: Session, row: DispatchAttempt, step: str, actor: str, correlation_id: str
    ) -> None:
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.ORDER_DISPATCH_RECORDED,
                action=f"operate.dispatch.{step}",
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=row.product_order_id,
                correlation_id=correlation_id,
                details={
                    "attempt_no": row.attempt_no,
                    "grant_id": row.grant_id,
                    "state": row.state,
                    "verification": row.verification,
                    "fail_code": row.fail_code,
                },
            ),
            session=session,
        )
