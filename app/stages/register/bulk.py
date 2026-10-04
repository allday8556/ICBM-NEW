"""Durable, strictly sequential orchestration of ordinary single-product CREATE jobs."""

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities.jobs.models import Job, JobState
from app.capabilities.jobs.registry import TerminalJob
from app.capabilities.jobs.service import JobService
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, NotFoundError
from app.platform.db.database import Database
from app.stages.register.contracts import BulkCreateFailure, BulkCreateResult
from app.stages.register.execution import ACTIVE_JOB_STATES, CREATE_JOB_TYPE, target_ref
from app.stages.register.model import IntentState
from app.stages.register.models import (
    RegistrationBulkItem,
    RegistrationBulkRun,
    RegistrationIntent,
)

RUNNING: Final = "RUNNING"
COMPLETED: Final = "COMPLETED"
COMPLETED_WITH_FAILURES: Final = "COMPLETED_WITH_FAILURES"
WAITING: Final = "WAITING"
QUEUED: Final = "QUEUED"
SUCCEEDED: Final = "SUCCEEDED"
FAILED: Final = "FAILED"

logger = logging.getLogger("icbm.register.bulk")


class BulkRegistrationConflict(AppError):
    error_class = ErrorClass.CONFLICT


@dataclass(frozen=True)
class BulkCreatePlan:
    intent_id: str
    payload: Mapping[str, Any]


def request_fingerprint(intent_ids: Sequence[str]) -> str:
    encoded = json.dumps(list(intent_ids), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class BulkRegistrationService:
    """Own the durable chain; at most one item in a run is QUEUED at a time."""

    def __init__(self, db: Database, jobs: JobService, clock: Clock) -> None:
        self._db = db
        self._jobs = jobs
        self._clock = clock

    def active(self, intent_ids: Sequence[str]) -> BulkCreateResult | None:
        fingerprint = request_fingerprint(intent_ids)
        with self._db.read() as session:
            run = self._active_run(session, fingerprint)
            return None if run is None else self._view(session, run)

    def start(self, plans: Sequence[BulkCreatePlan], *, correlation_id: str) -> BulkCreateResult:
        ids = tuple(plan.intent_id for plan in plans)
        fingerprint = request_fingerprint(ids)
        notify = False
        with self._db.write() as session:
            existing = self._active_run(session, fingerprint)
            if existing is not None:
                return self._view(session, existing)
            reserved = session.scalar(
                select(RegistrationBulkItem.intent_id)
                .join(
                    RegistrationBulkRun,
                    RegistrationBulkRun.bulk_run_id == RegistrationBulkItem.bulk_run_id,
                )
                .where(
                    RegistrationBulkRun.state == RUNNING,
                    RegistrationBulkItem.intent_id.in_(ids),
                )
                .limit(1)
            )
            if reserved is not None:
                raise BulkRegistrationConflict(
                    "REGISTER_BULK_INTENT_RESERVED",
                    "an Intent already belongs to a running sequential bulk registration",
                    details={"intent_id": reserved},
                )
            targets = tuple(target_ref(intent_id) for intent_id in ids)
            active_job = session.scalar(
                select(Job)
                .where(
                    Job.job_type == CREATE_JOB_TYPE,
                    Job.target_ref.in_(targets),
                    Job.state.in_(ACTIVE_JOB_STATES),
                )
                .limit(1)
            )
            if active_job is not None:
                raise BulkRegistrationConflict(
                    "REGISTER_BULK_INTENT_ACTIVE",
                    "a requested Intent already has an active CREATE job",
                    details={"job_id": active_job.job_id, "target_ref": active_job.target_ref},
                )
            now = self._clock.now()
            run = RegistrationBulkRun(
                bulk_run_id=str(uuid.uuid4()),
                request_fingerprint=fingerprint,
                total=len(plans),
                state=RUNNING,
                error_code=None,
                correlation_id=correlation_id,
                created_at=now,
                updated_at=now,
            )
            session.add(run)
            session.flush()
            items: list[RegistrationBulkItem] = []
            for ordinal, plan in enumerate(plans, start=1):
                item = RegistrationBulkItem(
                    bulk_item_id=str(uuid.uuid4()),
                    bulk_run_id=run.bulk_run_id,
                    ordinal=ordinal,
                    intent_id=plan.intent_id,
                    send_request_json=json.dumps(
                        dict(plan.payload), ensure_ascii=False, sort_keys=True
                    ),
                    job_id=None,
                    state=WAITING,
                    error_class=None,
                    error_code=None,
                    error_message=None,
                    queued_at=None,
                    finished_at=None,
                )
                session.add(item)
                items.append(item)
            session.flush()
            self._queue(session, items[0], now=now)
            notify = True
            result = self._view(session, run)
        if notify:
            self._jobs.notify_worker()
        return result

    def status(self, bulk_run_id: str) -> BulkCreateResult:
        with self._db.read() as session:
            run = session.get(RegistrationBulkRun, bulk_run_id)
            if run is None:
                raise NotFoundError(
                    "REGISTER_BULK_RUN_NOT_FOUND",
                    f"bulk registration {bulk_run_id!r} does not exist",
                )
            return self._view(session, run)

    def settle_terminal(self, terminal: TerminalJob) -> None:
        """Finish the current item and queue exactly one successor, idempotently."""
        notify = False
        failure_logs: list[dict[str, Any]] = []
        with self._db.write() as session:
            item = session.scalar(
                select(RegistrationBulkItem).where(
                    RegistrationBulkItem.job_id == terminal.job_id,
                    RegistrationBulkItem.state == QUEUED,
                )
            )
            if item is None:
                return
            run = session.get(RegistrationBulkRun, item.bulk_run_id)
            if run is None or run.state != RUNNING:
                return
            now = self._clock.now()
            item.finished_at = now
            if terminal.state != JobState.SUCCEEDED.value:
                job = session.get(Job, terminal.job_id)
                self._fail_item(
                    item,
                    now=now,
                    error_class=(
                        terminal.error_class
                        or (None if job is None else job.last_error_class)
                        or ErrorClass.UNKNOWN.value
                    ),
                    error_code=(
                        terminal.error_code
                        or (None if job is None else job.last_error_code)
                        or "REGISTER_BULK_ITEM_DEAD"
                    ),
                    error_message=(None if job is None else job.last_error_message)
                    or "the single-product CREATE job ended without success",
                )
                failure_logs.append(self._failure_log(run, item))
            else:
                item.state = SUCCEEDED
            notify = self._advance(session, run, now=now, failure_logs=failure_logs)
            run.updated_at = now
        if notify:
            self._jobs.notify_worker()
        for details in failure_logs:
            logger.warning("register.bulk_item_failed", extra=details)

    def unsettled_jobs(self, terminal_states: Sequence[str]) -> Sequence[str]:
        """Terminal child jobs whose bulk item still says QUEUED, for recovery sweeps."""
        with self._db.read() as session:
            job_ids = session.scalars(
                select(RegistrationBulkItem.job_id)
                .join(
                    RegistrationBulkRun,
                    RegistrationBulkRun.bulk_run_id == RegistrationBulkItem.bulk_run_id,
                )
                .join(Job, Job.job_id == RegistrationBulkItem.job_id)
                .where(
                    RegistrationBulkRun.state == RUNNING,
                    RegistrationBulkItem.state == QUEUED,
                    Job.state.in_(tuple(terminal_states)),
                )
                .order_by(RegistrationBulkItem.queued_at)
            ).all()
            return tuple(job_id for job_id in job_ids if job_id is not None)

    def _queue(self, session: Session, item: RegistrationBulkItem, *, now: Any) -> None:
        payload = json.loads(item.send_request_json)
        record = self._jobs.enqueue(
            CREATE_JOB_TYPE,
            payload=payload,
            target_ref=target_ref(item.intent_id),
            session=session,
        )
        item.job_id = record.job_id
        item.state = QUEUED
        item.queued_at = now

    def _advance(
        self,
        session: Session,
        run: RegistrationBulkRun,
        *,
        now: Any,
        failure_logs: list[dict[str, Any]],
    ) -> bool:
        """Skip locally unsendable members, or queue the first sendable successor."""
        while True:
            next_item = session.scalar(
                select(RegistrationBulkItem)
                .where(
                    RegistrationBulkItem.bulk_run_id == run.bulk_run_id,
                    RegistrationBulkItem.state == WAITING,
                )
                .order_by(RegistrationBulkItem.ordinal)
                .limit(1)
            )
            if next_item is None:
                failed = session.scalar(
                    select(RegistrationBulkItem.bulk_item_id)
                    .where(
                        RegistrationBulkItem.bulk_run_id == run.bulk_run_id,
                        RegistrationBulkItem.state == FAILED,
                    )
                    .limit(1)
                )
                run.state = COMPLETED if failed is None else COMPLETED_WITH_FAILURES
                return False
            intent = session.get(RegistrationIntent, next_item.intent_id)
            if intent is not None and IntentState(intent.state) in (
                IntentState.PREPARED,
                IntentState.FAILED,
            ):
                self._queue(session, next_item, now=now)
                return True
            actual_state = "MISSING" if intent is None else intent.state
            next_item.queued_at = now
            self._fail_item(
                next_item,
                now=now,
                error_class=ErrorClass.CONFLICT.value,
                error_code="REGISTER_BULK_NEXT_NOT_SENDABLE",
                error_message=f"Intent is no longer sendable (state={actual_state})",
            )
            failure_logs.append(self._failure_log(run, next_item))

    @staticmethod
    def _fail_item(
        item: RegistrationBulkItem,
        *,
        now: Any,
        error_class: str,
        error_code: str,
        error_message: str,
    ) -> None:
        item.state = FAILED
        item.error_class = error_class
        item.error_code = error_code
        item.error_message = error_message
        item.finished_at = now

    @staticmethod
    def _failure_log(run: RegistrationBulkRun, item: RegistrationBulkItem) -> dict[str, Any]:
        return {
            "bulk_run_id": run.bulk_run_id,
            "position": item.ordinal,
            "total": run.total,
            "intent_id": item.intent_id,
            "job_id": item.job_id,
            "error_class": item.error_class,
            "error_code": item.error_code,
            "error_message": item.error_message,
        }

    @staticmethod
    def _active_run(session: Session, fingerprint: str) -> RegistrationBulkRun | None:
        return session.scalar(
            select(RegistrationBulkRun)
            .where(
                RegistrationBulkRun.request_fingerprint == fingerprint,
                RegistrationBulkRun.state == RUNNING,
            )
            .order_by(RegistrationBulkRun.created_at.desc())
            .limit(1)
        )

    @staticmethod
    def _view(session: Session, run: RegistrationBulkRun) -> BulkCreateResult:
        items = tuple(
            session.scalars(
                select(RegistrationBulkItem)
                .where(RegistrationBulkItem.bulk_run_id == run.bulk_run_id)
                .order_by(RegistrationBulkItem.ordinal)
            ).all()
        )
        processed = sum(item.state in (SUCCEEDED, FAILED) for item in items)
        current = next((item for item in items if item.state == QUEUED), None)
        if current is None and run.state == RUNNING:
            current = next((item for item in items if item.state == WAITING), None)
        failures = tuple(item for item in items if item.state == FAILED)
        failed = failures[0] if failures else None
        position = run.total if current is None else current.ordinal
        return BulkCreateResult(
            bulk_run_id=run.bulk_run_id,
            state=run.state,
            total=run.total,
            processed=processed,
            succeeded=sum(item.state == SUCCEEDED for item in items),
            failed=len(failures),
            position=position,
            progress=f"{position}/{run.total}",
            current_intent_id=None if current is None else current.intent_id,
            current_job_id=None if current is None else current.job_id,
            failed_intent_id=None if failed is None else failed.intent_id,
            error_code=None if failed is None else failed.error_code,
            failures=tuple(
                BulkCreateFailure(
                    position=failure.ordinal,
                    intent_id=failure.intent_id,
                    error_class=(
                        None if failure.error_class is None else ErrorClass(failure.error_class)
                    ),
                    error_code=failure.error_code or "REGISTER_BULK_ITEM_FAILED",
                    message=failure.error_message or "single-product registration failed",
                )
                for failure in failures
            ),
        )


__all__ = [
    "BulkCreatePlan",
    "BulkRegistrationService",
    "request_fingerprint",
]
