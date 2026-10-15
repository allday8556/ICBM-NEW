"""The StoreCatalog owner (ADR-0042 S1a, §5): bring a store's whole catalog into ICBM, read only.

**One sync** of the connected store:

1. a run row opens (``as_of`` = its start, ADR-0042 §5.2);
2. the marketplace source enumerates every page of the catalog. Only a full, consistent
   enumeration is ``COMPLETE``; anything else ends the run with what it was (``INCOMPLETE``,
   ``UNAVAILABLE`` or ``ERROR``) and concludes nothing (SCP-04);
3. each enumerated listing gets an identity row the first time it is seen, and each one is read by
   its origin read, paced at one provider call a second (ADR-0023 §3). The search carries no
   documented field beyond identities and the seller code (E3 ``NOT_CAPTURED``) and no documented
   change signal (E5), so every listing is read every sync: none is assumed unchanged;
4. a listing the catalog knows that the enumeration did **not** list is read too, for provider
   evidence of its end: the sale status ``DELETE`` or a 404 ends it (SCP-08). Absence alone never
   does — it is only ``NOT_SEEN`` (「스토어에서 확인 안 됨」). A live listing found there means the
   enumeration did not cover the whole store, so the run is ``INCOMPLETE``;
5. the run is ``COMPLETE`` only when the enumeration was complete and every listing read answered
   (an observation or a provider removal).

**What is stored** is the §5.3 allowlist the source hands over (SCP-03), as one content-addressed
field set with its per-group SHA-256 digests, and one append-only observation per listing per
sync. Nothing is written to any marketplace: the only provider calls are the source's adopted
reads (M6-01).

**What a sync proves.** A ``COMPLETE`` snapshot proves the presence of what it read. It is never
remote absence for ADR-0014 §10/§14/§28 and frees no ``UNKNOWN`` scope (SCP-05); as duplicate
evidence it is fresh for 60 minutes from ``as_of`` and only until an ICBM mutation of the store
starts (:meth:`StoreCatalogService.snapshot`; consumed by S3b, ADR-0042 §6).

One sync per store at a time; a ``429`` or a lost session ends the run as ``UNAVAILABLE``.
"""

import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, PolicyBlockedError
from app.platform.db.database import Database
from app.stages.operate.catalog.facts import (
    COMPLETE,
    INCOMPLETE,
    OBSERVED,
    RATE_LIMITED,
    READ_FAILED,
    REMOVED,
    SESSION_UNAVAILABLE,
    UNAVAILABLE,
    CatalogEntry,
    CatalogSource,
    ListingRead,
)
from app.stages.operate.catalog.models import (
    CatalogFieldSet,
    CatalogListing,
    CatalogListingEnding,
    CatalogObservation,
    CatalogSyncRun,
)

logger = logging.getLogger("icbm.operate.catalog")

AUTO: Final = "AUTO"
OPERATOR: Final = "OPERATOR"
CONNECT: Final = "CONNECT"
PRE_REGISTRATION: Final = "PRE_REGISTRATION"
TRIGGERS: Final = frozenset({AUTO, OPERATOR, CONNECT, PRE_REGISTRATION})
RUNNING: Final = "RUNNING"
FINISHED: Final = "FINISHED"
INTERRUPTED: Final = "INTERRUPTED"

# ADR-0042 §5.2 (owner 6190978708 Q1): a COMPLETE snapshot is duplicate evidence for 60 minutes
# from its as_of. The target policy will hold the audited value when S3b consumes it.
CATALOG_MAX_AGE: Final = timedelta(minutes=60)
# The pause between two provider calls of one sync (ICBM policy; no rate is documented).
PROVIDER_PAUSE_S: Final = 1.0
# A sync that did not complete is tried again after this long, not after a whole interval.
RETRY_AFTER_S: Final = 300.0

FRESH: Final = "FRESH"
STALE: Final = "STALE"
NONE: Final = "NONE"

# The listing states a view derives (never stored).
LISTED: Final = "LISTED"
NOT_SEEN: Final = "NOT_SEEN"
ENDED: Final = "ENDED"
UNCONFIRMED: Final = "UNCONFIRMED"

# Run error codes of the owner itself.
DETAIL_READ_FAILED: Final = "STORE_CATALOG_DETAIL_READ_FAILED"
OUTSIDE_ENUMERATION: Final = "STORE_CATALOG_LISTING_OUTSIDE_ENUMERATION"
SYNC_INTERRUPTED: Final = "STORE_CATALOG_SYNC_INTERRUPTED"
UNREADABLE: Final = "STORE_CATALOG_LISTING_UNREADABLE"


class StoreCatalogBusy(AppError):
    error_class = ErrorClass.CONFLICT


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def field_digests(canonical: Mapping[str, Any]) -> dict[str, str]:
    """The SHA-256 of each §5.3 group of one field set, for change detection (ADR-0042 §5.4)."""
    return {str(key): _sha256(_canonical(value)) for key, value in sorted(canonical.items())}


@dataclass(frozen=True)
class CatalogRunView:
    run_id: str
    marketplace_key: str
    marketplace_account_id: str
    trigger: str
    state: str
    outcome: str | None
    error_code: str | None
    total_elements: int | None
    pages_read: int
    listings_enumerated: int
    details_target: int
    details_done: int
    details_failed: int
    as_of: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class CatalogListingView:
    """One store listing as the catalog knows it: its derived state and its latest read fields.

    ``fields`` and ``observed_at`` are from the latest observation that read the listing; they
    are never filled in from anything else.
    """

    listing_id: str
    origin_product_no: str
    channel_product_no: str | None
    state: str
    last_result: str | None
    sale_status: str | None
    display_status: str | None
    sale_price: int | None
    stock_quantity: int | None
    seller_management_code: str | None
    seller_barcode: str | None
    fields: Mapping[str, Any] | None
    observed_at: datetime | None
    ended_evidence: str | None


@dataclass(frozen=True)
class CatalogStatus:
    """What the screens show beside any store data (SCP-07): the store, the running sync and its
    progress, the last sync whatever its outcome, and the last COMPLETE sync."""

    marketplace_key: str
    marketplace_account_id: str | None
    interval_s: float
    running: CatalogRunView | None
    last_run: CatalogRunView | None
    last_complete: CatalogRunView | None
    listing_count: int | None


@dataclass(frozen=True)
class CatalogSnapshot:
    """The current duplicate-evidence snapshot of one store (ADR-0042 §5.2, §6).

    ``freshness`` is ``NONE`` without a COMPLETE sync, ``STALE`` when it is older than the bound
    or an ICBM mutation of the store started after it, else ``FRESH``. Only a ``FRESH`` snapshot
    may ever be evidence, and only for a new Intent's duplicate preflight (SCP-05, SCP-06).
    """

    freshness: str
    run: CatalogRunView | None
    listings: tuple[CatalogListingView, ...]


def _run_view(row: CatalogSyncRun) -> CatalogRunView:
    return CatalogRunView(
        run_id=row.run_id,
        marketplace_key=row.marketplace_key,
        marketplace_account_id=row.marketplace_account_id,
        trigger=row.trigger,
        state=row.state,
        outcome=row.outcome,
        error_code=row.error_code,
        total_elements=row.total_elements,
        pages_read=row.pages_read,
        listings_enumerated=row.listings_enumerated,
        details_target=row.details_target,
        details_done=row.details_done,
        details_failed=row.details_failed,
        as_of=row.started_at,
        finished_at=row.finished_at,
    )


class StoreCatalogService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        source: CatalogSource,
        account_of: Callable[[str], str | None],
        interval_s: float,
        retry_after_s: float = RETRY_AFTER_S,
        pause_s: float = PROVIDER_PAUSE_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._db = db
        self._clock = clock
        self._source = source
        # The one canonical account of the marketplace bound to its committed identity.
        self._account_of = account_of
        self._interval_s = interval_s
        self._retry_after_s = retry_after_s
        self._pause_s = pause_s
        self._sleep = sleep
        self._last_call: float | None = None
        self._lock = threading.Lock()

    @property
    def marketplace_key(self) -> str:
        return self._source.marketplace_key

    @property
    def interval_s(self) -> float:
        return self._interval_s

    def account(self) -> str | None:
        return self._account_of(self.marketplace_key)

    # ------------------------------------------------------------------ scheduling

    def due(self) -> str | None:
        """The trigger of the automatic sync that is due now, or ``None``.

        Nothing is due without a bound store and a readable session. The first sync of a store is
        due at once (``CONNECT``: the first sync after connecting starts by itself); afterwards a
        sync is due one interval after the last COMPLETE one, and a sync that did not complete is
        retried after ``retry_after_s``.
        """
        account = self.account()
        if account is None or not self._source.available():
            return None
        with self._db.read() as session:
            last = self._last(session, account)
            if last is None:
                return CONNECT
            if last.state == RUNNING:
                return None
            wait = self._interval_s if last.outcome == COMPLETE else self._retry_after_s
            if self._clock.now() - last.started_at >= timedelta(seconds=wait):
                return AUTO
        return None

    def settle_interrupted(self) -> int:
        """A run an earlier process left RUNNING is finished as INTERRUPTED (startup)."""
        with self._db.write() as session:
            rows = session.scalars(
                select(CatalogSyncRun).where(CatalogSyncRun.state == RUNNING)
            ).all()
            for row in rows:
                row.state, row.outcome, row.finished_at = FINISHED, INTERRUPTED, self._clock.now()
                row.error_code = SYNC_INTERRUPTED
            return len(rows)

    # ------------------------------------------------------------------ the sync

    def sync(self, *, trigger: str, correlation_id: str) -> CatalogRunView:
        """One read-only sync of the bound store's whole catalog. One at a time.

        A run always ends: anything that escapes the pass finishes it as ``INTERRUPTED`` before it
        is raised, so the one-running slot is never left held.
        """
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown catalog sync trigger {trigger!r}")
        account = self.account()
        if account is None:
            raise PolicyBlockedError(
                "STORE_CATALOG_STORE_NOT_CONNECTED",
                "connect and bind the store before its catalog can be synced",
            )
        if not self._lock.acquire(blocking=False):
            raise StoreCatalogBusy(
                "STORE_CATALOG_SYNC_RUNNING", "a catalog sync is already running"
            )
        try:
            run_id = self._start(account, trigger, correlation_id)
            try:
                return self._pass(run_id, account)
            except BaseException:
                self._finish(run_id, INTERRUPTED, SYNC_INTERRUPTED)
                raise
        finally:
            self._lock.release()

    def _pass(self, run_id: str, account: str) -> CatalogRunView:
        if not self._source.available():
            return self._finish(run_id, UNAVAILABLE, "SMARTSTORE_SESSION_UNAVAILABLE")
        enumeration = self._source.enumerate(pause=self._pace)
        self._progress(
            run_id,
            total_elements=enumeration.total_elements,
            pages_read=enumeration.pages_read,
        )
        if enumeration.outcome != COMPLETE:
            return self._finish(
                run_id, enumeration.outcome, enumeration.error_code or "STORE_CATALOG_ENUMERATION"
            )
        listed = self._identities(run_id, account, enumeration.entries)
        unseen = self._unseen(account, {listing_id for listing_id, _ in listed})
        targets: list[tuple[str, str, CatalogEntry | None]] = [
            (listing_id, entry.origin_product_no, entry) for listing_id, entry in listed
        ]
        targets += [(listing_id, origin, None) for listing_id, origin in unseen]
        self._progress(run_id, listings_enumerated=len(listed), details_target=len(targets))
        done = failed = outside = 0
        for listing_id, origin, entry in targets:
            self._pace()
            read = self._read(origin)
            if read.result in (RATE_LIMITED, SESSION_UNAVAILABLE):
                return self._finish(run_id, UNAVAILABLE, read.error_code or read.result)
            self._observe(run_id, listing_id, entry, read)
            if read.result == READ_FAILED:
                failed += 1
            else:
                done += 1
                if entry is None and read.result == OBSERVED:
                    outside += 1
            self._progress(run_id, details_done=done, details_failed=failed)
        if failed:
            return self._finish(run_id, INCOMPLETE, DETAIL_READ_FAILED)
        if outside:
            return self._finish(run_id, INCOMPLETE, OUTSIDE_ENUMERATION)
        return self._finish(run_id, COMPLETE, None)

    def _pace(self) -> None:
        """Wait out the pause after the previous provider call of this owner (ICBM policy)."""
        if self._last_call is not None:
            waited = time.monotonic() - self._last_call
            if waited < self._pause_s:
                self._sleep(self._pause_s - waited)
        self._last_call = time.monotonic()

    def _read(self, origin_product_no: str) -> ListingRead:
        try:
            return self._source.read(origin_product_no)
        except Exception:
            # A source that raised proves nothing about the listing; the run records a failure.
            logger.warning(
                "operate.catalog_read_failed",
                extra={"origin_product_no": origin_product_no},
                exc_info=True,
            )
            return ListingRead(READ_FAILED, error_code=UNREADABLE)

    # ------------------------------------------------------------------ rows

    def _start(self, account: str, trigger: str, correlation_id: str) -> str:
        run_id = str(uuid.uuid4())
        try:
            with self._db.write() as session:
                session.add(
                    CatalogSyncRun(
                        run_id=run_id,
                        marketplace_key=self.marketplace_key,
                        marketplace_account_id=account,
                        trigger=trigger,
                        state=RUNNING,
                        outcome=None,
                        error_code=None,
                        contract_version=self._source.contract_version,
                        total_elements=None,
                        pages_read=0,
                        listings_enumerated=0,
                        details_target=0,
                        details_done=0,
                        details_failed=0,
                        correlation_id=correlation_id,
                        started_at=self._clock.now(),
                        finished_at=None,
                    )
                )
        except IntegrityError as exc:  # another process holds this store's running slot
            raise StoreCatalogBusy(
                "STORE_CATALOG_SYNC_RUNNING", "a catalog sync is already running"
            ) from exc
        return run_id

    def _progress(self, run_id: str, **counts: int | None) -> None:
        with self._db.write() as session:
            row = session.get(CatalogSyncRun, run_id)
            assert row is not None
            for name, value in counts.items():
                setattr(row, name, value)

    def _finish(self, run_id: str, outcome: str, error_code: str | None) -> CatalogRunView:
        with self._db.write() as session:
            row = session.get(CatalogSyncRun, run_id)
            assert row is not None
            row.state, row.outcome, row.finished_at = FINISHED, outcome, self._clock.now()
            row.error_code = error_code
            session.flush()
            view = _run_view(row)
        logger.info(
            "operate.catalog_sync_finished",
            extra={"run_id": run_id, "outcome": outcome, "error_code": error_code},
        )
        return view

    def _identities(
        self, run_id: str, account: str, entries: Iterable[CatalogEntry]
    ) -> list[tuple[str, CatalogEntry]]:
        """The listing id of every enumerated entry, in provider order; new identities are
        recorded once and never change."""
        found: list[tuple[str, CatalogEntry]] = []
        with self._db.write() as session:
            known = {
                row.origin_product_no: row.listing_id
                for row in session.scalars(
                    select(CatalogListing).where(
                        CatalogListing.marketplace_key == self.marketplace_key,
                        CatalogListing.marketplace_account_id == account,
                    )
                )
            }
            now = self._clock.now()
            for entry in entries:
                listing_id = known.get(entry.origin_product_no)
                if listing_id is None:
                    listing_id = str(uuid.uuid4())
                    session.add(
                        CatalogListing(
                            listing_id=listing_id,
                            marketplace_key=self.marketplace_key,
                            marketplace_account_id=account,
                            origin_product_no=entry.origin_product_no,
                            first_seen_run_id=run_id,
                            first_seen_at=now,
                        )
                    )
                    known[entry.origin_product_no] = listing_id
                found.append((listing_id, entry))
        return found

    def _unseen(self, account: str, listed: set[str]) -> list[tuple[str, str]]:
        """Known listings the enumeration did not list and no provider evidence has ended."""
        with self._db.read() as session:
            ended = set(session.scalars(select(CatalogListingEnding.listing_id)))
            rows = session.scalars(
                select(CatalogListing)
                .where(
                    CatalogListing.marketplace_key == self.marketplace_key,
                    CatalogListing.marketplace_account_id == account,
                )
                .order_by(CatalogListing.first_seen_at, CatalogListing.origin_product_no)
            ).all()
            return [
                (row.listing_id, row.origin_product_no)
                for row in rows
                if row.listing_id not in listed and row.listing_id not in ended
            ]

    def _observe(
        self, run_id: str, listing_id: str, entry: CatalogEntry | None, read: ListingRead
    ) -> None:
        now = self._clock.now()
        fields = read.fields if read.result == OBSERVED else None
        digest: str | None = None
        with self._db.write() as session:
            if fields is not None:
                text = _canonical(dict(fields.canonical))
                digest = _sha256(text)
                if session.get(CatalogFieldSet, digest) is None:
                    session.add(
                        CatalogFieldSet(
                            digest=digest,
                            fields_version=fields.fields_version,
                            fields_json=text,
                            field_digests_json=_canonical(field_digests(fields.canonical)),
                            recorded_at=now,
                        )
                    )
                    session.flush()
            session.add(
                CatalogObservation(
                    observation_id=str(uuid.uuid4()),
                    run_id=run_id,
                    listing_id=listing_id,
                    result=read.result if read.result in (OBSERVED, REMOVED) else READ_FAILED,
                    enumerated=entry is not None,
                    channel_product_no=None if entry is None else entry.channel_product_no,
                    listed_seller_code=None
                    if entry is None
                    else _bounded(entry.listed_seller_code),
                    sale_status=None if fields is None else fields.sale_status,
                    display_status=None if fields is None else fields.display_status,
                    sale_price=None if fields is None else fields.sale_price,
                    stock_quantity=None if fields is None else fields.stock_quantity,
                    seller_management_code=None
                    if fields is None
                    else fields.seller_management_code,
                    seller_barcode=None if fields is None else fields.seller_barcode,
                    field_set_digest=digest,
                    removal_evidence=read.removal_evidence if read.result == REMOVED else None,
                    error_code=(
                        (read.error_code or UNREADABLE)
                        if read.result not in (OBSERVED, REMOVED)
                        else None
                    ),
                    observed_at=now,
                )
            )
            if read.result == REMOVED and session.get(CatalogListingEnding, listing_id) is None:
                assert read.removal_evidence is not None
                session.add(
                    CatalogListingEnding(
                        listing_id=listing_id,
                        run_id=run_id,
                        evidence=read.removal_evidence,
                        ended_at=now,
                    )
                )

    # ------------------------------------------------------------------ reads

    def _last(
        self, session: Session, account: str, *, outcome: str | None = None
    ) -> CatalogSyncRun | None:
        query = select(CatalogSyncRun).where(
            CatalogSyncRun.marketplace_key == self.marketplace_key,
            CatalogSyncRun.marketplace_account_id == account,
        )
        if outcome is not None:
            query = query.where(CatalogSyncRun.outcome == outcome)
        return session.scalars(query.order_by(CatalogSyncRun.started_at.desc()).limit(1)).first()

    def status(self) -> CatalogStatus:
        account = self.account()
        if account is None:
            return CatalogStatus(
                self.marketplace_key, None, self._interval_s, None, None, None, None
            )
        with self._db.read() as session:
            last = self._last(session, account)
            complete = self._last(session, account, outcome=COMPLETE)
            count = None
            if complete is not None:
                count = len(
                    session.scalars(
                        select(CatalogObservation.observation_id).where(
                            CatalogObservation.run_id == complete.run_id,
                            CatalogObservation.enumerated.is_(True),
                            CatalogObservation.result == OBSERVED,
                        )
                    ).all()
                )
            return CatalogStatus(
                marketplace_key=self.marketplace_key,
                marketplace_account_id=account,
                interval_s=self._interval_s,
                running=None if last is None or last.state != RUNNING else _run_view(last),
                last_run=None if last is None else _run_view(last),
                last_complete=None if complete is None else _run_view(complete),
                listing_count=count,
            )

    def listings(self) -> tuple[CatalogListingView, ...]:
        """Every listing of the bound store with its derived state and its latest read fields.

        The state is derived from the listing's latest observation, which only a fully enumerated
        sync records: ``LISTED`` when that enumeration listed it, ``NOT_SEEN`` (「스토어에서 확인
        안 됨」) when it did not, ``ENDED`` once provider evidence ended it, and ``UNCONFIRMED``
        for an identity no read has reached yet. Absence alone never ends a listing (SCP-08).
        """
        account = self.account()
        if account is None:
            return ()
        with self._db.read() as session:
            return tuple(
                self._listing_view(session, row) for row in self._store_listings(session, account)
            )

    def snapshot(
        self,
        *,
        max_age: timedelta = CATALOG_MAX_AGE,
        mutation_started_at: datetime | None = None,
    ) -> CatalogSnapshot:
        """The listings of the last COMPLETE sync and whether that sync is still fresh evidence.

        Only what that sync's enumeration listed and read is in it, as that sync read it; an
        ended listing is not.
        """
        account = self.account()
        if account is None:
            return CatalogSnapshot(NONE, None, ())
        with self._db.read() as session:
            complete = self._last(session, account, outcome=COMPLETE)
            if complete is None:
                return CatalogSnapshot(NONE, None, ())
            stale = self._clock.now() - complete.started_at > max_age or (
                mutation_started_at is not None and mutation_started_at >= complete.started_at
            )
            observed = {
                row.listing_id: row
                for row in session.scalars(
                    select(CatalogObservation).where(
                        CatalogObservation.run_id == complete.run_id,
                        CatalogObservation.enumerated.is_(True),
                        CatalogObservation.result == OBSERVED,
                    )
                )
            }
            views = tuple(
                self._listing_view(session, row, as_read=observed[row.listing_id])
                for row in self._store_listings(session, account)
                if row.listing_id in observed
            )
            return CatalogSnapshot(STALE if stale else FRESH, _run_view(complete), views)

    def _store_listings(self, session: Session, account: str) -> list[CatalogListing]:
        return list(
            session.scalars(
                select(CatalogListing)
                .where(
                    CatalogListing.marketplace_key == self.marketplace_key,
                    CatalogListing.marketplace_account_id == account,
                )
                .order_by(CatalogListing.first_seen_at, CatalogListing.origin_product_no)
            )
        )

    def _listing_view(
        self, session: Session, row: CatalogListing, *, as_read: CatalogObservation | None = None
    ) -> CatalogListingView:
        """One listing's view: as one sync read it (``as_read``, a snapshot), or as of now."""
        ending = session.get(CatalogListingEnding, row.listing_id)
        last = (
            as_read
            or session.scalars(
                select(CatalogObservation)
                .where(CatalogObservation.listing_id == row.listing_id)
                .order_by(CatalogObservation.observed_at.desc())
                .limit(1)
            ).first()
        )
        if as_read is None and ending is not None:
            state = ENDED
        elif last is None:
            state = UNCONFIRMED
        else:
            state = LISTED if last.enumerated else NOT_SEEN
        observation = (
            as_read
            or session.scalars(
                select(CatalogObservation)
                .where(
                    CatalogObservation.listing_id == row.listing_id,
                    CatalogObservation.result == OBSERVED,
                )
                .order_by(CatalogObservation.observed_at.desc())
                .limit(1)
            ).first()
        )
        channel = (
            as_read.channel_product_no
            if as_read is not None
            else session.scalars(
                select(CatalogObservation.channel_product_no)
                .where(
                    CatalogObservation.listing_id == row.listing_id,
                    CatalogObservation.channel_product_no.is_not(None),
                )
                .order_by(CatalogObservation.observed_at.desc())
                .limit(1)
            ).first()
        )
        fields: Mapping[str, Any] | None = None
        if observation is not None and observation.field_set_digest is not None:
            content = session.get(CatalogFieldSet, observation.field_set_digest)
            fields = None if content is None else json.loads(content.fields_json)
        return CatalogListingView(
            listing_id=row.listing_id,
            origin_product_no=row.origin_product_no,
            channel_product_no=channel,
            state=state,
            last_result=None if last is None else last.result,
            sale_status=None if observation is None else observation.sale_status,
            display_status=None if observation is None else observation.display_status,
            sale_price=None if observation is None else observation.sale_price,
            stock_quantity=None if observation is None else observation.stock_quantity,
            seller_management_code=(
                None if observation is None else observation.seller_management_code
            ),
            seller_barcode=None if observation is None else observation.seller_barcode,
            fields=fields,
            observed_at=None if observation is None else observation.observed_at,
            ended_evidence=None if ending is None or as_read is not None else ending.evidence,
        )


def _bounded(code: str | None) -> str | None:
    return code if code is not None and len(code) <= 64 else None


class StoreCatalogScheduler:
    """The periodic sync and the requested ones (ADR-0042 §5.4), on its own thread.

    The loop wakes every ``tick_s`` and whenever :meth:`wake` is called — the CONNECT routes wake
    it after a successful connect, so a store's first sync starts by itself (§0 a1, b1).
    :meth:`request` runs one requested sync (「지금 동기화」) in the background.
    """

    def __init__(self, service: StoreCatalogService, *, tick_s: float = 60.0) -> None:
        self._service = service
        self._tick_s = tick_s
        self._halt = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._service.settle_interrupted()
        if self._service.interval_s <= 0:
            return  # disabled by policy
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="icbm-store-catalog", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """Look for a due sync now instead of at the next tick (after CONNECT)."""
        self._wake.set()

    def request(self, *, trigger: str = OPERATOR) -> bool:
        """Start one sync in the background now. ``False`` when none can start: no bound store,
        or one is already running (that one is the sync the caller asked for)."""
        status = self._service.status()
        if status.marketplace_account_id is None or status.running is not None:
            return False

        def run() -> None:
            try:
                self._service.sync(trigger=trigger, correlation_id=f"store-catalog-{uuid.uuid4()}")
            except StoreCatalogBusy:
                return
            except Exception:
                logger.exception("operate.catalog_sync_error")

        threading.Thread(target=run, name="icbm-store-catalog-request", daemon=True).start()
        return True

    def _loop(self) -> None:
        while not self._halt.is_set():
            self._wake.wait(self._tick_s)
            self._wake.clear()
            if self._halt.is_set():
                return
            try:
                trigger = self._service.due()
                if trigger is not None:
                    self._service.sync(
                        trigger=trigger, correlation_id=f"store-catalog-{uuid.uuid4()}"
                    )
            except StoreCatalogBusy:
                continue
            except Exception:
                logger.exception("operate.catalog_sync_error")

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None


__all__ = [
    "AUTO",
    "CATALOG_MAX_AGE",
    "CONNECT",
    "ENDED",
    "FRESH",
    "LISTED",
    "NONE",
    "NOT_SEEN",
    "OPERATOR",
    "PRE_REGISTRATION",
    "STALE",
    "UNCONFIRMED",
    "CatalogListingView",
    "CatalogRunView",
    "CatalogSnapshot",
    "CatalogStatus",
    "StoreCatalogBusy",
    "StoreCatalogScheduler",
    "StoreCatalogService",
    "field_digests",
]
