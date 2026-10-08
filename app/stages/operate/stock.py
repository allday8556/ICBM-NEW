"""M6-B supplier stock recheck (ADR-0023 §4): re-collect what is listed, judge it, show it.

**Targets.** Only the source products bound to an ACTIVE SmartStore registration ICBM has not
deleted (owner decision `6033662015`, M6-05): each registration's frozen Items, through the
products owner's open binding, to ``(supplier_key, source_product_id)``.

**Mechanism.** COLLECT's own public command — :meth:`ProductCollectionService.submit` with the
source URL its current revision recorded — so the supplier transport, its pacing (one request at a
time, the same-product interval) and its refusals stay COLLECT's. A recheck is an ordinary
collection run and appends an ordinary immutable revision. OPERATE never imports a supplier adapter.

**Judge.** The run's revision states the stock (ADR-0010 §10): ON_SALE, SOLD_OUT or, when the
field itself is under review, REVIEW_REQUIRED. Availability is never proven by a supplier write.

**Bounds.** A cadence (default 6 hours), a per-round cap, one pending recheck per source product,
and a round that stops after consecutive refusals. A sold-out judgment changes no listing: the
operator decides (M6-07).
"""

import logging
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.platform.core.clock import Clock
from app.platform.core.errors import AppError
from app.platform.db.database import Database
from app.stages.collect.facts import Availability, FieldStatus
from app.stages.collect.models import CollectionOutcome
from app.stages.operate.stock_models import StockRecheck
from app.stages.register.model import RegistrationLifecycle
from app.stages.register.store import RegistrationStore

logger = logging.getLogger("icbm.operate.stock")

AUTO: Final = "AUTO"
OPERATOR: Final = "OPERATOR"
# The one-pending slot is reserved before COLLECT is asked, so a second request — another
# operator click, the schedule, another worker — never submits a second re-collection.
SUBMITTING: Final = "SUBMITTING"
REQUESTED: Final = "REQUESTED"
# A reservation an earlier process left before COLLECT answered: whether a run was opened is not
# known here, and any run it opened is an ordinary collection that settles on its own.
SUBMIT_INTERRUPTED: Final = "OPERATE_STOCK_SUBMIT_INTERRUPTED"
FINISHED: Final = "FINISHED"
RECORDED: Final = "RECORDED"
NO_REVISION: Final = "NO_REVISION"
FAILED: Final = "FAILED"
REFUSED: Final = "REFUSED"
STOCK_FIELD: Final = "stock"
# A round stops after this many refusals in a row (a lost supplier session, a pacing wall).
MAX_CONSECUTIVE_REFUSALS: Final = 3
NO_SOURCE_URL: Final = "OPERATE_STOCK_NO_SOURCE_URL"


class Collector(Protocol):
    """The COLLECT owner's public surface M6-B calls (``ProductCollectionService``)."""

    def submit(self, supplier_key: str, product_url: str) -> Any: ...

    def run(self, collection_run_id: str) -> Any: ...


class Revisions(Protocol):
    def current_recorded(self, supplier_key: str, source_product_id: str) -> Any: ...

    def get(self, revision_id: str) -> Any: ...


class SourceIdentities(Protocol):
    """Item → its open binding's source identity (the products owner, read-only)."""

    def __call__(self, item_id: str) -> tuple[str, str] | None: ...


@dataclass(frozen=True)
class ListedSource:
    supplier_key: str
    source_product_id: str
    registration_ids: tuple[str, ...]
    product_names: tuple[str, ...]
    # M6-E (ADR-0024 §4): the ACTIVE adopted listings that sell this source product.
    adoption_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StockStateView:
    supplier_key: str
    source_product_id: str
    registration_ids: tuple[str, ...]
    product_name: str | None
    availability: str | None
    availability_revision_id: str | None
    last_recheck_outcome: str | None
    last_recheck_error: str | None
    last_requested_at: datetime | None
    last_finished_at: datetime | None
    pending: bool
    # The server's reading of the pair: the source is sold out while the listing is live.
    needs_decision: bool


@dataclass(frozen=True)
class StockOverview:
    interval_s: float
    cap: int
    sources: tuple[StockStateView, ...]


def availability_of(revision: Any) -> str | None:
    """The judged availability one revision states (ADR-0010 §10), or ``None``."""
    if revision is None:
        return None
    stored = revision.fields.get(STOCK_FIELD)
    if stored is None:
        return None
    if stored.status is FieldStatus.REVIEW_REQUIRED:
        return Availability.REVIEW_REQUIRED.value
    value = getattr(stored.value, "availability", None)
    return None if value is None else Availability(value).value


class StockRecheckService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        registrations: RegistrationStore,
        source_identity: SourceIdentities,
        collector: Collector,
        revisions: Revisions,
        interval_s: float,
        cap: int,
        marketplace_key: str = "smartstore",
        adoptions: Any = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._registrations = registrations
        self._source_identity = source_identity
        self._collector = collector
        self._revisions = revisions
        self._interval_s = interval_s
        self._cap = cap
        self._marketplace_key = marketplace_key
        # M6-E (ADR-0024 §4): adopted listings are listed sources too.
        self._adoptions = adoptions
        self._lock = threading.Lock()

    @property
    def interval_s(self) -> float:
        return self._interval_s

    # ------------------------------------------------------------------ targets

    def listed_sources(self) -> tuple[ListedSource, ...]:
        """Every source product an ACTIVE, not ICBM-deleted registration sells, in stable order."""
        found: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
        for record in self._registrations.marketplace_registrations(self._marketplace_key):
            if record.lifecycle_state is not RegistrationLifecycle.ACTIVE:
                continue
            if any(d.deleted for d in self._registrations.deletions(record.registration_id)):
                continue
            payload = self._registrations.snapshot_payload(record.registration_snapshot_id) or {}
            name = payload.get("name")
            label = str(name["value"]) if isinstance(name, Mapping) and name.get("value") else None
            # The frozen Items of the registration's Snapshot (its ItemSnapshot rows).
            snapshot = self._registrations.snapshot(record.registration_snapshot_id)
            for item in () if snapshot is None else snapshot.items:
                identity = self._source_identity(item.item_id)
                if identity is None:
                    continue
                ids, names = found.setdefault(identity, ([], []))
                if record.registration_id not in ids:
                    ids.append(record.registration_id)
                if label and label not in names:
                    names.append(label)
        adopted: dict[tuple[str, str], list[str]] = {}
        for adoption in () if self._adoptions is None else self._adoptions.active():
            key = (adoption.supplier_key, adoption.source_product_id)
            adopted.setdefault(key, []).append(adoption.adoption_id)
            found.setdefault(key, ([], []))
        return tuple(
            ListedSource(key[0], key[1], tuple(ids), tuple(names), tuple(adopted.get(key, ())))
            for key, (ids, names) in sorted(found.items())
        )

    # ------------------------------------------------------------------ reads

    def overview(self) -> StockOverview:
        listed = self.listed_sources()
        with self._db.read() as session:
            views = [self._state(session, source) for source in listed]
        return StockOverview(interval_s=self._interval_s, cap=self._cap, sources=tuple(views))

    def availability(self, supplier_key: str, source_product_id: str) -> str | None:
        return availability_of(self._revisions.current_recorded(supplier_key, source_product_id))

    def due(self) -> bool:
        with self._db.read() as session:
            last = session.scalars(
                select(StockRecheck.requested_at)
                .order_by(StockRecheck.requested_at.desc())
                .limit(1)
            ).first()
        if last is None:
            return True
        return self._clock.now() - last >= timedelta(seconds=self._interval_s)

    # ------------------------------------------------------------------ rounds

    def request_round(self, *, trigger: str) -> tuple[str, ...]:
        """Ask COLLECT to re-collect up to ``cap`` listed source products, the least recently
        rechecked first. Returns the recheck ids opened. A refusal is recorded and counted; three
        in a row end the round."""
        if not self._lock.acquire(blocking=False):
            return ()
        try:
            opened: list[str] = []
            refusals = 0
            for source in self._order(self.listed_sources())[: self._cap]:
                if refusals >= MAX_CONSECUTIVE_REFUSALS:
                    break
                recheck_id = self._request(source, trigger)
                if recheck_id is None:
                    continue
                with self._db.read() as session:
                    row = session.get(StockRecheck, recheck_id)
                    refused = row is not None and row.outcome == REFUSED
                refusals = refusals + 1 if refused else 0
                opened.append(recheck_id)
            return tuple(opened)
        finally:
            self._lock.release()

    def settle(self) -> int:
        """Finish every requested recheck whose collection run has ended. Returns how many."""
        settled = 0
        with self._db.read() as session:
            pending = list(
                session.scalars(select(StockRecheck).where(StockRecheck.state == REQUESTED)).all()
            )
        for row in pending:
            try:
                run = self._collector.run(str(row.collection_run_id))
            except AppError as exc:
                self._finish(row.recheck_id, FAILED, error_code=exc.code)
                settled += 1
                continue
            outcome = CollectionOutcome(run.outcome)
            if outcome is CollectionOutcome.PENDING:
                continue
            if outcome is CollectionOutcome.RECORDED and run.revision_id:
                revision = self._revisions.get(run.revision_id)
                self._finish(
                    row.recheck_id,
                    RECORDED,
                    revision_id=run.revision_id,
                    availability=availability_of(revision),
                )
            elif outcome is CollectionOutcome.NO_REVISION:
                self._finish(row.recheck_id, NO_REVISION)
            else:
                self._finish(row.recheck_id, FAILED, error_code=f"COLLECT_RUN_{outcome.value}")
            settled += 1
        return settled

    # ------------------------------------------------------------------ internals

    def _order(self, sources: Sequence[ListedSource]) -> list[ListedSource]:
        with self._db.read() as session:
            last: dict[tuple[str, str], datetime] = {}
            for row in session.execute(
                select(
                    StockRecheck.supplier_key,
                    StockRecheck.source_product_id,
                    StockRecheck.requested_at,
                )
            ):
                key = (row[0], row[1])
                if key not in last or row[2] > last[key]:
                    last[key] = row[2]
        floor = datetime.min.replace(tzinfo=self._clock.now().tzinfo)
        return sorted(sources, key=lambda s: last.get((s.supplier_key, s.source_product_id), floor))

    def _request(self, source: ListedSource, trigger: str) -> str | None:
        current = self._revisions.current_recorded(source.supplier_key, source.source_product_id)
        recheck_id = str(uuid.uuid4())
        url = getattr(current, "source_url", None)
        if not url:
            return self._refusal(recheck_id, source, trigger, NO_SOURCE_URL)
        # Reserve the source's one pending slot first: only the request that holds it asks COLLECT
        # (GPT audit, PR #248). A slot already held means a recheck is pending; nothing is sent.
        try:
            with self._db.write() as session:
                session.add(
                    StockRecheck(
                        recheck_id=recheck_id,
                        supplier_key=source.supplier_key,
                        source_product_id=source.source_product_id,
                        trigger=trigger,
                        state=SUBMITTING,
                        outcome=None,
                        collection_run_id=None,
                        revision_id=None,
                        availability=None,
                        error_code=None,
                        requested_at=self._clock.now(),
                        finished_at=None,
                    )
                )
        except IntegrityError:
            return None
        try:
            submitted = self._collector.submit(source.supplier_key, url)
        except AppError as exc:
            self._finish(recheck_id, REFUSED, error_code=exc.code)
            return recheck_id
        except BaseException:
            self._finish(recheck_id, FAILED, error_code=SUBMIT_INTERRUPTED)
            raise
        with self._db.write() as session:
            row = session.get(StockRecheck, recheck_id)
            assert row is not None
            row.state, row.collection_run_id = REQUESTED, submitted.collection_run_id
        return recheck_id

    def settle_interrupted(self) -> int:
        """A reservation an earlier process left before COLLECT answered is finished as FAILED
        (startup): the slot is released, and nothing is concluded about the source."""
        with self._db.write() as session:
            rows = session.scalars(
                select(StockRecheck).where(StockRecheck.state == SUBMITTING)
            ).all()
            for row in rows:
                row.state, row.outcome, row.finished_at = FINISHED, FAILED, self._clock.now()
                row.error_code = SUBMIT_INTERRUPTED
            return len(rows)

    def _refusal(self, recheck_id: str, source: ListedSource, trigger: str, code: str) -> str:
        now = self._clock.now()
        with self._db.write() as session:
            session.add(
                StockRecheck(
                    recheck_id=recheck_id,
                    supplier_key=source.supplier_key,
                    source_product_id=source.source_product_id,
                    trigger=trigger,
                    state=FINISHED,
                    outcome=REFUSED,
                    collection_run_id=None,
                    revision_id=None,
                    availability=None,
                    error_code=code,
                    requested_at=now,
                    finished_at=now,
                )
            )
        return recheck_id

    def _finish(
        self,
        recheck_id: str,
        outcome: str,
        *,
        revision_id: str | None = None,
        availability: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._db.write() as session:
            row = session.get(StockRecheck, recheck_id)
            assert row is not None
            row.state, row.outcome, row.finished_at = FINISHED, outcome, self._clock.now()
            row.revision_id, row.availability, row.error_code = (
                revision_id,
                availability,
                error_code,
            )

    def _state(self, session: Any, source: ListedSource) -> StockStateView:
        rows = session.scalars(
            select(StockRecheck)
            .where(
                StockRecheck.supplier_key == source.supplier_key,
                StockRecheck.source_product_id == source.source_product_id,
            )
            .order_by(StockRecheck.requested_at.desc())
            .limit(5)
        ).all()
        last = next((r for r in rows if r.state == FINISHED), None)
        current = self._revisions.current_recorded(source.supplier_key, source.source_product_id)
        availability = availability_of(current)
        return StockStateView(
            supplier_key=source.supplier_key,
            source_product_id=source.source_product_id,
            registration_ids=source.registration_ids,
            product_name=source.product_names[0] if source.product_names else None,
            availability=availability,
            availability_revision_id=None if current is None else current.revision_id,
            last_recheck_outcome=None if last is None else last.outcome,
            last_recheck_error=None if last is None else last.error_code,
            last_requested_at=rows[0].requested_at if rows else None,
            last_finished_at=None if last is None else last.finished_at,
            pending=any(r.state in (SUBMITTING, REQUESTED) for r in rows),
            needs_decision=availability == Availability.SOLD_OUT.value,
        )


class StockRecheckScheduler:
    """Settles finished rechecks every tick, and opens a round when the cadence is due."""

    def __init__(self, service: StockRecheckService, *, tick_s: float = 60.0) -> None:
        self._service = service
        self._tick_s = tick_s
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._service.settle_interrupted()
        if self._service.interval_s <= 0:
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="icbm-stock-recheck", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._halt.wait(self._tick_s):
            try:
                self._service.settle()
                if self._service.due():
                    self._service.request_round(trigger=AUTO)
            except Exception:
                logger.exception("operate.stock_recheck_error")

    def stop(self) -> None:
        self._halt.set()
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None


__all__ = [
    "ListedSource",
    "StockOverview",
    "StockRecheckScheduler",
    "StockRecheckService",
    "StockStateView",
    "availability_of",
]
