"""Executes durable jobs one attempt at a time.

The runner depends only on the Database, registry, clock and audit log — not on FastAPI or an
event loop — so the worker can later move to a separate process without touching business
contracts (ADR-0002 migration boundary).
"""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.core.clock import Clock
from app.core.correlation import correlation_scope
from app.core.errors import AUTO_RETRYABLE, ErrorClass, classify
from app.db.database import Database
from app.jobs.models import (
    DUE_STATES,
    TERMINAL_STATE_NAMES,
    TERMINAL_STATES,
    AttemptOutcome,
    Job,
    JobAttempt,
    JobState,
)
from app.jobs.policy import RetryPolicy
from app.jobs.registry import JobContext, JobRegistry, TerminalJob

logger = logging.getLogger("icbm.jobs")

WORKER_ACTOR = "system:job-worker"
_MAX_MESSAGE = 2000
# How many job ids one reconciliation query asks about. SQLite binds each one, so the sweep asks
# in batches rather than building a statement whose size depends on how much is unsettled.
_RECONCILE_BATCH = 400


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    job_type: str
    attempt_no: int
    max_attempts: int
    correlation_id: str
    target_ref: str | None
    payload: Mapping[str, Any]
    scheduled_for: datetime
    started_at: datetime


@dataclass(frozen=True)
class AttemptResult:
    job_id: str
    job_type: str
    attempt_no: int
    state: JobState
    next_attempt_at: datetime | None
    error_class: ErrorClass | None
    error_code: str | None


class JobRunner:
    def __init__(
        self,
        db: Database,
        registry: JobRegistry,
        clock: Clock,
        audit: AuditLog,
        *,
        default_policy: RetryPolicy,
        worker_id: str,
        lease_s: float,
    ) -> None:
        self._db = db
        self._registry = registry
        self._clock = clock
        self._audit = audit
        self._default_policy = default_policy
        self._worker_id = worker_id
        self._lease = timedelta(seconds=lease_s)

    def _policy_for(self, job_type: str) -> RetryPolicy:
        definition = self._registry.find(job_type)
        if definition is not None and definition.retry_policy is not None:
            return definition.retry_policy
        return self._default_policy

    def seconds_until_next_due(self) -> float | None:
        with self._db.read() as session:
            next_at = session.scalar(
                select(func.min(Job.next_attempt_at)).where(Job.state.in_(DUE_STATES))
            )
        if next_at is None:
            return None
        return max(0.0, (next_at - self._clock.now()).total_seconds())

    def run_next(self) -> AttemptResult | None:
        """Claim and execute the next due job. Returns None when nothing is due."""
        claimed = self._claim()
        if claimed is None:
            return None
        return self._execute(claimed)

    def _claim(self) -> ClaimedJob | None:
        with self._db.write() as session:
            now = self._clock.now()
            job = session.scalars(
                select(Job)
                .where(Job.state.in_(DUE_STATES), Job.next_attempt_at <= now)
                .order_by(Job.next_attempt_at, Job.created_at)
                .limit(1)
            ).first()
            if job is None:
                return None
            scheduled_for = job.next_attempt_at or now
            job.state = JobState.RUNNING
            job.attempt_count += 1
            job.started_at = job.started_at or now
            job.next_attempt_at = None
            job.lease_owner = self._worker_id
            job.lease_expires_at = now + self._lease
            job.updated_at = now
            session.add(
                JobAttempt(
                    job_id=job.job_id,
                    attempt_no=job.attempt_count,
                    scheduled_for=scheduled_for,
                    started_at=now,
                )
            )
            return ClaimedJob(
                job_id=job.job_id,
                job_type=job.job_type,
                attempt_no=job.attempt_count,
                max_attempts=job.max_attempts,
                correlation_id=job.correlation_id,
                target_ref=job.target_ref,
                payload=json.loads(job.payload_json),
                scheduled_for=scheduled_for,
                started_at=now,
            )

    def _execute(self, claimed: ClaimedJob) -> AttemptResult:
        with correlation_scope(claimed.correlation_id):
            lag_ms = (claimed.started_at - claimed.scheduled_for).total_seconds() * 1000
            logger.info(
                "job.attempt.started",
                extra={
                    "job_id": claimed.job_id,
                    "job_type": claimed.job_type,
                    "attempt_no": claimed.attempt_no,
                    "max_attempts": claimed.max_attempts,
                    "scheduled_for": claimed.scheduled_for.isoformat(),
                    "start_lag_ms": round(lag_ms, 1),
                },
            )
            failure: Exception | None = None
            try:
                definition = self._registry.get(claimed.job_type)
                definition.handler(
                    JobContext(
                        job_id=claimed.job_id,
                        job_type=claimed.job_type,
                        attempt_no=claimed.attempt_no,
                        max_attempts=claimed.max_attempts,
                        correlation_id=claimed.correlation_id,
                        target_ref=claimed.target_ref,
                        payload=claimed.payload,
                    )
                )
            except Exception as exc:  # the job boundary: no handler failure reaches the worker
                failure = exc
            return self._finish(claimed, failure)

    def _finish(self, claimed: ClaimedJob, failure: Exception | None) -> AttemptResult:
        policy = self._policy_for(claimed.job_type)
        error_class: ErrorClass | None = None
        code: str | None = None
        message: str | None = None
        if failure is not None:
            error_class, code, message = classify(failure)
            message = message[:_MAX_MESSAGE]

        with self._db.write() as session:
            now = self._clock.now()
            job = session.get(Job, claimed.job_id)
            if job is None:  # pragma: no cover - rows are never deleted
                raise RuntimeError(f"claimed job {claimed.job_id} vanished")
            attempt = session.scalars(
                select(JobAttempt).where(
                    JobAttempt.job_id == claimed.job_id,
                    JobAttempt.attempt_no == claimed.attempt_no,
                )
            ).one()
            attempt.finished_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now

            next_attempt_at: datetime | None = None
            if error_class is None:
                attempt.outcome = AttemptOutcome.SUCCEEDED
                job.state = JobState.SUCCEEDED
                job.finished_at = now
            else:
                attempt.outcome = AttemptOutcome.FAILED
                attempt.error_class = error_class
                attempt.error_code = code
                attempt.error_message = message
                job.last_error_class = error_class
                job.last_error_code = code
                job.last_error_message = message
                if policy.allows_retry(error_class, claimed.attempt_no):
                    next_attempt_at = now + policy.delay_after(claimed.attempt_no)
                    attempt.retry_at = next_attempt_at
                    job.state = JobState.RETRY_SCHEDULED
                    job.next_attempt_at = next_attempt_at
                else:
                    job.state = JobState.DEAD
                    job.finished_at = now
                    self._record_dead_letter(session, job, error_class, code)
            state = JobState(job.state)

        self._settle_owner(
            state,
            TerminalJob(
                job_id=claimed.job_id,
                job_type=claimed.job_type,
                state=state.value,
                attempt_no=claimed.attempt_no,
                correlation_id=claimed.correlation_id,
                target_ref=claimed.target_ref,
                error_class=None if error_class is None else error_class.value,
                error_code=code,
            ),
        )
        self._log_result(claimed, state, error_class, code, next_attempt_at)
        return AttemptResult(
            job_id=claimed.job_id,
            job_type=claimed.job_type,
            attempt_no=claimed.attempt_no,
            state=state,
            next_attempt_at=next_attempt_at,
            error_class=error_class,
            error_code=code,
        )

    def _settle_owner(self, state: JobState, terminal: TerminalJob) -> None:
        """Tell the owner its job is over, once that is committed (ARCHITECTURE §8).

        A job that can no longer run must leave nothing of its owner's waiting on it: whatever the
        handler was in the middle of, including a failure it never expected, ends here. The job's
        own bookkeeping is already durable, so a hook that fails changes none of it — it is logged
        and never raised into the worker.

        This call is the prompt path, not the authoritative one. Whether it happened, and whether
        it worked, is not remembered anywhere: what the owner did not settle stays visible in the
        owner's own rows, and :meth:`reconcile_terminal_owners` finds it there.
        """
        if state not in TERMINAL_STATES:
            return
        definition = self._registry.find(terminal.job_type)
        if definition is None or definition.on_terminal is None:
            return
        with correlation_scope(terminal.correlation_id):
            try:
                definition.on_terminal(terminal)
            except Exception:
                logger.exception(
                    "job.terminal_owner_failed",
                    extra={"job_id": terminal.job_id, "job_type": terminal.job_type},
                )

    # ------------------------------------------------------------------ reconciliation

    def reconcile_terminal_owners(self) -> int:
        """Settle every owner the database still shows waiting on a job that can no longer run.

        Correctness does not rest on the hook in :meth:`_finish` having been called, or having
        worked (Issue #52 ruling 5721367502 S2/S4). Both halves of the inconsistency are already
        written down — the job's terminal state, and the owner's unsettled row — so this asks each
        owner which jobs its rows are waiting on, reads what the job table says about exactly those
        jobs, and hands it the ones that are over. Nothing in memory takes part, so a crash before
        the hook, a hook that raised, and a process that never reached either all converge here.

        It is safe to repeat and safe to interrupt: an owner that has settled is waiting on nothing
        and is never asked again, and one told twice about the same job settles nothing new. A
        settlement that fails again is logged and leaves the inconsistency exactly as discoverable
        as it was, for the next sweep.

        What counts as over is this layer's to define and is handed to the owner, which asks with
        it. An owner may bound how much it answers with; what it answers with is then a page of
        inconsistencies, so every sweep settles some and the next one reaches further.

        Returns how many terminal jobs it found an owner still waiting on — what it discovered,
        not what converged, which is the same question the next sweep asks.
        """
        found = 0
        for job_type in self._registry.job_types():
            definition = self._registry.find(job_type)
            if definition is None or definition.unsettled_owned_jobs is None:
                continue
            waiting_on = definition.unsettled_owned_jobs(TERMINAL_STATE_NAMES)
            for terminal in self._unsettled_terminal(job_type, waiting_on):
                with correlation_scope(terminal.correlation_id):
                    logger.warning(
                        "job.owner_left_unsettled",
                        extra={
                            "job_id": terminal.job_id,
                            "job_type": terminal.job_type,
                            "state": terminal.state,
                            "error_class": terminal.error_class,
                            "error_code": terminal.error_code,
                        },
                    )
                self._settle_owner(JobState(terminal.state), terminal)
                found += 1
        return found

    def _unsettled_terminal(self, job_type: str, waiting_on: Sequence[str]) -> list[TerminalJob]:
        """Of the jobs this owner is waiting on, the ones the job table says are already over."""
        job_ids = list(dict.fromkeys(waiting_on))
        if not job_ids:
            return []
        terminal: list[TerminalJob] = []
        with self._db.read() as session:
            for start in range(0, len(job_ids), _RECONCILE_BATCH):
                batch = job_ids[start : start + _RECONCILE_BATCH]
                rows = session.scalars(
                    select(Job).where(
                        Job.job_id.in_(batch),
                        Job.job_type == job_type,
                        Job.state.in_(TERMINAL_STATES),
                    )
                ).all()
                terminal.extend(_terminal_job(session, job) for job in rows)
        return terminal

    def _record_dead_letter(
        self, session: Any, job: Job, error_class: ErrorClass, code: str | None
    ) -> None:
        reason = (
            "RETRY_CAP_REACHED" if error_class in AUTO_RETRYABLE else "NON_RETRYABLE_ERROR_CLASS"
        )
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.JOB_DEAD_LETTERED,
                action="JOB_DEAD_LETTER",
                actor=WORKER_ACTOR,
                outcome=AuditOutcome.RECORDED,
                target_ref=f"job:{job.job_id}",
                reason_code=code,
                correlation_id=job.correlation_id,
                before={"state": JobState.RUNNING},
                after={"state": JobState.DEAD},
                details={
                    "job_type": job.job_type,
                    "attempt_count": job.attempt_count,
                    "max_attempts": job.max_attempts,
                    "error_class": error_class,
                    "dead_letter_reason": reason,
                },
            ),
            session=session,
        )

    def _log_result(
        self,
        claimed: ClaimedJob,
        state: JobState,
        error_class: ErrorClass | None,
        code: str | None,
        next_attempt_at: datetime | None,
    ) -> None:
        extra: dict[str, Any] = {
            "job_id": claimed.job_id,
            "job_type": claimed.job_type,
            "attempt_no": claimed.attempt_no,
            "max_attempts": claimed.max_attempts,
            "state": state,
            "error_class": error_class,
            "error_code": code,
        }
        with correlation_scope(claimed.correlation_id):
            if state is JobState.SUCCEEDED:
                logger.info("job.attempt.succeeded", extra=extra)
            elif state is JobState.RETRY_SCHEDULED and next_attempt_at is not None:
                extra["retry_at"] = next_attempt_at.isoformat()
                extra["retry_delay_s"] = (next_attempt_at - self._clock.now()).total_seconds()
                logger.warning("job.attempt.failed", extra=extra)
            else:
                logger.error("job.dead_lettered", extra=extra)

    def recover_interrupted(self) -> int:
        """Startup recovery for attempts cut off by a previous process exit.

        Exactly one worker exists per process (ADR-0002), so a RUNNING job seen at startup has
        no live owner. Idempotent jobs are rescheduled; others go to dead-letter as UNKNOWN
        because their external effect cannot be proven either way.
        """
        recovered = 0
        ended: list[TerminalJob] = []
        with self._db.write() as session:
            now = self._clock.now()
            for job in session.scalars(select(Job).where(Job.state == JobState.RUNNING)).all():
                recovered += 1
                attempt = session.scalars(
                    select(JobAttempt).where(
                        JobAttempt.job_id == job.job_id,
                        JobAttempt.attempt_no == job.attempt_count,
                    )
                ).first()
                definition = self._registry.find(job.job_type)
                resumable = definition is not None and definition.idempotent
                error_class = ErrorClass.TRANSIENT if resumable else ErrorClass.UNKNOWN
                code = "WORKER_INTERRUPTED" if resumable else "INTERRUPTED_OUTCOME_UNKNOWN"
                message = "attempt interrupted by application shutdown"
                if attempt is not None:
                    attempt.outcome = AttemptOutcome.INTERRUPTED
                    attempt.finished_at = now
                    attempt.error_class = error_class
                    attempt.error_code = code
                    attempt.error_message = message
                job.last_error_class = error_class
                job.last_error_code = code
                job.last_error_message = message
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
                if resumable and job.attempt_count < job.max_attempts:
                    job.state = JobState.RETRY_SCHEDULED
                    job.next_attempt_at = now
                    if attempt is not None:
                        attempt.retry_at = now
                else:
                    job.state = JobState.DEAD
                    job.finished_at = now
                    self._record_dead_letter(session, job, error_class, code)
                    ended.append(
                        TerminalJob(
                            job_id=job.job_id,
                            job_type=job.job_type,
                            state=JobState.DEAD.value,
                            attempt_no=job.attempt_count,
                            correlation_id=job.correlation_id,
                            target_ref=job.target_ref,
                            error_class=error_class.value,
                            error_code=code,
                        )
                    )
                with correlation_scope(job.correlation_id):
                    logger.warning(
                        "job.recovered_after_interruption",
                        extra={"job_id": job.job_id, "state": job.state, "error_code": code},
                    )
        for (
            terminal
        ) in ended:  # an interrupted job that will never run again ends its owner's work too
            self._settle_owner(JobState.DEAD, terminal)
        return recovered


def _terminal_job(session: Session, job: Job) -> TerminalJob:
    """Rebuild what the hook was handed, from committed rows only (ruling 5721367502 G5).

    Everything here is the job system's own: the state it will not leave, the attempt that ended
    it, its correlation and its target. The classification is that last attempt's own outcome
    rather than ``jobs.last_error_*``, because that column keeps the most recent *failure* — a job
    that failed once and then succeeded still carries it, and calling that the end of the job
    would be a reinterpretation, which G5 forbids.
    """
    attempt = session.scalars(
        select(JobAttempt).where(
            JobAttempt.job_id == job.job_id, JobAttempt.attempt_no == job.attempt_count
        )
    ).first()
    if attempt is not None:
        succeeded = attempt.outcome == AttemptOutcome.SUCCEEDED
        error_class = None if succeeded else attempt.error_class
        error_code = None if succeeded else attempt.error_code
    elif JobState(job.state) is JobState.SUCCEEDED:  # pragma: no cover - every attempt is recorded
        error_class, error_code = None, None
    else:  # pragma: no cover - as above; the job's own last error is all there would be
        error_class, error_code = job.last_error_class, job.last_error_code
    return TerminalJob(
        job_id=job.job_id,
        job_type=job.job_type,
        state=JobState(job.state).value,
        attempt_no=job.attempt_count,
        correlation_id=job.correlation_id,
        target_ref=job.target_ref,
        error_class=error_class,
        error_code=error_code,
    )
