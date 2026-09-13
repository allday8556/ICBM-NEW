import json
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.correlation import get_correlation_id, new_correlation_id
from app.core.errors import NotFoundError
from app.db.database import Database
from app.jobs.models import Job, JobAttempt, JobState
from app.jobs.policy import RetryPolicy
from app.jobs.records import JobAttemptRecord, JobRecord
from app.jobs.registry import JobRegistry

logger = logging.getLogger("icbm.jobs")


class JobService:
    """Application-facing job API. Routes enqueue here; they never execute jobs inline."""

    def __init__(
        self, db: Database, registry: JobRegistry, clock: Clock, default_policy: RetryPolicy
    ) -> None:
        self._db = db
        self._registry = registry
        self._clock = clock
        self._default_policy = default_policy
        self._notify: Callable[[], None] | None = None

    def set_worker_notifier(self, notify: Callable[[], None]) -> None:
        self._notify = notify

    def notify_worker(self) -> None:
        if self._notify is not None:
            self._notify()

    def policy_for(self, job_type: str) -> RetryPolicy:
        return self._registry.get(job_type).retry_policy or self._default_policy

    def enqueue(
        self,
        job_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        target_ref: str | None = None,
        session: Session | None = None,
    ) -> JobRecord:
        """Persist a QUEUED job under the active correlation ID.

        With ``session`` the insert joins the caller's unit of work, and the caller must call
        :meth:`notify_worker` after commit; otherwise the job commits and the worker is woken.
        """
        policy = self.policy_for(job_type)
        now = self._clock.now()
        job = Job(
            job_id=str(uuid.uuid4()),
            job_type=job_type,
            target_ref=target_ref,
            payload_json=json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True),
            state=JobState.QUEUED,
            attempt_count=0,
            max_attempts=policy.max_attempts,
            next_attempt_at=now,
            correlation_id=get_correlation_id() or new_correlation_id(),
            created_at=now,
            updated_at=now,
        )
        if session is None:
            with self._db.write() as own:
                own.add(job)
                own.flush()
                record = JobRecord.model_validate(job)
        else:
            session.add(job)
            session.flush()
            record = JobRecord.model_validate(job)
        logger.info(
            "job.enqueued",
            extra={
                "job_id": record.job_id,
                "job_type": job_type,
                "max_attempts": record.max_attempts,
                "retry_schedule_s": policy.schedule_s(),
            },
        )
        if session is None:
            self.notify_worker()
        return record

    def get(self, job_id: str) -> JobRecord:
        with self._db.read() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise NotFoundError("JOB_NOT_FOUND", f"job {job_id} does not exist")
            attempts = session.scalars(
                select(JobAttempt)
                .where(JobAttempt.job_id == job_id)
                .order_by(JobAttempt.attempt_no)
            ).all()
            record = JobRecord.model_validate(job)
            record.attempts = [JobAttemptRecord.model_validate(a) for a in attempts]
            return record

    def list_jobs(self, *, state: JobState | None = None, limit: int = 50) -> list[JobRecord]:
        query = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if state is not None:
            query = query.where(Job.state == state)
        with self._db.read() as session:
            return [JobRecord.model_validate(job) for job in session.scalars(query).all()]

    def count(self, *, job_type_prefix: str | None = None) -> int:
        query = select(func.count()).select_from(Job)
        if job_type_prefix is not None:
            query = query.where(Job.job_type.startswith(job_type_prefix, autoescape=True))
        with self._db.read() as session:
            return int(session.scalar(query) or 0)
