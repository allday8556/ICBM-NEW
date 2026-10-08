"""M6-A listing-state sync (ADR-0023 §3): read every ACTIVE registration back and keep what it
showed.

**Read only.** The only provider call is the adopted origin-product read
(``SMARTSTORE_ORIGIN_PRODUCT_READ_V2``) with the CONNECT owner's committed bearer; nothing here
ever writes the marketplace (M6-01). A read that fails or answers nothing readable proves
nothing (M6-02).

**Evidence.** Every read appends one observation — only the sanitized listing fields — and touches
the registration's ``last_readback_at`` through the REGISTER owner. Only provider evidence moves a
registration to ``EXTERNALLY_REMOVED``: the documented sale status ``DELETE`` or an HTTP 404 of the
origin product (M6-03, ADR-0014 §14). A registration ICBM itself deleted (ADR-0018 §3.5) is not a
target: its absence is ICBM's own act, never an external removal.

**Drift is shown, never repaired** (M6-04): a price that differs from the frozen Snapshot, a listing
not on sale, a stock of zero, a seller code that does not match. One run at a time; a provider
rate limit ends the run without failing any registration.
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Final, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass
from app.platform.db.database import Database
from app.stages.operate.listing_models import ListingObservation, ListingSyncRun
from app.stages.register.model import AbsenceEvidence, RegistrationLifecycle
from app.stages.register.store import RegistrationRecord, RegistrationStore

logger = logging.getLogger("icbm.operate.listing")

AUTO: Final = "AUTO"
OPERATOR: Final = "OPERATOR"
RUNNING: Final = "RUNNING"
FINISHED: Final = "FINISHED"
COMPLETED: Final = "COMPLETED"
COMPLETED_WITH_FAILURES: Final = "COMPLETED_WITH_FAILURES"
SESSION_UNAVAILABLE: Final = "SESSION_UNAVAILABLE"
RATE_LIMITED: Final = "RATE_LIMITED"
INTERRUPTED: Final = "INTERRUPTED"
OBSERVED: Final = "OBSERVED"
NOT_FOUND: Final = "NOT_FOUND"
READ_FAILED: Final = "READ_FAILED"
# A provider answer the normalizer could not read, or a local failure while recording it.
UNREADABLE: Final = "OPERATE_LISTING_UNREADABLE"
# The pause between two provider reads of one pass (ICBM policy; no rate is documented).
PROVIDER_PAUSE_S: Final = 1.0
DELETED_SALE_STATUS: Final = "DELETE"
NOT_FOUND_STATUS: Final = 404
ACTOR: Final = "operate.listing_sync"

# The drift the server shows beside a listing (M6-04). Never repaired from here.
PRICE_DIFFERS: Final = "PRICE_DIFFERS_FROM_SNAPSHOT"
NOT_ON_SALE: Final = "NOT_ON_SALE"
STOCK_ZERO: Final = "STOCK_ZERO"
SELLER_CODE_MISMATCH: Final = "SELLER_CODE_MISMATCH"


class ListingReader(Protocol):
    """The adopted origin-product read, wired by the composition root (OPERATE never imports a
    marketplace adapter)."""

    def available(self) -> bool: ...

    def read(self, *, marketplace_product_id: str) -> Mapping[str, Any]: ...


class ListingFields(Protocol):
    """The normalized listing leaves the marketplace normalizer reads (read-only)."""

    @property
    def sale_status(self) -> str | None: ...

    @property
    def display_status(self) -> str | None: ...

    @property
    def sale_price(self) -> int | None: ...

    @property
    def stock_quantity(self) -> int | None: ...

    @property
    def seller_management_code(self) -> str | None: ...


Normalize = Callable[[Mapping[str, Any]], ListingFields]


class ListingSyncBusy(AppError):
    error_class = ErrorClass.CONFLICT


@dataclass(frozen=True)
class SyncRunView:
    run_id: str
    trigger: str
    state: str
    outcome: str | None
    targets: int
    observed: int
    failed: int
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class ListingStateView:
    # ``None`` for an adopted listing, which is no REGISTER registration (ADR-0024).
    registration_id: str | None
    marketplace_product_id: str
    seller_product_code: str
    product_name: str | None
    lifecycle_state: str
    deleted_by_icbm: bool
    snapshot_sale_price: int | None
    last_result: str | None = None
    sale_status: str | None = None
    display_status: str | None = None
    sale_price: int | None = None
    stock_quantity: int | None = None
    error_code: str | None = None
    observed_at: datetime | None = None
    drift: tuple[str, ...] = field(default_factory=tuple)
    # REGISTRATION (ICBM created it) or ADOPTED (ADR-0024), and the adoption it is.
    kind: str = "REGISTRATION"
    adoption_id: str | None = None


@dataclass(frozen=True)
class ListingSyncOverview:
    interval_s: float
    last_run: SyncRunView | None
    listings: tuple[ListingStateView, ...]


def _run_view(row: ListingSyncRun) -> SyncRunView:
    return SyncRunView(
        run_id=row.run_id,
        trigger=row.trigger,
        state=row.state,
        outcome=row.outcome,
        targets=row.targets,
        observed=row.observed,
        failed=row.failed,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def snapshot_sale_price(payload: Mapping[str, Any] | None) -> int | None:
    """The one frozen sale price of a single-price Snapshot, or ``None``."""
    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return None
    prices = {
        item["sale_price_krw"]
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("sale_price_krw"), int)
    }
    return prices.pop() if len(prices) == 1 else None


def drift_of(
    observation: Any, registration: RegistrationRecord | None, frozen: int | None
) -> tuple[str, ...]:
    if observation is None or observation.result != OBSERVED:
        return ()
    found: list[str] = []
    if (
        frozen is not None
        and observation.sale_price is not None
        and observation.sale_price != frozen
    ):
        found.append(PRICE_DIFFERS)
    if (observation.sale_status, observation.display_status) != ("SALE", "ON"):
        found.append(NOT_ON_SALE)
    if observation.stock_quantity == 0:
        found.append(STOCK_ZERO)
    if observation.seller_code_matches is False:
        found.append(SELLER_CODE_MISMATCH)
    return tuple(found)


class ListingSyncService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        registrations: RegistrationStore,
        reader: ListingReader,
        normalize: Normalize,
        interval_s: float,
        marketplace_key: str = "smartstore",
        seller_code: Callable[[str], str] | None = None,
        adoptions: Any = None,
        pause_s: float = PROVIDER_PAUSE_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._db = db
        self._clock = clock
        self._registrations = registrations
        self._reader = reader
        self._normalize = normalize
        self._interval_s = interval_s
        # The provider seller code of an ICBM listing identity (the marketplace's own projection;
        # SmartStore: ``seller_management_code``). The read-back carries the projection, never the
        # internal identity, so the comparison is made on it.
        self._seller_code = seller_code or (lambda identity: identity)
        # M6-E (ADR-0024 §4): the adopted listings, read back in the same pass.
        self._adoptions = adoptions
        self._pause_s = pause_s
        self._sleep = sleep
        self._last_read: float | None = None
        # The marketplace whose read-back this reader is (only SmartStore is wired).
        self._marketplace_key = marketplace_key
        self._lock = threading.Lock()

    @property
    def interval_s(self) -> float:
        return self._interval_s

    # ------------------------------------------------------------------ reads

    def overview(self) -> ListingSyncOverview:
        listings = [self._state(record) for record in self._targets(include_inactive=True)]
        if self._adoptions is not None:
            listings += [self._adopted_state(adoption) for adoption in self._adoptions.all()]
        with self._db.read() as session:
            last = session.scalars(
                select(ListingSyncRun).order_by(ListingSyncRun.started_at.desc()).limit(1)
            ).first()
            return ListingSyncOverview(
                interval_s=self._interval_s,
                last_run=None if last is None else _run_view(last),
                listings=tuple(listings),
            )

    def due(self) -> bool:
        """Whether the periodic pass is due: no finished run within one interval."""
        with self._db.read() as session:
            last = session.scalars(
                select(ListingSyncRun).order_by(ListingSyncRun.started_at.desc()).limit(1)
            ).first()
        if last is None:
            return True
        return self._clock.now() - last.started_at >= timedelta(seconds=self._interval_s)

    # ------------------------------------------------------------------ the pass

    def settle_interrupted(self) -> int:
        """A run an earlier process left RUNNING is finished as INTERRUPTED (startup)."""
        with self._db.write() as session:
            rows = session.scalars(
                select(ListingSyncRun).where(ListingSyncRun.state == RUNNING)
            ).all()
            for row in rows:
                row.state, row.outcome, row.finished_at = FINISHED, INTERRUPTED, self._clock.now()
            return len(rows)

    def sync(self, *, trigger: str, correlation_id: str) -> SyncRunView:
        """One pass over every ACTIVE registration ICBM has not deleted. One at a time.

        A run always ends: an unexpected failure of one registration is that registration's
        ``READ_FAILED`` and the pass goes on, and anything that escapes the pass still finishes
        the run as ``INTERRUPTED`` before it is raised, so the one-running slot is never left
        held (a held slot would stop every later sync until a restart).
        """
        if not self._lock.acquire(blocking=False):
            raise ListingSyncBusy(
                "OPERATE_LISTING_SYNC_RUNNING", "a listing sync is already running"
            )
        try:
            targets = self._targets(include_inactive=False)
            adopted = () if self._adoptions is None else self._adoptions.active()
            run_id = self._start(trigger, len(targets) + len(adopted), correlation_id)
            observed = failed = 0
            try:
                if not self._reader.available():
                    return self._finish(run_id, SESSION_UNAVAILABLE, 0, 0)
                visits = [
                    partial(self._visit, run_id, record, correlation_id) for record in targets
                ]
                visits += [
                    partial(self._visit_adopted, run_id, adoption, correlation_id)
                    for adoption in adopted
                ]
                for visit in visits:
                    result = visit()
                    if result == RATE_LIMITED:
                        return self._finish(run_id, RATE_LIMITED, observed, failed)
                    if result == READ_FAILED:
                        failed += 1
                    else:
                        observed += 1
            except BaseException:
                self._finish(run_id, INTERRUPTED, observed, failed)
                raise
            outcome = COMPLETED_WITH_FAILURES if failed else COMPLETED
            return self._finish(run_id, outcome, observed, failed)
        finally:
            self._lock.release()

    def _visit(self, run_id: str, record: RegistrationRecord, correlation_id: str) -> str:
        """Read one registration back and record what it showed: OBSERVED, NOT_FOUND,
        READ_FAILED, or RATE_LIMITED (nothing recorded; the run ends)."""
        written = False
        try:
            try:
                retained = self._read(record.marketplace_product_id)
            except AppError as exc:
                if exc.error_class is ErrorClass.RATE_LIMITED:
                    return RATE_LIMITED
                if exc.details.get("http_status") != NOT_FOUND_STATUS:
                    self._observe(run_id, record, READ_FAILED, error_code=exc.code)
                    return READ_FAILED
                written = True
                self._absent(run_id, record, correlation_id, {"http_status": NOT_FOUND_STATUS})
                return NOT_FOUND
            fields = self._normalize(retained)
            if fields.sale_status == DELETED_SALE_STATUS:
                written = True
                self._absent(run_id, record, correlation_id, {"statusType": DELETED_SALE_STATUS})
                return NOT_FOUND
            written = True
            self._observe(run_id, record, OBSERVED, fields=fields)
            with self._registrations.transaction() as unit:
                unit.record_readback(
                    record.registration_id, actor=ACTOR, correlation_id=correlation_id
                )
            return OBSERVED
        except Exception:
            # An unreadable answer or a failed local write proves nothing about the listing.
            logger.warning(
                "operate.listing_visit_failed",
                extra={"registration_id": record.registration_id},
                exc_info=True,
            )
            if not written:
                self._observe(run_id, record, READ_FAILED, error_code=UNREADABLE)
            return READ_FAILED

    def _read(self, marketplace_product_id: str) -> Mapping[str, Any]:
        """One paced provider read: it waits out the pause after the previous read (ICBM policy,
        as order ingest; unpaced reads met 429 after 16 listings on 2026-10-08)."""
        if self._last_read is not None:
            waited = time.monotonic() - self._last_read
            if waited < self._pause_s:
                self._sleep(self._pause_s - waited)
        try:
            return self._reader.read(marketplace_product_id=marketplace_product_id)
        finally:
            self._last_read = time.monotonic()

    def _visit_adopted(self, run_id: str, adoption: Any, correlation_id: str) -> str:
        """Read one adopted listing back (ADR-0024 §4): OBSERVED, NOT_FOUND, READ_FAILED or
        RATE_LIMITED. Provider evidence of removal ends the adoption; nothing is ever written to
        the marketplace."""
        assert self._adoptions is not None
        written = False
        try:
            try:
                retained = self._read(adoption.marketplace_product_id)
            except AppError as exc:
                if exc.error_class is ErrorClass.RATE_LIMITED:
                    return RATE_LIMITED
                if exc.details.get("http_status") != NOT_FOUND_STATUS:
                    self._adoptions.observe(
                        run_id, adoption.adoption_id, READ_FAILED, error_code=exc.code
                    )
                    return READ_FAILED
                written = True
                self._adoptions.observe(run_id, adoption.adoption_id, NOT_FOUND)
                self._adoptions.record_removed(
                    adoption.adoption_id, "HTTP_404", correlation_id=correlation_id
                )
                return NOT_FOUND
            fields = self._normalize(retained)
            if fields.sale_status == DELETED_SALE_STATUS:
                written = True
                self._adoptions.observe(run_id, adoption.adoption_id, NOT_FOUND)
                self._adoptions.record_removed(
                    adoption.adoption_id, "STATUS_DELETE", correlation_id=correlation_id
                )
                return NOT_FOUND
            written = True
            self._adoptions.observe(
                run_id,
                adoption.adoption_id,
                OBSERVED,
                sale_status=fields.sale_status,
                display_status=fields.display_status,
                sale_price=fields.sale_price,
                stock_quantity=fields.stock_quantity,
                seller_code_matches=(
                    None
                    if fields.seller_management_code is None
                    else fields.seller_management_code == adoption.seller_code
                ),
            )
            return OBSERVED
        except Exception:
            logger.warning(
                "operate.adopted_visit_failed",
                extra={"adoption_id": adoption.adoption_id},
                exc_info=True,
            )
            if not written:
                self._adoptions.observe(
                    run_id, adoption.adoption_id, READ_FAILED, error_code=UNREADABLE
                )
            return READ_FAILED

    def _adopted_state(self, adoption: Any) -> ListingStateView:
        assert self._adoptions is not None
        last = self._adoptions.last_observation(adoption.adoption_id)
        base = ListingStateView(
            registration_id=None,
            marketplace_product_id=adoption.marketplace_product_id,
            seller_product_code=adoption.seller_code,
            product_name=None,
            lifecycle_state=adoption.state,
            deleted_by_icbm=False,
            snapshot_sale_price=None,
            kind="ADOPTED",
            adoption_id=adoption.adoption_id,
        )
        if last is None:
            return base
        return ListingStateView(
            **{
                **base.__dict__,
                "last_result": last.result,
                "sale_status": last.sale_status,
                "display_status": last.display_status,
                "sale_price": last.sale_price,
                "stock_quantity": last.stock_quantity,
                "error_code": last.error_code,
                "observed_at": last.observed_at,
                "drift": drift_of(last, None, None),
            }
        )

    # ------------------------------------------------------------------ internals

    def _targets(self, *, include_inactive: bool) -> list[RegistrationRecord]:
        found: list[RegistrationRecord] = []
        # Every registration of the marketplace, never a capped window: a pass that skipped one
        # would still report completion (GPT audit, PR #247).
        for record in self._registrations.marketplace_registrations(self._marketplace_key):
            active = record.lifecycle_state is RegistrationLifecycle.ACTIVE
            deleted = any(d.deleted for d in self._registrations.deletions(record.registration_id))
            if include_inactive or (active and not deleted):
                found.append(record)
        return found

    def _start(self, trigger: str, targets: int, correlation_id: str) -> str:
        run_id = str(uuid.uuid4())
        try:
            with self._db.write() as session:
                session.add(
                    ListingSyncRun(
                        run_id=run_id,
                        trigger=trigger,
                        state=RUNNING,
                        outcome=None,
                        targets=targets,
                        observed=0,
                        failed=0,
                        correlation_id=correlation_id,
                        started_at=self._clock.now(),
                        finished_at=None,
                    )
                )
        except IntegrityError as exc:  # another process holds the one RUNNING slot
            raise ListingSyncBusy(
                "OPERATE_LISTING_SYNC_RUNNING", "a listing sync is already running"
            ) from exc
        return run_id

    def _finish(self, run_id: str, outcome: str, observed: int, failed: int) -> SyncRunView:
        with self._db.write() as session:
            row = session.get(ListingSyncRun, run_id)
            assert row is not None
            row.state, row.outcome, row.finished_at = FINISHED, outcome, self._clock.now()
            row.observed, row.failed = observed, failed
            session.flush()
            view = _run_view(row)
        logger.info(
            "operate.listing_sync_finished",
            extra={"run_id": run_id, "outcome": outcome, "observed": observed, "failed": failed},
        )
        return view

    def _observe(
        self,
        run_id: str,
        record: RegistrationRecord,
        result: str,
        *,
        fields: ListingFields | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._db.write() as session:
            session.add(
                ListingObservation(
                    observation_id=str(uuid.uuid4()),
                    run_id=run_id,
                    registration_id=record.registration_id,
                    result=result,
                    sale_status=None if fields is None else fields.sale_status,
                    display_status=None if fields is None else fields.display_status,
                    sale_price=None if fields is None else fields.sale_price,
                    stock_quantity=None if fields is None else fields.stock_quantity,
                    seller_code_matches=(
                        None
                        if fields is None or fields.seller_management_code is None
                        else fields.seller_management_code
                        == self._seller_code(record.seller_product_code)
                    ),
                    error_code=error_code,
                    observed_at=self._clock.now(),
                )
            )

    def _absent(
        self,
        run_id: str,
        record: RegistrationRecord,
        correlation_id: str,
        evidence: Mapping[str, Any],
    ) -> None:
        self._observe(run_id, record, NOT_FOUND)
        with self._registrations.transaction() as unit:
            unit.record_external_absence(
                record.registration_id,
                evidence_kind=AbsenceEvidence.PROVIDER_READ_BACK,
                sanitized_evidence={
                    "rule": "operate-listing-sync/v1",
                    "marketplace_product_id": record.marketplace_product_id,
                    **evidence,
                },
                recorded_by=ACTOR,
                correlation_id=correlation_id,
            )

    def _state(self, record: RegistrationRecord) -> ListingStateView:
        payload = self._registrations.snapshot_payload(record.registration_snapshot_id)
        name = payload.get("name") if isinstance(payload, Mapping) else None
        frozen = snapshot_sale_price(payload)
        deleted = any(d.deleted for d in self._registrations.deletions(record.registration_id))
        with self._db.read() as session:
            last = session.scalars(
                select(ListingObservation)
                .where(ListingObservation.registration_id == record.registration_id)
                .order_by(ListingObservation.observed_at.desc())
                .limit(1)
            ).first()
            base = ListingStateView(
                registration_id=record.registration_id,
                marketplace_product_id=record.marketplace_product_id,
                seller_product_code=record.seller_product_code,
                product_name=(
                    str(name["value"]) if isinstance(name, Mapping) and name.get("value") else None
                ),
                lifecycle_state=record.lifecycle_state.value,
                deleted_by_icbm=deleted,
                snapshot_sale_price=frozen,
            )
            if last is None:
                return base
            return ListingStateView(
                **{
                    **base.__dict__,
                    "last_result": last.result,
                    "sale_status": last.sale_status,
                    "display_status": last.display_status,
                    "sale_price": last.sale_price,
                    "stock_quantity": last.stock_quantity,
                    "error_code": last.error_code,
                    "observed_at": last.observed_at,
                    "drift": drift_of(last, record, frozen),
                }
            )


class ListingSyncScheduler:
    """The periodic pass (ADR-0023 §3: automatic cadence plus "지금 동기화"), on its own thread."""

    def __init__(self, service: ListingSyncService, *, tick_s: float = 60.0) -> None:
        self._service = service
        self._tick_s = tick_s
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._service.settle_interrupted()
        if self._service.interval_s <= 0:
            return  # disabled by policy
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="icbm-listing-sync", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._halt.wait(self._tick_s):
            try:
                if self._service.due():
                    self._service.sync(trigger=AUTO, correlation_id=f"listing-sync-{uuid.uuid4()}")
            except ListingSyncBusy:
                continue
            except Exception:
                logger.exception("operate.listing_sync_error")

    def stop(self) -> None:
        self._halt.set()
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None


__all__ = [
    "ListingStateView",
    "ListingSyncBusy",
    "ListingSyncOverview",
    "ListingSyncScheduler",
    "ListingSyncService",
    "SyncRunView",
    "drift_of",
    "snapshot_sale_price",
]
