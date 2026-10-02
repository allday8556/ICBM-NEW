"""The extension list-queue owner (ADR-0019 §8.1, E3).

The operator finds a list page's products in the side panel and declares a queue. This owner
decides everything after that, and the extension's own clock decides nothing:

- **Discovery is judged here.** The extension sends only product URLs (scheme, host and path). A
  link is a candidate only if ``check_target`` accepts it as a product read under the supplier's
  extension envelope and the §6.1 secret rules find nothing in it. Candidates are deduplicated by
  the product the URL names. A refused link is counted; it is never stored or logged.
- **The bounds are declared twice.** The supplier's ``CollectionProfile`` declares its
  ``QueueLimits``; the operator declares the queue's size and interval inside them. Either missing
  or out of range refuses before the queue exists. Nothing is defaulted.
- **Every read is issued here, durably, before it happens.** :meth:`ExtensionQueues.next` answers
  ``WAIT`` (with the seconds left), ``ISSUE`` (one item, its URL and a random single-use ticket,
  written before the answer is returned) or ``DONE``. A supplier has at most one open queue and at
  most one issued, unsettled read.
- **One item is one read.** An item is never reissued or retried. An issued read that ends without
  a capture — its time ran out, its capture was refused, or the extension gave it back because it
  could not capture it — is ``EXPIRED``: spent, still counted.
- **A queue goes on past an item that fails** (the user's rule of 2026-10-02). An expired read, a
  refused capture and a ``FAILED`` run leave their item as it ended, and the queue reads the next
  item. The operator sees every failure in the queue and in Collection Management.

A ticketed capture goes through the unchanged ingest (``service.py``), which claims its item here
for exactly that URL, once, in the ingest's own write unit. The run is an ordinary ``EXTENSION``
run; its outcome stays its own, and an item's state is never a run outcome (AC-31). A single click
is serialized with the queue: the ingest refuses it while a queue read of the supplier is out.

Only a ticket's SHA-256 is stored. Neither a ticket nor a refused link is ever logged.
"""

import hashlib
import logging
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.platform.core.clock import Clock
from app.platform.core.errors import (
    InputValidationError,
    NotFoundError,
    PolicyBlockedError,
    RateLimitedError,
)
from app.platform.db.database import Database
from app.stages.collect.collection import RegisteredCollection, pacing_key
from app.stages.collect.extension.gate import locator_holds_secret
from app.stages.collect.extension.service import MINIMUM_INGEST_INTERVAL_S, extension_profile
from app.stages.collect.models import (
    CollectionOutcome,
    CollectionRun,
    ExtensionQueue,
    ExtensionQueueItem,
    QueueItemState,
    QueueState,
    TransportKind,
)
from app.stages.collect.runs import CollectionRunStore, PacingKey
from integrations.suppliers.collection import (
    CollectionProfile,
    QueueLimits,
    ReadKind,
    SupplierCollection,
)
from integrations.suppliers.transport.collection import CollectionTargetRefused, check_target

logger = logging.getLogger("icbm.collect.extension.queue")

# How long the extension is told to wait for a run that is still being processed. It is a polling
# hint, never a bound: no read is issued until the run has settled.
SETTLING_WAIT_S = 2.0
# The longest product URL a declaration may carry. A longer one is refused and counted.
MAX_LINK_CHARS = 2048


class ExtensionQueueRefused(PolicyBlockedError):
    """A queue call the declared bounds or the queue's own state do not allow."""


class ExtensionQueueBusy(RateLimitedError):
    """The supplier already has an open queue."""


@dataclass(frozen=True)
class QueueDeclaration:
    """What the operator declared for one queue. ``None`` is a missing bound, never a default."""

    supplier_key: str
    links: Sequence[str]
    max_products: int | None
    interval_s: float | None
    skip_collected: bool


@dataclass(frozen=True)
class QueueItemView:
    item_id: str
    position: int
    source_url: str
    # The product the URL names, as the supplier's own URL form says it, or the normalized URL.
    product_key: str
    state: QueueItemState
    collection_run_id: str | None
    # The run's own outcome, read from the run: a separate axis from the item state (AC-31).
    run_outcome: CollectionOutcome | None
    run_detail: str | None


@dataclass(frozen=True)
class QueueView:
    queue_id: str
    supplier_key: str
    state: QueueState
    max_products: int
    interval_s: float
    skip_collected: bool
    items: tuple[QueueItemView, ...]


@dataclass(frozen=True)
class DiscoveryPolicy:
    """What the extension needs to find a list page's products, and the bounds it may declare.

    ``open_queue`` is the supplier's queue that is still open, or ``None``. The queue lives here,
    not in the extension: a panel that was closed, or a worker that was stopped, finds it again
    and resumes or cancels it."""

    supplier_key: str
    storefront_host: str
    product_path: str
    max_discovered_links: int
    max_queue_products: int
    min_queue_interval_s: float
    open_queue: QueueView | None


@dataclass(frozen=True)
class DiscoveryCount:
    """What became of the submitted links. Counts only: a refused link is never kept."""

    submitted: int
    refused: int
    duplicates: int
    skipped: int
    queued: int
    beyond_cap: int


@dataclass(frozen=True)
class CreatedQueue:
    view: QueueView
    count: DiscoveryCount


@dataclass(frozen=True)
class QueueAnswer:
    """``WAIT``, ``ISSUE`` or ``DONE``. Only an ``ISSUE`` carries an item and its ticket."""

    kind: str
    queue_state: QueueState
    wait_s: float | None = None
    item: QueueItemView | None = None
    ticket: str | None = None
    expires_in_s: float | None = None


def _ticket_digest(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("ascii")).hexdigest()


def _aware(value: datetime, now: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=now.tzinfo)


def _product_key(key: PacingKey) -> str:
    return key.source_product_id if key.source_product_id is not None else key.url


class ExtensionQueues:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        runs: CollectionRunStore,
        collections: Sequence[RegisteredCollection],
    ) -> None:
        self._db = db
        self._clock = clock
        self._runs = runs
        self._collections = {registered.supplier_key: registered for registered in collections}

    # ------------------------------------------------------------------ the declared bounds

    def discovery_policy(self, supplier_key: str) -> DiscoveryPolicy:
        """The supplier's reviewed product path form and its queue limits, or a refusal, with
        the supplier's queue that is still open, settled first."""
        registered, limits = self._declared(supplier_key)
        profile = registered.collection.profile
        with self._db.write() as session:
            open_queue: QueueView | None = None
            queue = session.scalars(
                select(ExtensionQueue).where(
                    ExtensionQueue.supplier_key == registered.supplier_key,
                    ExtensionQueue.state == QueueState.OPEN.value,
                )
            ).first()
            if queue is not None:
                self._settle(session, queue, self._clock.now())
                if queue.state == QueueState.OPEN.value:
                    open_queue = _view(session, queue.queue_id)
        return DiscoveryPolicy(
            supplier_key=registered.supplier_key,
            storefront_host=profile.storefront_host,
            product_path=profile.product_path,
            max_discovered_links=limits.max_discovered_links,
            max_queue_products=limits.max_queue_products,
            min_queue_interval_s=limits.min_queue_interval_s,
            open_queue=open_queue,
        )

    def _declared(self, supplier_key: str) -> tuple[RegisteredCollection, QueueLimits]:
        try:
            registered = self._collections[supplier_key]
        except KeyError:
            raise NotFoundError(
                "COLLECT_SUPPLIER_UNKNOWN", f"no collection definition for {supplier_key!r}"
            ) from None
        limits = registered.collection.profile.queue_limits
        if limits is None:
            raise ExtensionQueueRefused(
                "EXTENSION_QUEUE_NOT_DECLARED", "this supplier declares no list queue"
            )
        if limits.min_queue_interval_s < MINIMUM_INGEST_INTERVAL_S:
            raise ExtensionQueueRefused(
                "EXTENSION_QUEUE_LIMITS_INVALID",
                "the supplier's queue interval is below the extension ingest interval",
            )
        return registered, limits

    # ------------------------------------------------------------------ the declaration

    def create(self, declaration: QueueDeclaration) -> CreatedQueue:
        """Judge the discovered links and open one queue, or refuse before it exists."""
        registered, limits = self._declared(declaration.supplier_key)
        max_products, interval_s = _operator_bounds(declaration, limits)
        if len(declaration.links) > limits.max_discovered_links:
            raise ExtensionQueueRefused(
                "EXTENSION_QUEUE_TOO_MANY_LINKS",
                "the discovery submits more links than the supplier allows",
                details={"bound": limits.max_discovered_links},
            )
        collection = registered.collection
        envelope = extension_profile(collection.profile)
        candidates: dict[str, tuple[str, PacingKey]] = {}
        refused = duplicates = 0
        for link in declaration.links:
            if not _acceptable(envelope, link):
                refused += 1
                continue
            key = pacing_key(collection, link)
            if _product_key(key) in candidates:
                duplicates += 1
                continue
            candidates[_product_key(key)] = (link, key)
        if not candidates:
            raise InputValidationError(
                "EXTENSION_QUEUE_NO_PRODUCTS", "no submitted link is a product of this supplier"
            )
        now = self._clock.now()
        queue_id = str(uuid.uuid4())
        skipped = queued = beyond = 0
        with self._db.write() as session:
            open_queue = session.scalar(
                select(ExtensionQueue.queue_id).where(
                    ExtensionQueue.supplier_key == registered.supplier_key,
                    ExtensionQueue.state == QueueState.OPEN.value,
                )
            )
            if open_queue is not None:
                raise ExtensionQueueBusy(
                    "EXTENSION_QUEUE_BUSY", "this supplier already has an open queue"
                )
            session.add(
                ExtensionQueue(
                    queue_id=queue_id,
                    supplier_key=registered.supplier_key,
                    state=QueueState.OPEN.value,
                    max_products=max_products,
                    interval_s=interval_s,
                    skip_collected=declaration.skip_collected,
                    created_at=now,
                    last_issued_at=None,
                    stop_reason=None,
                    finished_at=None,
                )
            )
            session.flush()
            position = 0
            for product_key, (link, key) in candidates.items():
                if declaration.skip_collected and _collected(session, key):
                    # Skipped without a read, and taking no read slot: the operator's number bounds
                    # the reads, and the supplier's link bound (checked above) bounds what a queue
                    # holds, skipped products included.
                    state = QueueItemState.SKIPPED
                    skipped += 1
                elif queued < max_products:
                    state = QueueItemState.WAITING
                    queued += 1
                else:
                    beyond += 1
                    continue
                position += 1
                session.add(
                    ExtensionQueueItem(
                        item_id=str(uuid.uuid4()),
                        queue_id=queue_id,
                        supplier_key=registered.supplier_key,
                        position=position,
                        source_url=link,
                        product_key=product_key,
                        state=state.value,
                        ticket_sha256=None,
                        issued_at=None,
                        expires_at=None,
                        collection_run_id=None,
                    )
                )
            if queued == 0:
                raise InputValidationError(
                    "EXTENSION_QUEUE_NO_PRODUCTS", "every discovered product is already collected"
                )
            session.flush()
            view = _view(session, queue_id)
        count = DiscoveryCount(
            submitted=len(declaration.links),
            refused=refused,
            duplicates=duplicates,
            skipped=skipped,
            queued=queued,
            beyond_cap=beyond,
        )
        logger.info(
            "collect.extension_queue_opened",
            extra={
                "queue_id": queue_id,
                "supplier": registered.supplier_key,
                "max_products": max_products,
                "interval_s": interval_s,
                "submitted": count.submitted,
                "refused": count.refused,
                "duplicates": count.duplicates,
                "skipped": count.skipped,
                "queued": count.queued,
                "beyond_cap": count.beyond_cap,
            },
        )
        return CreatedQueue(view, count)

    # ------------------------------------------------------------------ the server-issued read

    def next(self, queue_id: str) -> QueueAnswer:
        """Wait, issue one read, or done. An issue is written before this returns."""
        with self._db.write() as session:
            queue = _queue(session, queue_id)
            now = self._clock.now()
            self._settle(session, queue, now)
            state = QueueState(queue.state)
            if state is not QueueState.OPEN:
                return QueueAnswer("DONE", state)
            registered, limits = self._declared(queue.supplier_key)
            in_flight = _in_flight(session, queue_id)
            if in_flight is not None:
                return QueueAnswer("WAIT", state, wait_s=_in_flight_wait(session, in_flight, now))
            # One issued read per supplier, across its queues: a cancelled queue's read still out
            # holds a new queue too.
            if self.read_in_flight(session, queue.supplier_key):
                return QueueAnswer("WAIT", state, wait_s=SETTLING_WAIT_S)
            waiting = session.scalars(
                select(ExtensionQueueItem)
                .where(
                    ExtensionQueueItem.queue_id == queue_id,
                    ExtensionQueueItem.state == QueueItemState.WAITING.value,
                )
                .order_by(ExtensionQueueItem.position)
                .limit(1)
            ).first()
            if waiting is None:
                _close(queue, QueueState.FINISHED, now)
                return QueueAnswer("DONE", QueueState.FINISHED)
            wait = self._wait(session, queue, registered, waiting, now)
            if wait > 0:
                return QueueAnswer("WAIT", state, wait_s=wait)
            ticket = secrets.token_urlsafe(32)
            waiting.state = QueueItemState.ISSUED.value
            waiting.ticket_sha256 = _ticket_digest(ticket)
            waiting.issued_at = now
            waiting.expires_at = now + timedelta(seconds=limits.issue_ttl_s)
            queue.last_issued_at = now
            session.flush()
            item = _item_view(waiting, None)
        logger.info(
            "collect.extension_queue_issued",
            extra={"queue_id": queue_id, "item_id": item.item_id, "position": item.position},
        )
        return QueueAnswer(
            "ISSUE", QueueState.OPEN, item=item, ticket=ticket, expires_in_s=limits.issue_ttl_s
        )

    def _wait(
        self,
        session: Session,
        queue: ExtensionQueue,
        registered: RegisteredCollection,
        item: ExtensionQueueItem,
        now: datetime,
    ) -> float:
        """Seconds still owed before this item may be read: the queue interval, any unsettled or
        too-recent extension capture, and the same-product interval (ADR-0010 §4)."""
        owed = 0.0
        if queue.last_issued_at is not None:
            due = _aware(queue.last_issued_at, now) + timedelta(seconds=queue.interval_s)
            owed = max(owed, (due - now).total_seconds())
        pending, newest = self._runs.transport_activity(TransportKind.EXTENSION, session=session)
        if pending:
            owed = max(owed, SETTLING_WAIT_S)
        if newest is not None:
            due = _aware(newest, now) + timedelta(seconds=MINIMUM_INGEST_INTERVAL_S)
            owed = max(owed, (due - now).total_seconds())
        collection = registered.collection
        key = pacing_key(collection, item.source_url)
        interval = collection.profile.limits.same_product_interval_s
        last = _last_read(session, self._runs, collection, key, now, interval)
        if last is not None:
            owed = max(
                owed, (_aware(last, now) + timedelta(seconds=interval) - now).total_seconds()
            )
        return max(0.0, owed)

    def _settle(self, session: Session, queue: ExtensionQueue, now: datetime) -> None:
        """Expire an issued read whose time ran out: spent, still counted, never reissued. The
        queue goes on. Written in the caller's unit."""
        for item in session.scalars(
            select(ExtensionQueueItem).where(
                ExtensionQueueItem.queue_id == queue.queue_id,
                ExtensionQueueItem.state == QueueItemState.ISSUED.value,
            )
        ):
            assert item.expires_at is not None
            if _aware(item.expires_at, now) <= now:
                item.state = QueueItemState.EXPIRED.value
                logger.info(
                    "collect.extension_queue_read_expired",
                    extra={"queue_id": queue.queue_id, "item_id": item.item_id},
                )
        session.flush()

    # ------------------------------------------------------------------ the operator's own calls

    def cancel(self, queue_id: str) -> QueueView:
        """Cancel an open queue: every unissued item is settled CANCELLED. An issued read stays
        what it is — it was already counted, and its capture may still arrive in time."""
        with self._db.write() as session:
            queue = _queue(session, queue_id)
            now = self._clock.now()
            self._settle(session, queue, now)
            if queue.state == QueueState.OPEN.value:
                for item in session.scalars(
                    select(ExtensionQueueItem).where(
                        ExtensionQueueItem.queue_id == queue_id,
                        ExtensionQueueItem.state == QueueItemState.WAITING.value,
                    )
                ):
                    item.state = QueueItemState.CANCELLED.value
                _close(queue, QueueState.CANCELLED, now)
                session.flush()
            return _view(session, queue_id)

    def read(self, queue_id: str) -> QueueView:
        """The queue as it stands, with each captured item's run outcome read from the run."""
        with self._db.write() as session:
            queue = _queue(session, queue_id)
            self._settle(session, queue, self._clock.now())
            return _view(session, queue_id)

    # ------------------------------------------------------------------ the ingest's claim

    def claim(self, session: Session, *, supplier_key: str, ticket: str, url: str) -> str:
        """Claim an issued item for a ticketed capture of exactly its URL, once, in time."""
        item = session.scalar(
            select(ExtensionQueueItem).where(
                ExtensionQueueItem.ticket_sha256 == _ticket_digest(ticket)
            )
        )
        now = self._clock.now()
        if (
            item is None
            or item.supplier_key != supplier_key
            or item.state != QueueItemState.ISSUED.value
            or item.expires_at is None
            or _aware(item.expires_at, now) <= now
            or item.source_url != url
        ):
            raise ExtensionQueueRefused(
                "EXTENSION_QUEUE_TICKET_REFUSED", "this ticket answers no open queue read"
            )
        return item.item_id

    def attach(self, session: Session, item_id: str, collection_run_id: str) -> None:
        item = session.get(ExtensionQueueItem, item_id)
        assert item is not None and item.state == QueueItemState.ISSUED.value
        item.state = QueueItemState.CAPTURED.value
        item.collection_run_id = collection_run_id
        session.flush()

    def read_in_flight(self, session: Session, supplier_key: str) -> bool:
        """Whether a queue read of this supplier is issued and still open."""
        held = session.scalar(
            select(func.count())
            .select_from(ExtensionQueueItem)
            .where(
                ExtensionQueueItem.supplier_key == supplier_key,
                ExtensionQueueItem.state == QueueItemState.ISSUED.value,
                ExtensionQueueItem.expires_at > self._clock.now(),
            )
        )
        return bool(held)

    def refuse(self, ticket: str) -> None:
        """A ticketed capture was refused: its issued read is spent, never reissued, and the queue
        goes on. The ticket alone names the read — a random 256-bit value issued once — so nothing
        else the refused capture claims, its supplier included, can keep the read open. A ticket
        that answers no issued read changes nothing."""
        self._spend(ticket, None, "collect.extension_queue_read_refused")

    def release(self, queue_id: str, ticket: str) -> QueueView:
        """The extension could not capture an issued read — the page did not load, or the cut
        refused it before anything was sent — and gives the read back at once instead of letting
        it run out. It is spent exactly as an expired or refused read is, never reissued, and the
        queue goes on without waiting for the issue lifetime. The ticket alone names the read; a
        ticket that answers no issued read of this queue changes nothing."""
        self._spend(ticket, queue_id, "collect.extension_queue_read_released")
        return self.read(queue_id)

    def _spend(self, ticket: str, queue_id: str | None, event: str) -> None:
        with self._db.write() as session:
            item = session.scalar(
                select(ExtensionQueueItem).where(
                    ExtensionQueueItem.ticket_sha256 == _ticket_digest(ticket)
                )
            )
            if (
                item is None
                or item.state != QueueItemState.ISSUED.value
                or (queue_id is not None and item.queue_id != queue_id)
            ):
                return
            item.state = QueueItemState.EXPIRED.value
            logger.info(event, extra={"queue_id": item.queue_id, "item_id": item.item_id})
            session.flush()


def _operator_bounds(declaration: QueueDeclaration, limits: QueueLimits) -> tuple[int, float]:
    """The operator's own bounds, inside the supplier's. Missing or out of range refuses."""
    if declaration.max_products is None or declaration.interval_s is None:
        raise InputValidationError(
            "EXTENSION_QUEUE_CAP_MISSING", "a queue needs its number of products and its interval"
        )
    max_products, interval_s = declaration.max_products, declaration.interval_s
    if not 1 <= max_products <= limits.max_queue_products:
        raise InputValidationError(
            "EXTENSION_QUEUE_CAP_OUT_OF_RANGE",
            "the number of products is outside the supplier's bound",
            details={"bound": limits.max_queue_products},
        )
    if not interval_s >= limits.min_queue_interval_s:
        raise InputValidationError(
            "EXTENSION_QUEUE_CAP_OUT_OF_RANGE",
            "the interval is below the supplier's bound",
            details={"bound": limits.min_queue_interval_s},
        )
    return max_products, float(interval_s)


def _acceptable(envelope: CollectionProfile, link: str) -> bool:
    """A product read of the supplier with nothing the §6.1 secret rules refuse. Anything the URL
    parser cannot read is refused, never an error."""
    if len(link) > MAX_LINK_CHARS or "?" in link or "#" in link:
        return False
    try:
        parts = urlsplit(link)
        if parts.query or parts.fragment:
            return False
        check_target(envelope, link, ReadKind.PRODUCT_READ)
    except (CollectionTargetRefused, ValueError):
        return False
    return not locator_holds_secret(link)


def _collected(session: Session, key: PacingKey) -> bool:
    """Whether this product already has a RECORDED run for the supplier."""
    named = CollectionRun.pacing_key == key.url
    if key.source_product_id is not None:
        named = named | (CollectionRun.source_product_id == key.source_product_id)
    found = session.scalar(
        select(func.count())
        .select_from(CollectionRun)
        .where(
            CollectionRun.supplier_key == key.supplier_key,
            CollectionRun.outcome == CollectionOutcome.RECORDED.value,
            named,
        )
    )
    return bool(found)


def _last_read(
    session: Session,
    runs: CollectionRunStore,
    collection: SupplierCollection,
    key: PacingKey,
    now: datetime,
    interval_s: float,
) -> datetime | None:
    """The most recent read of this product of any kind: the server's own reads, issued queue reads
    and extension captures (ADR-0019 §8.1)."""
    reads: list[datetime] = []
    server = runs.last_product_read(session, key)
    if server is not None:
        reads.append(_aware(server, now))
    issued = session.scalar(
        select(func.max(ExtensionQueueItem.issued_at)).where(
            ExtensionQueueItem.supplier_key == key.supplier_key,
            ExtensionQueueItem.product_key == _product_key(key),
            ExtensionQueueItem.issued_at.is_not(None),
        )
    )
    if issued is not None:
        reads.append(_aware(issued, now))
    since = now - timedelta(seconds=interval_s)
    for source_url, source_product_id, requested_at in session.execute(
        select(
            CollectionRun.source_url, CollectionRun.source_product_id, CollectionRun.requested_at
        ).where(
            CollectionRun.supplier_key == key.supplier_key,
            CollectionRun.transport_kind == TransportKind.EXTENSION.value,
            CollectionRun.requested_at >= since,
        )
    ):
        captured = pacing_key(collection, source_url)
        same = captured.url == key.url or (
            key.source_product_id is not None
            and key.source_product_id in (captured.source_product_id, source_product_id)
        )
        if same:
            reads.append(_aware(requested_at, now))
    return max(reads, default=None)


def _queue(session: Session, queue_id: str) -> ExtensionQueue:
    queue = session.get(ExtensionQueue, queue_id)
    if queue is None:
        raise NotFoundError("EXTENSION_QUEUE_UNKNOWN", "no extension queue has that identifier")
    return queue


def _close(queue: ExtensionQueue, state: QueueState, now: datetime) -> None:
    queue.state = state.value
    queue.finished_at = now


def _in_flight(session: Session, queue_id: str) -> ExtensionQueueItem | None:
    """The issued read not yet settled: an issued item, or a captured one whose run is pending."""
    issued = session.scalars(
        select(ExtensionQueueItem).where(
            ExtensionQueueItem.queue_id == queue_id,
            ExtensionQueueItem.state == QueueItemState.ISSUED.value,
        )
    ).first()
    if issued is not None:
        return issued
    return session.scalars(
        select(ExtensionQueueItem)
        .join(
            CollectionRun,
            CollectionRun.collection_run_id == ExtensionQueueItem.collection_run_id,
        )
        .where(
            ExtensionQueueItem.queue_id == queue_id,
            ExtensionQueueItem.state == QueueItemState.CAPTURED.value,
            CollectionRun.outcome == CollectionOutcome.PENDING.value,
        )
    ).first()


def _in_flight_wait(session: Session, item: ExtensionQueueItem, now: datetime) -> float:
    if item.state == QueueItemState.ISSUED.value:
        assert item.expires_at is not None
        return max(0.0, min(SETTLING_WAIT_S, (_aware(item.expires_at, now) - now).total_seconds()))
    return SETTLING_WAIT_S


def _item_view(item: ExtensionQueueItem, run: CollectionRun | None) -> QueueItemView:
    return QueueItemView(
        item_id=item.item_id,
        position=item.position,
        source_url=item.source_url,
        product_key=item.product_key,
        state=QueueItemState(item.state),
        collection_run_id=item.collection_run_id,
        run_outcome=None if run is None else CollectionOutcome(run.outcome),
        run_detail=None if run is None else run.detail,
    )


def _view(session: Session, queue_id: str) -> QueueView:
    queue = _queue(session, queue_id)
    rows = session.execute(
        select(ExtensionQueueItem, CollectionRun)
        .outerjoin(
            CollectionRun,
            CollectionRun.collection_run_id == ExtensionQueueItem.collection_run_id,
        )
        .where(ExtensionQueueItem.queue_id == queue_id)
        .order_by(ExtensionQueueItem.position)
    ).all()
    return QueueView(
        queue_id=queue.queue_id,
        supplier_key=queue.supplier_key,
        state=QueueState(queue.state),
        max_products=queue.max_products,
        interval_s=queue.interval_s,
        skip_collected=queue.skip_collected,
        items=tuple(_item_view(item, run) for item, run in rows),
    )
