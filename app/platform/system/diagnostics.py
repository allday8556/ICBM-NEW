from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.capabilities.jobs.diagnostic import FAILING_JOB_TYPE
from app.capabilities.jobs.records import JobRecord
from app.capabilities.jobs.service import JobService
from app.platform.core.errors import NotFoundError
from app.platform.db.database import Database


class DiagnosticsService:
    """Operator-triggered M0 diagnostics. Disabled unless ``ICBM_DIAGNOSTICS=1``."""

    def __init__(self, *, enabled: bool, db: Database, jobs: JobService, audit: AuditLog) -> None:
        self._enabled = enabled
        self._db = db
        self._jobs = jobs
        self._audit = audit

    @property
    def enabled(self) -> bool:
        return self._enabled

    def request_failing_job(self, *, actor: str) -> JobRecord:
        if not self._enabled:
            raise NotFoundError(
                "DIAGNOSTICS_DISABLED", "diagnostics are disabled; set ICBM_DIAGNOSTICS=1"
            )
        with self._db.write() as session:
            job = self._jobs.enqueue(
                FAILING_JOB_TYPE,
                target_ref="m0:diagnostic",
                payload={"purpose": "retry/backoff/dead-letter demonstration"},
                session=session,
            )
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.DIAGNOSTIC_REQUEST,
                    action="ENQUEUE_FAILING_JOB",
                    actor=actor,
                    outcome=AuditOutcome.ALLOWED,
                    target_ref=f"job:{job.job_id}",
                    details={"job_type": FAILING_JOB_TYPE, "max_attempts": job.max_attempts},
                ),
                session=session,
            )
        self._jobs.notify_worker()
        return job
