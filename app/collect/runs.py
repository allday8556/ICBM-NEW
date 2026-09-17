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

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.collect.facts import FactsStatus
from app.collect.models import CollectionOutcome, CollectionRun
from app.core.clock import Clock
from app.core.errors import NotFoundError, RateLimitedError
from app.db.database import Database


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


class CollectionRunStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

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

    def reserve_product_read(self, run_id: str, *, key: PacingKey, interval_s: float) -> None:
        """Take one real product read, or refuse because the last one was too recent.

        Every attempt passes through here, a retry of this same run included: what is stored is
        the timestamp of the most recent real read, never a permit the run keeps. The check and
        the write are one transaction, so two runs racing for one product cannot both pass, and a
        restart sees what the earlier attempt committed. It is taken *before* the request, so a
        refusal has sent nothing.
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

    def recorded(self, run_id: str, *, revision_id: str, facts_status: FactsStatus) -> None:
        self._finish(
            run_id,
            CollectionOutcome.RECORDED,
            revision_id=revision_id,
            facts_status=facts_status,
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

    def get(self, run_id: str) -> CollectionRunRecord:
        with self._db.read() as session:
            row = session.get(CollectionRun, run_id)
            if row is None:
                raise NotFoundError("COLLECT_RUN_UNKNOWN", "no collection run has that identifier")
            return _record(row)

    def unsettled_job_ids(self, *, limit: int = 500) -> tuple[str, ...]:
        """The jobs whose runs are still waiting for an answer, from the durable rows alone.

        This is the owner's half of the reconciliation join (Issue #52 ruling 5721367502 S1): a run
        with no outcome names the job it belongs to, and the job table says whether that job can
        still run. Nothing is remembered in memory, so an inconsistency outlives the process that
        caused it, and an answered run is simply not here.
        """
        with self._db.read() as session:
            return tuple(
                session.scalars(
                    select(CollectionRun.job_id)
                    .where(CollectionRun.outcome == CollectionOutcome.PENDING)
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
    )
