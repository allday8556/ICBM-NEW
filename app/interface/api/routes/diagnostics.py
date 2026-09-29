from fastapi import APIRouter

from app.capabilities.jobs.records import JobRecord
from app.interface.api.deps import ContainerDep

router = APIRouter(tags=["diagnostics"])


@router.post("/api/v1/diagnostics/failing-job", status_code=202)
def enqueue_failing_job(container: ContainerDep) -> JobRecord:
    """Enqueue the M0 always-failing job (only when ICBM_DIAGNOSTICS=1)."""
    return container.diagnostics.request_failing_job(actor=container.config.operator_actor)
