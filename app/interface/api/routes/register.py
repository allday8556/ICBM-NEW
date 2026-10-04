"""Registration Management API (Issue #89 PR-F §B, §27).

Reads return server-owned M5 state; actions are executed by the owner of the action and never by
the route. Each action re-enters the same owner that decided it was available, so an eligibility
the screen ignored still cannot be performed: the Intent must be sendable, an UNKNOWN is
reconciled rather than resent, an applied-but-unverified Intent is read back, and only a `POLICY`
or `FAILURE_BUDGET` brake accepts an operator resume.

The preparation routes are the operator's authoring path (§27): they record the inputs a
provider-listing unit is prepared from, evaluate them against current truth through the preflight
owner, and freeze a Snapshot only through the owners that already decide READY and freshness.

No route here makes a marketplace mutation by itself. A CREATE and a deletion (ADR-0018 §3.5) run
only through their owners and the send-time safety stack, which refuses unless every layer allows
— a bounded LIVE window, the released brake and the exact grant among them; outside one the
execution mode refuses every mutation even with a committed session. The canary readiness result
is derived and read-only — it authorizes nothing.

The deletion routes delete one ICBM-confirmed registration under its own DELETE grant, read a
possibly applied deletion back, and list a registration's deletion attempts. An unknown deletion is
never resent.
"""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.stages.register.canary import CanaryReadinessView
from app.stages.register.contracts import (
    ActionResult,
    AuthoredInputsView,
    AuthoringMetadataView,
    PreparationView,
    RegisterOverview,
    RegisterReadinessView,
    RegistrationStatusView,
    UnitView,
)
from app.stages.register.drafting import CreateDraftRequest, DraftCreatedView, DraftTargetsView
from app.stages.register.preview import SnapshotPreviewView

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


@router.get("/readiness")
def readiness(container: ContainerDep) -> RegisterReadinessView:
    """B-UX1: the readiness of every pre-send provider-listing unit, evaluated now by the
    preflight owner — five statuses plus NOT_EVALUATED with the owner's refusal code, and the
    areas the reasons belong to. Read-only and derived: nothing is stored."""
    return container.register.readiness_summary()


@router.get("/status")
def registration_status(container: ContainerDep) -> RegistrationStatusView:
    """The registration status card and its detail panel (ADR-0014 §28.5): every recent
    Intent's read state from the one server-side partition, and the batches' derived counts."""
    return container.register.registration_status()


@router.get("/live")
def live_status(container: ContainerDep) -> dict[str, Any]:
    """The protected-write brake, the grants and their ASSET readiness, and the unit-independent
    proofs (ADR-0018 §9, §10). Read-only: it records, grants and authorizes nothing."""
    return container.live_status.status()


@router.get("/draft-targets")
def draft_targets(container: ContainerDep) -> DraftTargetsView:
    """Every canonical account a Draft may target, with its binding and current policy."""
    return container.drafting.targets()


@router.post("/drafts")
def create_draft(container: ContainerDep, request: CreateDraftRequest) -> DraftCreatedView:
    """A Draft from the operator's Product DB selection (Gate 1 G1-D). The selection is
    revalidated, every Item is priced by M4 and pinned, or nothing is created. It creates no
    preparation, Snapshot, Intent or job."""
    return container.drafting.create(request, correlation_id=_correlation())


@router.get("/snapshots/{registration_snapshot_id}/preview")
def snapshot_preview(container: ContainerDep, registration_snapshot_id: str) -> SnapshotPreviewView:
    """B-PREVIEW: what one frozen Snapshot would send to the marketplace — its frozen outbound
    values and the provider document fields, with every provider URL redacted and the detail body
    as structure. Read-only."""
    return container.register.snapshot_preview(registration_snapshot_id)


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


def _deletion(record: Any) -> dict[str, Any]:
    return {
        "deletion_id": record.deletion_id,
        "registration_id": record.registration_id,
        "marketplace_product_id": record.marketplace_product_id,
        "attempt_no": record.attempt_no,
        "state": record.state.value,
        "verification": None if record.verification is None else record.verification.value,
        "response_status": record.response_status,
        "error_code": record.error_code,
        "deleted": record.deleted,
        "open": record.open,
    }


@router.post("/registrations/{registration_id}/delete")
def delete_registration(container: ContainerDep, registration_id: str) -> dict[str, Any]:
    record = container.registration_deletions.delete(
        registration_id,
        actor=container.config.operator_actor,
        correlation_id=_correlation(),
    )
    return _deletion(record)


@router.post("/registrations/{registration_id}/delete/verify")
def verify_deletion(container: ContainerDep, registration_id: str) -> dict[str, Any]:
    record = container.registration_deletions.verify(registration_id, correlation_id=_correlation())
    return _deletion(record)


@router.get("/registrations/{registration_id}/deletions")
def list_deletions(container: ContainerDep, registration_id: str) -> list[dict[str, Any]]:
    return [_deletion(r) for r in container.registration_deletions.deletions(registration_id)]


@router.post("/scopes/resume")
def resume_scope(container: ContainerDep, request: ResumeScopeRequest) -> ActionResult:
    return container.register.resume_scope(
        request.marketplace_key,
        request.marketplace_account_id,
        actor=request.actor,
        reason=request.reason,
        correlation_id=_correlation(),
    )
