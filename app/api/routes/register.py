"""Registration Management API (Issue #89 PR-F §B).

Reads return server-owned M5 state; actions are executed by the owner of the action and never by
the route. Each action re-enters the same owner that decided it was available, so an eligibility
the screen ignored still cannot be performed: the Intent must be sendable, an UNKNOWN is
reconciled rather than resent, an applied-but-unverified Intent is read back, and only a `POLICY`
or `FAILURE_BUDGET` brake accepts an operator resume.

No route here can reach a marketplace mutation: CREATE stays NOT_ADOPTED, and the canary readiness
result is derived and read-only — it authorizes nothing.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.core.correlation import get_correlation_id, new_correlation_id
from app.register.canary import CanaryReadinessView
from app.register.contracts import ActionResult, RegisterOverview, UnitView

router = APIRouter(prefix="/api/v1/register", tags=["register"])


class ResumeScopeRequest(BaseModel):
    """An explicit, audited operator release of one execution scope's send brake (§26)."""

    marketplace_key: str = Field(min_length=1, max_length=40)
    marketplace_account_id: str = Field(min_length=1, max_length=40)
    actor: str = Field(min_length=2, max_length=64)
    reason: str = Field(min_length=2, max_length=64)


def _correlation() -> str:
    return get_correlation_id() or new_correlation_id()


@router.get("/overview")
def overview(container: ContainerDep) -> RegisterOverview:
    return container.register.overview()


@router.get("/canary")
def canary(container: ContainerDep, draft_id: str | None = None) -> CanaryReadinessView:
    return container.register.canary_readiness(draft_id)


@router.get("/units/{draft_id}")
def units(container: ContainerDep, draft_id: str) -> tuple[UnitView, ...]:
    """Every provider-listing unit of one Draft: a Draft is never one row (ADR-0014 §2, R3)."""
    return container.register.units(draft_id)


@router.post("/intents/{intent_id}/create")
def enqueue_create(container: ContainerDep, intent_id: str) -> ActionResult:
    return container.register.enqueue_create(intent_id)


@router.post("/intents/{intent_id}/reconcile")
def reconcile(container: ContainerDep, intent_id: str) -> ActionResult:
    return container.register.reconcile(intent_id, correlation_id=_correlation())


@router.post("/intents/{intent_id}/verify")
def verify(container: ContainerDep, intent_id: str) -> ActionResult:
    return container.register.verify(intent_id, correlation_id=_correlation())


@router.post("/scopes/resume")
def resume_scope(container: ContainerDep, request: ResumeScopeRequest) -> ActionResult:
    return container.register.resume_scope(
        request.marketplace_key,
        request.marketplace_account_id,
        actor=request.actor,
        reason=request.reason,
        correlation_id=_correlation(),
    )
