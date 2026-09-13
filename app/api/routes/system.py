from typing import Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from app import MILESTONE, __version__
from app.api.deps import ContainerDep
from app.audit.service import AuditEventRecord
from app.core.egress import EGRESS
from app.core.execution import ExecutionMode
from app.jobs.models import JobState
from app.jobs.records import JobRecord
from app.system.execution_mode import ExecutionModeState
from app.system.readiness import CheckStatus, ReadinessReport

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    milestone: str


class ExecutionModeChangeRequest(BaseModel):
    target_mode: ExecutionMode
    reason: str | None = Field(default=None, max_length=500)


@router.get("/api/health")
def health() -> HealthResponse:
    """Liveness: the process is up and serving HTTP."""
    return HealthResponse(status="ok", version=__version__, milestone=MILESTONE)


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
    """Protected action: audited, and always denied during M0."""
    return container.execution_mode.request_change(
        body.target_mode, actor=container.config.operator_actor, reason=body.reason
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
