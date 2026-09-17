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
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collect.facts import FactsStatus
from app.collect.models import CollectionOutcome, CollectionRun
from app.core.clock import Clock
from app.core.errors import NotFoundError
from app.db.database import Database


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
                finished_at=None,
            )
        )
        session.flush()
        return run_id

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

    def for_job(self, job_id: str) -> CollectionRunRecord | None:
        with self._db.read() as session:
            row = session.scalars(
                select(CollectionRun).where(CollectionRun.job_id == job_id)
            ).first()
            return None if row is None else _record(row)


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
        finished_at=row.finished_at,
    )
