"""Registration Management API (Issue #89 PR-F §B, §27).

Reads return server-owned M5 state; actions are executed by the owner of the action and never by
the route. Each action re-enters the same owner that decided it was available, so an eligibility
the screen ignored still cannot be performed: the Intent must be sendable, an UNKNOWN is
reconciled rather than resent, an applied-but-unverified Intent is read back, and only a `POLICY`
or `FAILURE_BUDGET` brake accepts an operator resume.

The preparation routes are the operator's authoring path (§27): they record the inputs a
provider-listing unit is prepared from, evaluate them against current truth through the preflight
owner, and freeze a Snapshot only through the owners that already decide READY and freshness.

No route here can reach a marketplace mutation: CREATE stays NOT_ADOPTED, and the canary readiness
result is derived and read-only — it authorizes nothing.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep
from app.core.correlation import get_correlation_id, new_correlation_id
from app.register.canary import CanaryReadinessView
from app.register.contracts import (
    ActionResult,
    AuthoredInputsView,
    AuthoringMetadataView,
    PreparationView,
    RegisterOverview,
    UnitView,
)

router = APIRouter(prefix="/api/v1/register", tags=["register"])


class ResumeScopeRequest(BaseModel):
    """An explicit, audited operator release of one execution scope's send brake (§26)."""

    marketplace_key: str = Field(min_length=1, max_length=40)
    marketplace_account_id: str = Field(min_length=1, max_length=40)
    actor: str = Field(min_length=2, max_length=64)
    reason: str = Field(min_length=2, max_length=64)


class CreatePreparationRequest(BaseModel):
    """Open a preparation for one provider-listing unit of a Draft (§27)."""

    draft_id: str = Field(min_length=1, max_length=36)
    item_ids: list[str] = Field(min_length=1, max_length=50)
    actor: str = Field(min_length=2, max_length=64)
    inputs: AuthoredInputsView


class UpdatePreparationRequest(BaseModel):
    """Append the next authored revision. Nothing already recorded changes (§27)."""

    item_ids: list[str] = Field(min_length=1, max_length=50)
    actor: str = Field(min_length=2, max_length=64)
    inputs: AuthoredInputsView


class FreezeRequest(BaseModel):
    """Freeze the unit this preparation authored and open its CREATE Intent, if it is READY."""

    actor: str = Field(min_length=2, max_length=64)


def _correlation() -> str:
    return get_correlation_id() or new_correlation_id()


@router.get("/overview")
def overview(container: ContainerDep) -> RegisterOverview:
    return container.register.overview()


@router.get("/canary")
def canary(container: ContainerDep, unit_ref: str | None = None) -> CanaryReadinessView:
    """The readiness of **one** provider-listing unit. Without an exact one it fails closed."""
    return container.register.canary_readiness(unit_ref)


@router.get("/units/{draft_id}")
def units(container: ContainerDep, draft_id: str) -> tuple[UnitView, ...]:
    """Every provider-listing unit of one Draft: a Draft is never one row (ADR-0014 §2, R3)."""
    return container.register.units(draft_id)


@router.post("/preparations")
def create_preparation(
    container: ContainerDep, request: CreatePreparationRequest
) -> PreparationView:
    return container.register.create_preparation(
        request.draft_id,
        item_ids=request.item_ids,
        inputs=request.inputs,
        actor=request.actor,
        correlation_id=_correlation(),
    )


@router.get("/preparations/{preparation_id}")
def preparation(container: ContainerDep, preparation_id: str) -> PreparationView:
    return container.register.preparation(preparation_id)


@router.get("/drafts/{draft_id}/authoring-metadata/{category_id}")
def authoring_metadata(
    container: ContainerDep, draft_id: str, category_id: str
) -> AuthoringMetadataView:
    return container.register.authoring_metadata(draft_id, category_id)


@router.post("/preparations/{preparation_id}")
def update_preparation(
    container: ContainerDep, preparation_id: str, request: UpdatePreparationRequest
) -> PreparationView:
    return container.register.update_preparation(
        preparation_id,
        item_ids=request.item_ids,
        inputs=request.inputs,
        actor=request.actor,
        correlation_id=_correlation(),
    )


@router.post("/preparations/{preparation_id}/evaluate")
def evaluate_preparation(container: ContainerDep, preparation_id: str) -> ActionResult:
    """The candidate preflight of what is authored now, with every server reason code (§3)."""
    return container.register.evaluate_preparation(preparation_id)


@router.post("/preparations/{preparation_id}/freeze")
def freeze_preparation(
    container: ContainerDep, preparation_id: str, request: FreezeRequest
) -> ActionResult:
    return container.register.freeze_preparation(
        preparation_id, actor=request.actor, correlation_id=_correlation()
    )


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
