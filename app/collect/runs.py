"""The durable result identity of one collection (Issue #52 ruling 5706133893).

A run is opened with its job, in the same unit of work, so the operator is handed an identity the
moment the request is accepted rather than after the work finishes. It then reaches exactly one
terminal outcome, and that outcome is the whole contract:

``RECORDED``      a revision exists; the run names it and its ``facts_status``.
``NO_REVISION``   the run finished its work and there is nothing to append, because the source
                  stated no stable identity. This is a success: the job did what it was asked,
                  and a retry would read the same page and reach the same answer.
``FAILED``        the run could not finish. The job's own attempt history says why and whether it
                  will be retried.

``detail`` holds one of our own codes or the parser's reason for an unresolved identity. No page
content, no URL of a provider's making and no credential ever reaches this row.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.collect.facts import FactsStatus
from app.collect.models import CollectionOutcome, CollectionRun
from app.collect.shadow import DISABLED, FrozenRun, FrozenShadow, ShadowFreezer
from app.core.clock import Clock
from app.core.errors import NotFoundError, RateLimitedError
from app.db.database import Database
from app.jobs.models import Job

logger = logging.getLogger("icbm.collect")


class SameProductTooSoon(RateLimitedError):
    """The same product was read less than the profile's interval ago (ADR-0010 §4).

    Waiting is the answer, so this is rate limiting and not a fault: a job that meets it is
    rescheduled by the owner that already schedules every retry, and a run of its own stays open
    until its turn comes. Nothing was requested, so nothing has to be undone.
    """

    def __init__(self, seconds_remaining: float) -> None:
        super().__init__(
            "COLLECT_SAME_PRODUCT_TOO_SOON",
            f"this product was read too recently; {seconds_remaining:.0f} s remain",
        )
        self.seconds_remaining = seconds_remaining


@dataclass(frozen=True)
class PacingKey:
    """What the same-product interval is measured on (ADR-0010 §4).

    ``supplier_key`` scopes the whole key. A source product number is unique only inside the
    supplier that issued it (ARCHITECTURE §14: the identity is ``supplier_key`` *and*
    ``source_product_id``), so two suppliers that both number a product ``355`` are two products
    and must not pace each other.

    ``url`` is the normalized in-scope URL, which is all there is before anything has been read.
    ``source_product_id`` is the product the source itself states, when the supplier's own URL
    form makes it plain in advance or an earlier run has already proven it; from then on the
    interval follows the product rather than whichever accepted spelling of its URL was used.
    """

    supplier_key: str
    url: str
    source_product_id: str | None = None


@dataclass(frozen=True)
class CollectionRunRecord:
    """One run, exactly as the database holds it."""

    collection_run_id: str
    job_id: str
    correlation_id: str
    supplier_key: str
    source_url: str
    outcome: CollectionOutcome
    revision_id: str | None
    facts_status: FactsStatus | None
    detail: str | None
    requested_at: datetime
    product_read_at: datetime | None
    pacing_key: str | None
    source_product_id: str | None
    finished_at: datetime | None
    frozen: FrozenRun | None = None
    settled_by_recovery: bool | None = None


class CollectionRunStore:
    def __init__(
        self, db: Database, clock: Clock, *, shadow_freezer: ShadowFreezer | None = None
    ) -> None:
        self._db = db
        self._clock = clock
        # Who answers a run's shadow decision at its first reservation (ADR-0017 §10.1). Without
        # one every run freezes an explicit DISABLED, so no run is ever shadow-eligible by default.
        self._shadow_freezer = shadow_freezer

    def open(
        self,
        session: Session,
        *,
        job_id: str,
        correlation_id: str,
        supplier_key: str,
        source_url: str,
    ) -> str:
        """Record a submitted collection in the caller's unit of work and return its identity."""
        run_id = str(uuid.uuid4())
        session.add(
            CollectionRun(
                collection_run_id=run_id,
                job_id=job_id,
                correlation_id=correlation_id,
                supplier_key=supplier_key,
                source_url=source_url,
                outcome=CollectionOutcome.PENDING,
                revision_id=None,
                facts_status=None,
                detail=None,
                requested_at=self._clock.now(),
                product_read_at=None,
                pacing_key=None,
                source_product_id=None,
                finished_at=None,
            )
        )
        session.flush()
        return run_id

    def reserve_product_read(self, run_id: str, *, key: PacingKey, interval_s: float) -> FrozenRun:
        """Take one real product read, or refuse because the last one was too recent.

        Every attempt passes through here, a retry of this same run included: what is stored is
        the timestamp of the most recent real read, never a permit the run keeps. The check and
        the write are one transaction, so two runs racing for one product cannot both pass, and a
        restart sees what the earlier attempt committed. It is taken *before* the request, so a
        refusal has sent nothing.

        The **first** reservation also freezes the run's shadow decision, in this same unit
        (ADR-0017 §10.1): the switch entry in effect and the exact bundle it names, or DISABLED,
        and when that first read was reserved. A retry keeps what the first froze and never asks
        again, so a later switch or bundle change affects later runs only. The freeze only reads;
        if the freezer cannot answer, the run freezes DISABLED and its read goes ahead unchanged.
        """
        with self._db.write() as session:
            row = session.get(CollectionRun, run_id)
            if row is None:
                raise NotFoundError("COLLECT_RUN_UNKNOWN", "no collection run has that identifier")
            now = self._clock.now()
            remaining = _remaining(_last_read(session, key), now, interval_s)
            if remaining > 0:
                raise SameProductTooSoon(remaining)
            row.pacing_key = key.url
            row.product_read_at = now
            if row.shadow_decision is None:
                frozen = self._freeze(session, row.supplier_key)
                row.shadow_decision = frozen.decision
                row.shadow_switch_entry_id = frozen.switch_entry_id
                row.shadow_bundle_key = frozen.bundle_key
                row.first_product_read_at = now
            frozen_run = _frozen(row)
            assert frozen_run is not None
            return frozen_run

    def _freeze(self, session: Session, supplier_key: str) -> FrozenShadow:
        if self._shadow_freezer is None:
            return DISABLED
        try:
            return self._shadow_freezer(session, supplier_key)
        except Exception:
            logger.exception("collect.shadow_freeze_failed", extra={"supplier": supplier_key})
            return DISABLED

    def seconds_until_readable(self, key: PacingKey, *, interval_s: float) -> float:
        """How long this product must still be left alone. Read-only; reserves nothing."""
        with self._db.read() as session:
            return _remaining(_last_read(session, key), self._clock.now(), interval_s)

    def note_identity(self, run_id: str, *, source_product_id: str) -> None:
        """Record what the document identified, so later reads are paced on the product itself.

        Before the read the only thing available was the URL. Afterwards the source itself has
        said which product it is, and ADR-0010 §4 paces on that; the URL key is kept beside it,
        because a read that already happened under it still counts.
        """
        with self._db.write() as session:
            row = session.get(CollectionRun, run_id)
            if row is None:
                raise NotFoundError("COLLECT_RUN_UNKNOWN", "no collection run has that identifier")
            row.source_product_id = source_product_id

    def recorded(
        self,
        run_id: str,
        *,
        revision_id: str,
        facts_status: FactsStatus,
        recovered: bool = False,
    ) -> None:
        """``recovered`` says the recovery path settled it, from a revision an earlier attempt had
        already appended: the canonical history ADR-0017 §11.2 reads."""
        self._finish(
            run_id,
            CollectionOutcome.RECORDED,
            revision_id=revision_id,
            facts_status=facts_status,
            recovered=recovered,
        )

    def no_revision(self, run_id: str, *, reason: str) -> None:
        """The source stated no identity to record against. Nothing is appended, and that is the
        answer — not an error to retry into."""
        self._finish(run_id, CollectionOutcome.NO_REVISION, detail=reason)

    def failed(self, run_id: str, *, detail: str) -> None:
        self._finish(run_id, CollectionOutcome.FAILED, detail=detail)

    def _finish(
        self,
        run_id: str,
        outcome: CollectionOutcome,
        *,
        revision_id: str | None = None,
        facts_status: FactsStatus | None = None,
        detail: str | None = None,
        recovered: bool | None = None,
    ) -> None:
        with self._db.write() as session:
            row = session.get(CollectionRun, run_id)
            if row is None:
                raise NotFoundError("COLLECT_RUN_UNKNOWN", "no collection run has that identifier")
            if row.outcome != CollectionOutcome.PENDING:
                # A run reaches one outcome. A second attempt of the same job opens no new row and
                # never rewrites the answer the first one recorded.
                return
            row.outcome = outcome
            row.revision_id = revision_id
            row.facts_status = facts_status
            row.detail = detail
            row.finished_at = self._clock.now()
            if outcome == CollectionOutcome.RECORDED:
                row.settled_by_recovery = bool(recovered)

    def get(self, run_id: str) -> CollectionRunRecord:
        with self._db.read() as session:
            row = session.get(CollectionRun, run_id)
            if row is None:
                raise NotFoundError("COLLECT_RUN_UNKNOWN", "no collection run has that identifier")
            return _record(row)

    def recent(self, *, limit: int) -> tuple[CollectionRunRecord, ...]:
        """The newest runs, newest first, exactly as the database holds them (Gate 1 G1-E).

        Ordered by request time and then by identifier, so the order is total and a reload shows
        the same list. These rows are the only run history: nothing is copied anywhere else.
        """
        with self._db.read() as session:
            rows = session.scalars(
                select(CollectionRun)
                .order_by(CollectionRun.requested_at.desc(), CollectionRun.collection_run_id.desc())
                .limit(limit)
            ).all()
            return tuple(_record(row) for row in rows)

    def unsettled_job_ids(
        self, terminal_states: Sequence[str], *, limit: int = 500
    ) -> tuple[str, ...]:
        """The jobs whose runs are still waiting for an answer and can no longer run.

        This is the owner's half of the reconciliation join (Issue #52 rulings 5721367502 S1 and
        5721796080): a run with no outcome names the job it belongs to, and the job table says what
        became of that job. Which states a job never leaves is the job system's to define and is
        handed in; this knows only where its own rows are. Nothing is remembered in memory, so an
        inconsistency outlives the process that caused it, and an answered run is simply not here.

        The join happens **before** the bound, so what comes back is a page of inconsistencies and
        not a page of runs that merely might be one. Bounding the waiting runs first would let a
        page of jobs that are still perfectly alive hide an orphan behind them — the same page,
        every sweep, for ever. Settled runs leave this set, so bounded sweeps keep reaching further
        until nothing is left.
        """
        with self._db.read() as session:
            return tuple(
                session.scalars(
                    select(CollectionRun.job_id)
                    .join(Job, Job.job_id == CollectionRun.job_id)
                    .where(
                        CollectionRun.outcome == CollectionOutcome.PENDING,
                        Job.state.in_(terminal_states),
                    )
                    .order_by(CollectionRun.requested_at)
                    .limit(limit)
                ).all()
            )

    def for_job(self, job_id: str) -> CollectionRunRecord | None:
        with self._db.read() as session:
            row = session.scalars(
                select(CollectionRun).where(CollectionRun.job_id == job_id)
            ).first()
            return None if row is None else _record(row)


def _last_read(session: Session, key: "PacingKey") -> datetime | None:
    """The most recent real read of this product, by either name it may have been read under.

    The supplier is a constraint of the query and not a prefix of a string: what the contract
    says is ``(supplier_key, source_product_id)``, so that is what is asked of the database.
    """
    named = CollectionRun.pacing_key == key.url
    if key.source_product_id is not None:
        named = or_(named, CollectionRun.source_product_id == key.source_product_id)
    return session.scalar(
        select(func.max(CollectionRun.product_read_at)).where(
            CollectionRun.supplier_key == key.supplier_key,
            named,
            CollectionRun.product_read_at.is_not(None),
        )
    )


def _remaining(last: datetime | None, now: datetime, interval_s: float) -> float:
    """Seconds still owed to the interval, or 0 when the product may be read."""
    if last is None:
        return 0.0
    if last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    return max(0.0, (last + timedelta(seconds=interval_s) - now).total_seconds())


def _record(row: CollectionRun) -> CollectionRunRecord:
    return CollectionRunRecord(
        collection_run_id=row.collection_run_id,
        job_id=row.job_id,
        correlation_id=row.correlation_id,
        supplier_key=row.supplier_key,
        source_url=row.source_url,
        outcome=CollectionOutcome(row.outcome),
        revision_id=row.revision_id,
        facts_status=None if row.facts_status is None else FactsStatus(row.facts_status),
        detail=row.detail,
        requested_at=row.requested_at,
        product_read_at=row.product_read_at,
        pacing_key=row.pacing_key,
        source_product_id=row.source_product_id,
        finished_at=row.finished_at,
        frozen=_frozen(row),
        settled_by_recovery=row.settled_by_recovery,
    )


def _frozen(row: CollectionRun) -> FrozenRun | None:
    if row.shadow_decision is None or row.first_product_read_at is None:
        return None
    shadow = (
        FrozenShadow("ENABLED", row.shadow_switch_entry_id, row.shadow_bundle_key)
        if row.shadow_decision == "ENABLED"
        else DISABLED
    )
    return FrozenRun(shadow, row.first_product_read_at)
