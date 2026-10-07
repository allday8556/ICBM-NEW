"""M6-D order ingest (ADR-0023 §5, §7): read what changed, keep each product order once, resolve it
to the canonical product, and keep only the shipping record it needs — encrypted.

**Read only** (M6-01). The only provider calls are the two adopted order reads behind
:class:`~app.stages.operate.order_facts.OrderSource`; nothing here writes the marketplace.

**Windows and the cursor.** A pass reads the change listing window by window (at most 24 hours
each, the documented default span) from the latest point an earlier pass read completely, minus
an overlap, so a late change is never missed. Only a window read completely moves the cursor; a
rate limit or a failure ends the pass and the next one starts again from there. The first pass
looks back a policy span. One pass at a time.

**Identity.** The provider product-order id is the key (M6-09): a re-read updates the clear
fields and appends only a change not seen before. An order resolves through its origin product
to ICBM's registration, through its option code (or the only Item) to the frozen registration
Item, and from there to the canonical Item and its source (M6-08). Nothing is ever attached by
name; what does not resolve is kept as ``UNMATCHED``/``ITEM_UNMATCHED``, and a seller code that
contradicts the registration is ``CONFLICT``. The resolution is immutable once recorded.

**The shipping record** (ADR-0023 §7) is sealed with AES-256-GCM under a key held by the OS
secret store; the database holds ciphertext and the list masks only. It is opened only for the
order detail view, and deleted 90 days (a policy value) after the order reached a terminal state.
What is stored, opened and deleted is audited by product-order id, never by content.
"""

import base64
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Final, TypeVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass
from app.platform.core.secrets import SecretStore
from app.platform.db.database import Database
from app.stages.operate.order_facts import (
    OrderChange,
    OrderSource,
    ProductOrderFacts,
    ShippingRecord,
)
from app.stages.operate.order_models import OrderStatusChange, OrderSyncRun, ProductOrder
from app.stages.register.store import RegistrationRecord, RegistrationStore

logger = logging.getLogger("icbm.operate.orders")

AUTO: Final = "AUTO"
OPERATOR: Final = "OPERATOR"
RUNNING: Final = "RUNNING"
FINISHED: Final = "FINISHED"
COMPLETED: Final = "COMPLETED"
SESSION_UNAVAILABLE: Final = "SESSION_UNAVAILABLE"
RATE_LIMITED: Final = "RATE_LIMITED"
FAILED: Final = "FAILED"
INTERRUPTED: Final = "INTERRUPTED"

MATCHED: Final = "MATCHED"
UNMATCHED: Final = "UNMATCHED"
ITEM_UNMATCHED: Final = "ITEM_UNMATCHED"
CONFLICT: Final = "CONFLICT"

STORED: Final = "STORED"
NONE: Final = "NONE"
DELETED: Final = "DELETED"

# ADR-0023 §5: the order read capability is proven by a read, never declared.
NOT_CONNECTED: Final = "NOT_CONNECTED"
CONNECTED: Final = "CONNECTED"

# ADR-0023 §7: purchase-decided, cancelled (including never paid) or returned.
TERMINAL_STATUSES: Final = frozenset(
    {"PURCHASE_DECIDED", "CANCELED", "CANCELED_BY_NOPAYMENT", "RETURNED"}
)
# A provider change listed without its type is still one change; this is ICBM's own label.
UNSPECIFIED_CHANGE: Final = "UNSPECIFIED"
ADDRESS_CHANGED: Final = "DELIVERY_ADDRESS_CHANGED"

WINDOW: Final = timedelta(hours=24)
OVERLAP: Final = timedelta(minutes=10)
MAX_WINDOWS: Final = 10
MAX_PAGES: Final = 50
DETAIL_BATCH: Final = 300
UNREADABLE: Final = "OPERATE_ORDER_UNREADABLE"
PAGES_EXCEEDED: Final = "OPERATE_ORDER_PAGES_EXCEEDED"
DETAIL_MISSING: Final = "OPERATE_ORDER_DETAIL_MISSING"
ACTOR: Final = "operate.orders"
LIST_LIMIT: Final = 200

_T = TypeVar("_T")


class OrderSyncBusy(AppError):
    error_class = ErrorClass.CONFLICT


class ShippingRecordUnavailable(AppError):
    error_class = ErrorClass.NOT_FOUND


# ---------------------------------------------------------------- the shipping record


class ShippingCipher:
    """AES-256-GCM over one order's shipping record. The key lives in the OS secret store; the
    product-order id is bound into the authenticated data, so a record cannot be moved to
    another order."""

    KEY_NAME: Final = "operate:orders:shipping_key"
    _MAGIC: Final = b"ICBMSHIP1"
    _NONCE: Final = 12

    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def _key(self, *, create: bool) -> bytes | None:
        stored = self._secrets.get(self.KEY_NAME)
        if stored:
            return base64.b64decode(stored)
        if not create:
            return None
        key = AESGCM.generate_key(bit_length=256)
        self._secrets.set(self.KEY_NAME, base64.b64encode(key).decode("ascii"))
        return key

    def _aad(self, product_order_id: str) -> bytes:
        return self._MAGIC + product_order_id.encode("utf-8")

    def seal(self, product_order_id: str, record: ShippingRecord) -> bytes:
        key = self._key(create=True)
        assert key is not None
        nonce = os.urandom(self._NONCE)
        sealed = AESGCM(key).encrypt(nonce, record.encoded(), self._aad(product_order_id))
        return self._MAGIC + nonce + sealed

    def open(self, product_order_id: str, blob: bytes) -> ShippingRecord | None:
        key = self._key(create=False)
        if key is None or not blob.startswith(self._MAGIC):
            return None
        body = blob[len(self._MAGIC) :]
        try:
            raw = AESGCM(key).decrypt(
                body[: self._NONCE], body[self._NONCE :], self._aad(product_order_id)
            )
        except (InvalidTag, ValueError):
            return None
        return ShippingRecord.decoded(raw)


_PHONE = re.compile(r"^(\d{2,3})-?(\d{3,4})-?(\d{4})$")


def mask_name(name: str | None) -> str | None:
    """``김철수`` → ``김*수``; a two-letter name keeps its first letter only."""
    if not name:
        return None
    if len(name) == 1:
        return "*"
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


def mask_phone(phone: str | None) -> str | None:
    """``010-1234-5678`` → ``010-****-5678``; any other number of at least eight digits keeps
    its last four only."""
    if not phone:
        return None
    found = _PHONE.fullmatch(phone.strip())
    if found:
        head, middle, tail = found.groups()
        return f"{head}-{'*' * len(middle)}-{tail}"
    digits = re.sub(r"\D", "", phone)
    # A short value would be shown whole by its last four digits: it is hidden entirely.
    return "****" + digits[-4:] if len(digits) >= 8 else "****"


# ---------------------------------------------------------------- read models


@dataclass(frozen=True)
class OrderRunView:
    run_id: str
    trigger: str
    state: str
    outcome: str | None
    window_from: datetime
    synced_until: datetime | None
    changes: int
    orders_read: int
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class OrderView:
    product_order_id: str
    order_id: str | None
    status: str | None
    claim_type: str | None
    claim_status: str | None
    place_order_status: str | None
    ordered_at: datetime | None
    paid_at: datetime | None
    # The name of the registration the order resolved to (ICBM's frozen Snapshot), never the
    # provider's product name, which is not retained (ADR-0023 §7).
    product_label: str | None
    original_product_id: str | None
    option_manage_code: str | None
    quantity: int | None
    total_payment_amount: int | None
    delivery_method: str | None
    resolution: str
    registration_id: str | None
    registration_item_key: str | None
    item_id: str | None
    supplier_key: str | None
    source_product_id: str | None
    recipient_masked: str | None
    phone_masked: str | None
    shipping_state: str
    last_changed_at: datetime | None


@dataclass(frozen=True)
class OrdersOverview:
    # NOT_CONNECTED until one change read has succeeded (ADR-0023 §5): no count is a zero then.
    capability: str
    interval_s: float
    last_run: OrderRunView | None
    synced_until: datetime | None
    total: int | None
    orders: tuple[OrderView, ...]


def _run_view(row: OrderSyncRun) -> OrderRunView:
    return OrderRunView(
        run_id=row.run_id,
        trigger=row.trigger,
        state=row.state,
        outcome=row.outcome,
        window_from=row.window_from,
        synced_until=row.synced_until,
        changes=row.changes,
        orders_read=row.orders_read,
        error_code=row.error_code,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _order_view(row: ProductOrder, product_label: str | None) -> OrderView:
    return OrderView(
        product_order_id=row.product_order_id,
        order_id=row.order_id,
        status=row.status,
        claim_type=row.claim_type,
        claim_status=row.claim_status,
        place_order_status=row.place_order_status,
        ordered_at=row.ordered_at,
        paid_at=row.paid_at,
        product_label=product_label,
        original_product_id=row.original_product_id,
        option_manage_code=row.option_manage_code,
        quantity=row.quantity,
        total_payment_amount=row.total_payment_amount,
        delivery_method=row.delivery_method,
        resolution=row.resolution,
        registration_id=row.registration_id,
        registration_item_key=row.registration_item_key,
        item_id=row.item_id,
        supplier_key=row.supplier_key,
        source_product_id=row.source_product_id,
        recipient_masked=row.recipient_masked,
        phone_masked=row.phone_masked,
        shipping_state=row.shipping_state,
        last_changed_at=row.last_changed_at,
    )


@dataclass(frozen=True)
class _Resolution:
    resolution: str
    registration_id: str | None = None
    registration_item_key: str | None = None
    item_id: str | None = None
    source_binding_id: str | None = None
    supplier_key: str | None = None
    source_product_id: str | None = None


class _Stop(Exception):
    """A pass ends early with ``outcome`` (and ``error_code``)."""

    def __init__(self, outcome: str, error_code: str | None = None) -> None:
        super().__init__(outcome)
        self.outcome = outcome
        self.error_code = error_code


# ---------------------------------------------------------------- the owner


class OrderSyncService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        registrations: RegistrationStore,
        source_identity: Callable[[str], tuple[str, str] | None],
        source: OrderSource,
        cipher: ShippingCipher,
        audit: AuditLog,
        interval_s: float,
        initial_lookback_s: float,
        retention_days: int,
        marketplace_key: str = "smartstore",
    ) -> None:
        self._db = db
        self._clock = clock
        self._registrations = registrations
        self._source_identity = source_identity
        self._source = source
        self._cipher = cipher
        self._audit = audit
        self._interval_s = interval_s
        self._lookback = timedelta(seconds=initial_lookback_s)
        self._retention = timedelta(days=retention_days)
        self._marketplace_key = marketplace_key
        self._lock = threading.Lock()

    @property
    def interval_s(self) -> float:
        return self._interval_s

    # ------------------------------------------------------------------ reads

    def capability(self) -> str:
        """``CONNECTED`` once any pass has read a change window successfully."""
        with self._db.read() as session:
            proven = session.scalar(
                select(func.count())
                .select_from(OrderSyncRun)
                .where(OrderSyncRun.synced_until.is_not(None))
            )
        return CONNECTED if proven else NOT_CONNECTED

    def order_count(self) -> int:
        with self._db.read() as session:
            return int(session.scalar(select(func.count()).select_from(ProductOrder)) or 0)

    def overview(self) -> OrdersOverview:
        capability = self.capability()
        with self._db.read() as session:
            last = session.scalars(
                select(OrderSyncRun).order_by(OrderSyncRun.started_at.desc()).limit(1)
            ).first()
            rows = session.scalars(
                select(ProductOrder)
                .order_by(ProductOrder.last_changed_at.desc(), ProductOrder.product_order_id.desc())
                .limit(LIST_LIMIT)
            ).all()
            total = int(session.scalar(select(func.count()).select_from(ProductOrder)) or 0)
            return OrdersOverview(
                capability=capability,
                interval_s=self._interval_s,
                last_run=None if last is None else _run_view(last),
                synced_until=self._cursor(session),
                total=total if capability == CONNECTED else None,
                orders=tuple(_order_view(row, self._label(row.registration_id)) for row in rows),
            )

    def shipping(self, product_order_id: str, *, actor: str, correlation_id: str) -> ShippingRecord:
        """Open one order's shipping record for the detail view. Audited by id."""
        with self._db.write() as session:
            row = session.get(ProductOrder, product_order_id)
            if row is None or row.shipping_state != STORED or row.shipping_ciphertext is None:
                raise ShippingRecordUnavailable(
                    "OPERATE_ORDER_SHIPPING_UNAVAILABLE", "no shipping record is kept for it"
                )
            record = self._cipher.open(product_order_id, row.shipping_ciphertext)
            if record is None:
                raise ShippingRecordUnavailable(
                    "OPERATE_ORDER_SHIPPING_UNREADABLE", "the shipping record cannot be opened"
                )
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.ORDER_SHIPPING_OPENED,
                    action="operate.order_shipping.open",
                    actor=actor,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=product_order_id,
                    correlation_id=correlation_id,
                ),
                session=session,
            )
            return record

    def due(self) -> bool:
        with self._db.read() as session:
            last = session.scalars(
                select(OrderSyncRun).order_by(OrderSyncRun.started_at.desc()).limit(1)
            ).first()
        if last is None:
            return True
        return self._clock.now() - last.started_at >= timedelta(seconds=self._interval_s)

    # ------------------------------------------------------------------ lifecycle

    def settle_interrupted(self) -> int:
        """A pass an earlier process left RUNNING is finished as INTERRUPTED (startup).

        Only the process that owns the data directory runs this, before its scheduler starts:
        one process owns a data directory (ADR-0006, ``owner.lock`` taken before the container
        is built, never shared and never taken over), so no RUNNING row can belong to a live
        pass of another process when it runs (GPT audit, PR #250). Within the owner, the lock
        and the unique RUNNING index refuse a second pass as busy."""
        with self._db.write() as session:
            rows = session.scalars(select(OrderSyncRun).where(OrderSyncRun.state == RUNNING)).all()
            for row in rows:
                row.state, row.outcome, row.finished_at = FINISHED, INTERRUPTED, self._clock.now()
            return len(rows)

    def purge_shipping(self, *, correlation_id: str) -> int:
        """Delete every shipping record whose order has been terminal for the retention span."""
        cutoff = self._clock.now() - self._retention
        with self._db.write() as session:
            rows = session.scalars(
                select(ProductOrder).where(
                    ProductOrder.shipping_state == STORED,
                    ProductOrder.terminal_at.is_not(None),
                    ProductOrder.terminal_at <= cutoff,
                )
            ).all()
            now = self._clock.now()
            for row in rows:
                row.shipping_ciphertext = None
                row.shipping_state = DELETED
                row.shipping_deleted_at = now
                row.recipient_masked = None
                row.phone_masked = None
                row.updated_at = now
                self._audit.append(
                    AuditEntry(
                        event_type=AuditEventType.ORDER_SHIPPING_DELETED,
                        action="operate.order_shipping.delete",
                        actor=ACTOR,
                        outcome=AuditOutcome.RECORDED,
                        target_ref=row.product_order_id,
                        reason_code="RETENTION_ELAPSED",
                        correlation_id=correlation_id,
                    ),
                    session=session,
                )
            return len(rows)

    # ------------------------------------------------------------------ the pass

    def sync(self, *, trigger: str, correlation_id: str) -> OrderRunView:
        if not self._lock.acquire(blocking=False):
            raise OrderSyncBusy("OPERATE_ORDER_SYNC_RUNNING", "an order sync is already running")
        try:
            now = self._clock.now()
            with self._db.read() as session:
                cursor = self._cursor(session)
            start = now - self._lookback if cursor is None else cursor - OVERLAP
            run_id = self._start(trigger, start, correlation_id)
            synced: datetime | None = None
            totals = [0, 0]
            try:
                if not self._source.available():
                    return self._finish(run_id, SESSION_UNAVAILABLE, None, totals)
                registrations = self._registrations.marketplace_registrations(self._marketplace_key)
                window_from = start
                for _ in range(MAX_WINDOWS):
                    window_to = min(window_from + WINDOW, now)
                    self._window(
                        run_id, window_from, window_to, registrations, totals, correlation_id
                    )
                    synced = window_to
                    if window_to >= now:
                        break
                    # Every window overlaps the last one, inside a pass as across passes, so a
                    # change reported late is never missed (ADR-0023 §5; GPT audit, PR #250).
                    window_from = window_to - OVERLAP
            except _Stop as stop:
                return self._finish(run_id, stop.outcome, synced, totals, stop.error_code)
            except BaseException:
                self._finish(run_id, INTERRUPTED, synced, totals)
                raise
            return self._finish(run_id, COMPLETED, synced, totals)
        finally:
            self._lock.release()

    def _window(
        self,
        run_id: str,
        since: datetime,
        until: datetime,
        registrations: Sequence[RegistrationRecord],
        totals: list[int],
        correlation_id: str,
    ) -> None:
        """Read one window completely and record it, or raise :class:`_Stop`."""
        changes: list[OrderChange] = []
        page_from: datetime = since
        sequence: str | None = None
        for _ in range(MAX_PAGES):
            page = self._read(
                partial(self._source.changes, since=page_from, until=until, more_sequence=sequence)
            )
            changes.extend(page.changes)
            if page.more_from is None:
                break
            if page.more_from < page_from:
                raise _Stop(FAILED, UNREADABLE)
            page_from, sequence = page.more_from, page.more_sequence
        else:
            raise _Stop(FAILED, PAGES_EXCEEDED)
        ids = list(dict.fromkeys(change.product_order_id for change in changes))
        facts: list[ProductOrderFacts] = []
        for start in range(0, len(ids), DETAIL_BATCH):
            batch = ids[start : start + DETAIL_BATCH]
            facts.extend(self._read(partial(self._source.details, batch)))
        by_order: dict[str, list[OrderChange]] = {}
        for change in changes:
            by_order.setdefault(change.product_order_id, []).append(change)
        try:
            with self._db.write() as session:
                for fact in facts:
                    self._ingest(
                        session,
                        run_id,
                        fact,
                        by_order.get(fact.product_order_id, []),
                        registrations,
                        correlation_id,
                    )
        except Exception as exc:
            # A failed local write records nothing of the window: the cursor does not move.
            logger.warning("operate.order_ingest_failed", exc_info=True)
            raise _Stop(FAILED, exc.code if isinstance(exc, AppError) else UNREADABLE) from exc
        totals[0] += len(changes)
        totals[1] += len(facts)
        # A listed change whose product order the detail read did not return was not recorded,
        # so the window was not read completely: the pass ends and the cursor stays (GPT audit,
        # PR #250). What was read is kept; the next pass reads the window again.
        missing = set(ids) - {fact.product_order_id for fact in facts}
        if missing:
            logger.warning("operate.order_details_missing", extra={"count": len(missing)})
            raise _Stop(FAILED, DETAIL_MISSING)

    def _read(self, call: Callable[[], _T]) -> _T:
        try:
            return call()
        except AppError as exc:
            if exc.error_class is ErrorClass.RATE_LIMITED:
                raise _Stop(RATE_LIMITED) from exc
            raise _Stop(FAILED, exc.code) from exc
        except Exception as exc:
            logger.warning("operate.order_read_failed", exc_info=True)
            raise _Stop(FAILED, UNREADABLE) from exc

    def _ingest(
        self,
        session: Session,
        run_id: str,
        fact: ProductOrderFacts,
        changes: Sequence[OrderChange],
        registrations: Sequence[RegistrationRecord],
        correlation_id: str,
    ) -> None:
        now = self._clock.now()
        row = session.get(ProductOrder, fact.product_order_id)
        if row is None:
            resolved = self._resolve(fact, registrations)
            row = ProductOrder(
                product_order_id=fact.product_order_id,
                marketplace_key=self._marketplace_key,
                resolution=resolved.resolution,
                registration_id=resolved.registration_id,
                registration_item_key=resolved.registration_item_key,
                item_id=resolved.item_id,
                source_binding_id=resolved.source_binding_id,
                supplier_key=resolved.supplier_key,
                source_product_id=resolved.source_product_id,
                resolved_at=now,
                shipping_state=NONE,
                first_seen_at=now,
                updated_at=now,
            )
            session.add(row)
        latest = max((change.changed_at for change in changes), default=None)
        row.order_id = fact.order_id
        row.status = fact.status
        row.claim_type = fact.claim_type
        row.claim_status = fact.claim_status
        row.place_order_status = fact.place_order_status
        row.ordered_at = fact.ordered_at
        row.paid_at = fact.paid_at
        row.decided_at = fact.decided_at
        row.channel_product_id = fact.channel_product_id
        row.original_product_id = fact.original_product_id
        row.option_manage_code = fact.option_manage_code
        row.seller_product_code = fact.seller_product_code
        row.quantity = fact.quantity
        row.unit_price = fact.unit_price
        row.total_payment_amount = fact.total_payment_amount
        row.delivery_method = fact.delivery_method
        if latest is not None and (row.last_changed_at is None or latest > row.last_changed_at):
            row.last_changed_at = latest
        if fact.status in TERMINAL_STATUSES and row.terminal_at is None:
            row.terminal_at = latest or now
        row.updated_at = now
        address_changed = any(change.change_type == ADDRESS_CHANGED for change in changes)
        # A deleted record is never kept again; a stored one is resealed only when its address
        # changed (ADR-0023 §7).
        if (
            row.shipping_state != DELETED
            and not fact.shipping.empty
            and (row.shipping_state == NONE or address_changed)
        ):
            row.shipping_ciphertext = self._cipher.seal(fact.product_order_id, fact.shipping)
            row.shipping_state = STORED
            row.recipient_masked = mask_name(fact.shipping.recipient_name)
            row.phone_masked = mask_phone(fact.shipping.phone1)
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.ORDER_SHIPPING_STORED,
                    action="operate.order_shipping.store",
                    actor=ACTOR,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=fact.product_order_id,
                    reason_code=ADDRESS_CHANGED if address_changed else None,
                    correlation_id=correlation_id,
                ),
                session=session,
            )
        session.flush()
        for change in changes:
            change_type = change.change_type or UNSPECIFIED_CHANGE
            seen = session.scalar(
                select(OrderStatusChange.entry_id).where(
                    OrderStatusChange.product_order_id == fact.product_order_id,
                    OrderStatusChange.changed_at == change.changed_at,
                    OrderStatusChange.change_type == change_type,
                )
            )
            if seen is None:
                session.add(
                    OrderStatusChange(
                        entry_id=str(uuid.uuid4()),
                        product_order_id=fact.product_order_id,
                        run_id=run_id,
                        change_type=change_type,
                        status=change.status,
                        claim_type=change.claim_type,
                        claim_status=change.claim_status,
                        changed_at=change.changed_at,
                        observed_at=now,
                    )
                )
                session.flush()

    def _resolve(
        self, fact: ProductOrderFacts, registrations: Sequence[RegistrationRecord]
    ) -> _Resolution:
        """ADR-0013 order-line resolution by provider identities only — never by name."""
        found = [
            record
            for record in registrations
            if fact.original_product_id
            and record.marketplace_product_id == fact.original_product_id
        ]
        if not found and fact.channel_product_id:
            found = [
                record
                for record in registrations
                if record.marketplace_channel_product_id == fact.channel_product_id
            ]
        if len(found) != 1:
            return _Resolution(UNMATCHED)
        (registration,) = found
        if (
            fact.seller_product_code
            and registration.seller_product_code
            and fact.seller_product_code != registration.seller_product_code
        ):
            return _Resolution(CONFLICT, registration_id=registration.registration_id)
        if fact.option_manage_code:
            items = [
                item
                for item in registration.items
                if item.registration_item_key == fact.option_manage_code
            ]
        else:
            items = list(registration.items) if len(registration.items) == 1 else []
        snapshot = self._registrations.snapshot(registration.registration_snapshot_id)
        frozen = {} if snapshot is None else {i.item_snapshot_id: i.item_id for i in snapshot.items}
        if len(items) != 1 or items[0].item_snapshot_id not in frozen:
            return _Resolution(ITEM_UNMATCHED, registration_id=registration.registration_id)
        (item,) = items
        item_id = frozen[item.item_snapshot_id]
        identity = self._source_identity(item_id)
        return _Resolution(
            MATCHED,
            registration_id=registration.registration_id,
            registration_item_key=item.registration_item_key,
            item_id=item_id,
            source_binding_id=item.current_source_binding_id,
            supplier_key=None if identity is None else identity[0],
            source_product_id=None if identity is None else identity[1],
        )

    # ------------------------------------------------------------------ internals

    def _label(self, registration_id: str | None) -> str | None:
        if registration_id is None:
            return None
        record = self._registrations.registration(registration_id)
        payload = (
            None
            if record is None
            else self._registrations.snapshot_payload(record.registration_snapshot_id)
        )
        name = payload.get("name") if isinstance(payload, Mapping) else None
        return str(name["value"]) if isinstance(name, Mapping) and name.get("value") else None

    @staticmethod
    def _cursor(session: Session) -> datetime | None:
        return session.scalar(select(func.max(OrderSyncRun.synced_until)))

    def _start(self, trigger: str, window_from: datetime, correlation_id: str) -> str:
        run_id = str(uuid.uuid4())
        try:
            self._insert_run(run_id, trigger, window_from, correlation_id)
        except IntegrityError:
            # Another process holds the one running slot (the unique RUNNING index).
            raise OrderSyncBusy(
                "OPERATE_ORDER_SYNC_RUNNING", "an order sync is already running"
            ) from None
        return run_id

    def _insert_run(
        self, run_id: str, trigger: str, window_from: datetime, correlation_id: str
    ) -> None:
        with self._db.write() as session:
            session.add(
                OrderSyncRun(
                    run_id=run_id,
                    trigger=trigger,
                    state=RUNNING,
                    outcome=None,
                    window_from=window_from,
                    synced_until=None,
                    changes=0,
                    orders_read=0,
                    error_code=None,
                    correlation_id=correlation_id,
                    started_at=self._clock.now(),
                    finished_at=None,
                )
            )

    def _finish(
        self,
        run_id: str,
        outcome: str,
        synced: datetime | None,
        totals: list[int],
        error_code: str | None = None,
    ) -> OrderRunView:
        with self._db.write() as session:
            row = session.get(OrderSyncRun, run_id)
            assert row is not None
            row.state, row.outcome, row.finished_at = FINISHED, outcome, self._clock.now()
            row.synced_until = synced
            row.changes, row.orders_read = totals
            row.error_code = error_code if outcome == FAILED else None
            session.flush()
            return _run_view(row)


class OrderSyncScheduler:
    """The periodic ingest (ADR-0023 §5: automatic cadence plus "지금 동기화") and the shipping
    record retention, on their own thread."""

    def __init__(self, service: OrderSyncService, *, tick_s: float = 60.0) -> None:
        self._service = service
        self._tick_s = tick_s
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._service.settle_interrupted()
        if self._service.interval_s <= 0:
            return  # disabled by policy
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="icbm-order-sync", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._halt.wait(self._tick_s):
            correlation_id = f"order-sync-{uuid.uuid4()}"
            try:
                self._service.purge_shipping(correlation_id=correlation_id)
                if self._service.due():
                    self._service.sync(trigger=AUTO, correlation_id=correlation_id)
            except OrderSyncBusy:
                continue
            except Exception:
                logger.exception("operate.order_sync_error")

    def stop(self) -> None:
        self._halt.set()
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None


__all__ = [
    "CONNECTED",
    "NOT_CONNECTED",
    "OrderRunView",
    "OrderSyncBusy",
    "OrderSyncScheduler",
    "OrderSyncService",
    "OrderView",
    "OrdersOverview",
    "ShippingCipher",
    "ShippingRecordUnavailable",
    "mask_name",
    "mask_phone",
]
