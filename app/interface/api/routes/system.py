import os
from typing import Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from app import MILESTONE, __version__
from app.capabilities.audit.service import AuditEventRecord
from app.capabilities.jobs.models import JobState
from app.capabilities.jobs.records import JobRecord
from app.interface.api.deps import ContainerDep
from app.platform.core.egress import EGRESS
from app.platform.core.execution import ExecutionMode
from app.platform.system.execution_mode import ExecutionModeState
from app.platform.system.readiness import CheckStatus, ReadinessReport

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    milestone: str
    # The serving interpreter's pid: diagnostics, and the id recorded in the ownership lock.
    pid: int


class ExecutionModeChangeRequest(BaseModel):
    target_mode: ExecutionMode
    reason: str | None = Field(default=None, max_length=500)
    # A LIVE request opens a bounded window only when it names its duration (ADR-0018 §2).
    window_s: int | None = None


@router.get("/api/health")
def health() -> HealthResponse:
    """Liveness: the process is up and serving HTTP."""
    return HealthResponse(status="ok", version=__version__, milestone=MILESTONE, pid=os.getpid())


@router.get("/api/ready")
def ready(container: ContainerDep, response: Response) -> ReadinessReport:
    report = container.readiness.check()
    if report.status is not CheckStatus.PASS:
        response.status_code = 503
    return report


@router.get("/api/v1/system/execution-mode")
def execution_mode(container: ContainerDep) -> ExecutionModeState:
    return container.execution_mode.state()


@router.post("/api/v1/system/execution-mode")
def request_execution_mode(
    body: ExecutionModeChangeRequest, container: ContainerDep
) -> ExecutionModeState:
    """Protected action, audited before the decision. DRY_RUN closes an open LIVE window; LIVE
    opens one only as a bounded window inside an approved bounded LIVE mutation scope (a live
    grant), and the mode alone is never authority for a mutation (ADR-0018 §2, §4.3)."""
    return container.execution_mode.request_change(
        body.target_mode,
        actor=container.config.operator_actor,
        reason=body.reason,
        window_s=body.window_s,
    )


@router.get("/api/v1/system/jobs")
def list_jobs(
    container: ContainerDep,
    state: JobState | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[JobRecord]:
    return container.jobs.list_jobs(state=state, limit=limit)


@router.get("/api/v1/system/jobs/{job_id}")
def get_job(job_id: str, container: ContainerDep) -> JobRecord:
    return container.jobs.get(job_id)


@router.get("/api/v1/system/audit-events")
def list_audit_events(
    container: ContainerDep,
    correlation_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditEventRecord]:
    return container.audit.list_events(correlation_id=correlation_id, limit=limit)


@router.get("/api/v1/system/egress")
def egress() -> dict[str, Any]:
    return EGRESS.snapshot()
