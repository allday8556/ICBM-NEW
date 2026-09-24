"""The review producers' coverage watermark (Gate 2 G2-B, ADR-0016 §4, §7).

A count is authoritative only while its producer's coverage is **current**. Coverage is current
only when all three of these hold:

1. a full pass completed in **this** process run, recorded as the watermark;
2. that watermark is younger than a finite freshness bound, so a stalled periodic scheduler
   eventually makes coverage stale instead of leaving an old zero standing;
3. no known indexing failure is unrecovered. Only a full pass that *started after* the failure
   clears it.

"Current" is derived on every read from the durable row, the process run and the clock. It is
never stored.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.core.clock import Clock
from app.db.database import Database
from app.review.models import ReviewCoverage

REVIEW_COVERAGE_NO_PASS_THIS_RUN: Final = "REVIEW_COVERAGE_NO_PASS_THIS_RUN"
REVIEW_COVERAGE_STALE: Final = "REVIEW_COVERAGE_STALE"
REVIEW_INDEX_FAILURE_UNRECOVERED: Final = "REVIEW_INDEX_FAILURE_UNRECOVERED"
SYSTEM_ACTOR: Final = "system:review"


@dataclass(frozen=True)
class CoverageView:
    producer: str
    current: bool
    # Why it is not current, as a server code; None when it is.
    reason: str | None
    watermark_at: datetime | None
    full_passes: int
    failure_code: str | None


class ReviewCoverageStore:
    """The only writer of ``review_coverage``."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    def record_pass(
        self, producer: str, *, process_run_id: str, started_at: datetime, correlation_id: str
    ) -> None:
        """A full pass that started at ``started_at`` has completed: renew the watermark, and clear
        a known failure that happened before the pass began."""
        now = self._clock.now()
        with self._db.write() as session:
            row = session.get(ReviewCoverage, producer)
            if row is None:
                row = ReviewCoverage(producer=producer, full_passes=0, updated_at=now)
                session.add(row)
            recovered = row.failure_at is not None and row.failure_at <= started_at
            recovered_code = row.failure_code
            row.process_run_id = process_run_id
            row.pass_started_at = started_at
            row.watermark_at = now
            row.full_passes += 1
            if recovered:
                row.failure_at = None
                row.failure_code = None
            row.updated_at = now
            session.flush()
            if recovered:
                self._audit.append(
                    AuditEntry(
                        event_type=AuditEventType.REVIEW_COVERAGE_RECOVERED,
                        action="REVIEW_COVERAGE_RECOVERED",
                        actor=SYSTEM_ACTOR,
                        outcome=AuditOutcome.RECORDED,
                        target_ref=producer,
                        details={"recovered_failure": recovered_code},
                        correlation_id=correlation_id,
                    ),
                    session=session,
                )

    def record_failure(self, producer: str, *, code: str, correlation_id: str) -> None:
        """A known indexing failure: it keeps coverage not current until a later full pass."""
        now = self._clock.now()
        with self._db.write() as session:
            row = session.get(ReviewCoverage, producer)
            if row is None:
                row = ReviewCoverage(producer=producer, full_passes=0, updated_at=now)
                session.add(row)
            row.failure_at = now
            row.failure_code = code[:64]
            row.updated_at = now
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.REVIEW_COVERAGE_FAILURE_RECORDED,
                    action="REVIEW_COVERAGE_FAILURE_RECORDED",
                    actor=SYSTEM_ACTOR,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=producer,
                    reason_code=code[:64],
                    correlation_id=correlation_id,
                ),
                session=session,
            )

    def coverage(self, producer: str, *, process_run_id: str, max_age_s: float) -> CoverageView:
        with self._db.read() as session:
            row = session.get(ReviewCoverage, producer)
            if row is None:
                return CoverageView(
                    producer, False, REVIEW_COVERAGE_NO_PASS_THIS_RUN, None, 0, None
                )
            reason: str | None = None
            if row.failure_at is not None:
                reason = REVIEW_INDEX_FAILURE_UNRECOVERED
            elif row.process_run_id != process_run_id or row.watermark_at is None:
                reason = REVIEW_COVERAGE_NO_PASS_THIS_RUN
            elif self._clock.now() - row.watermark_at > timedelta(seconds=max_age_s):
                reason = REVIEW_COVERAGE_STALE
            return CoverageView(
                producer=producer,
                current=reason is None,
                reason=reason,
                watermark_at=row.watermark_at,
                full_passes=row.full_passes,
                failure_code=row.failure_code,
            )
