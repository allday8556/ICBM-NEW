"""The review producers' coverage watermark (Gate 2 G2-B, ADR-0016 §4, §7).

A count is authoritative only while its producer's coverage is **current**. Coverage is current
only when all five of these hold:

1. a full pass completed in **this** process run, recorded as the watermark;
2. that watermark is younger than a finite freshness bound, so a stalled periodic scheduler
   eventually makes coverage stale instead of leaving an old zero standing;
3. that pass's owner fence held: the owner's truth token was the same when the pass began and
   when the watermark was published, checked inside the publishing write unit;
4. no known indexing failure is unrecovered. Only a full pass that *started after* the failure
   clears it. "After" is proven by a monotonic failure counter read when the pass begins, never by
   comparing timestamps: under a frozen or coarse clock a failure recorded during a pass can carry
   the pass's own start time (review ``5807477351`` B2);
5. the owner still holds the truth that pass was fenced on: its truth token **now** equals the
   token stored with the watermark (G2-C, migration 0023). An owner that moved since the pass, by
   a fast-path write or by whole-owner churn, is covered again only by a later stable pass, so
   repeated owner movement can never keep an old watermark authoritative.

"Current" is derived on every read from the durable row, the process run and the clock. It is
never stored.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.core.clock import Clock
from app.db.database import Database
from app.review.models import ReviewCoverage

REVIEW_COVERAGE_NO_PASS_THIS_RUN: Final = "REVIEW_COVERAGE_NO_PASS_THIS_RUN"
REVIEW_COVERAGE_STALE: Final = "REVIEW_COVERAGE_STALE"
REVIEW_INDEX_FAILURE_UNRECOVERED: Final = "REVIEW_INDEX_FAILURE_UNRECOVERED"
# The owner's truth moved while a full pass ran: the pass cannot prove it covered it.
REVIEW_OWNER_MOVED_DURING_PASS: Final = "REVIEW_OWNER_MOVED_DURING_PASS"
# The owner's truth moved after the watermark's pass (G2-C): the watermark no longer covers it.
REVIEW_OWNER_MOVED_SINCE_PASS: Final = "REVIEW_OWNER_MOVED_SINCE_PASS"
REVIEW_OWNER_TRUTH_UNREADABLE: Final = "REVIEW_OWNER_TRUTH_UNREADABLE"
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

    def failures_recorded(self, producer: str) -> int:
        """How many known failures this producer has ever recorded. A pass reads it first."""
        with self._db.read() as session:
            row = session.get(ReviewCoverage, producer)
            return 0 if row is None else row.failures_recorded

    def record_pass(
        self,
        producer: str,
        *,
        process_run_id: str,
        started_at: datetime,
        failures_before: int,
        token_before: str,
        owner_token: Callable[[], str],
        correlation_id: str,
    ) -> bool:
        """A full pass that started at ``started_at`` has reconciled every scope it snapshotted.

        The owner fence (review 5807902325 B3) runs **inside this write unit**: no owner write of
        this process can land between it and the watermark. If the owner moved during the pass,
        the watermark is not renewed; instead a known failure is recorded, so coverage is not
        current until a later, stable pass. Returns whether the watermark was published.

        On publication, the known failure is cleared only if no failure was recorded since the
        pass began — only if the counter still reads ``failures_before``. A failure recorded
        during the pass, even at the same instant, stays unrecovered for a later pass."""
        now = self._clock.now()
        with self._db.write() as session:
            row = self._row(session, producer, now)
            if owner_token() != token_before:
                self._fail(session, row, producer, REVIEW_OWNER_MOVED_DURING_PASS, correlation_id)
                return False
            recovered = row.failure_at is not None and row.failures_recorded == failures_before
            recovered_code = row.failure_code
            row.process_run_id = process_run_id
            row.pass_started_at = started_at
            row.watermark_at = now
            # What the pass was fenced on: coverage compares the owner with it on every read.
            row.truth_digest = token_before
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
            return True

    def record_failure(self, producer: str, *, code: str, correlation_id: str) -> None:
        """A known indexing failure: it keeps coverage not current until a later full pass."""
        now = self._clock.now()
        with self._db.write() as session:
            self._fail(session, self._row(session, producer, now), producer, code, correlation_id)

    @staticmethod
    def _row(session: Session, producer: str, now: datetime) -> ReviewCoverage:
        row = session.get(ReviewCoverage, producer)
        if row is None:
            row = ReviewCoverage(
                producer=producer, full_passes=0, failures_recorded=0, updated_at=now
            )
            session.add(row)
        return row

    def _fail(
        self, session: Session, row: ReviewCoverage, producer: str, code: str, correlation_id: str
    ) -> None:
        now = self._clock.now()
        row.failure_at = now
        row.failure_code = code[:64]
        row.failures_recorded += 1
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

    def coverage(
        self,
        producer: str,
        *,
        process_run_id: str,
        max_age_s: float,
        owner_token: Callable[[], str],
    ) -> CoverageView:
        """The producer's coverage now. ``owner_token`` reads the owner's truth token **now**: a
        watermark fenced on another token no longer covers the owner (G2-C)."""
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
            else:
                reason = _owner_reason(row.truth_digest, owner_token)
            return CoverageView(
                producer=producer,
                current=reason is None,
                reason=reason,
                watermark_at=row.watermark_at,
                full_passes=row.full_passes,
                failure_code=row.failure_code,
            )


def _owner_reason(fenced: str | None, owner_token: Callable[[], str]) -> str | None:
    """Whether the owner still holds the truth the watermark was fenced on. A token that cannot
    be read proves nothing, so it is never current either."""
    if fenced is None:
        return REVIEW_OWNER_MOVED_SINCE_PASS
    try:
        now = owner_token()
    except Exception:
        return REVIEW_OWNER_TRUTH_UNREADABLE
    return None if now == fenced else REVIEW_OWNER_MOVED_SINCE_PASS
