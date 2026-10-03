"""The operator's image decisions (M4 PR-E, ADR-0013 §9; owner decision 2026-10-03).

The image owner (``ProductImageService``) records an explicit operator selection and an
exact-binary QA verdict; these routes are the operator's way to reach it. A route never decides:
it shows the CONFIRMED source images of an Item's current bound revision — and serves the bytes of
exactly those, and of no other asset — and hands the operator's complete decision to the owner,
which validates every rule as before: one decision per image, nothing selected by default, one
verdict per exact binary and input. No route selects an image on an operator's behalf.
"""

from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.interface.api.deps import ContainerDep
from app.platform.core.errors import NotFoundError
from app.stages.collect.facts import ImageRole
from app.stages.products.image_model import (
    ImageAssetKind,
    QaVerdict,
    SelectedOutput,
    SourceDecision,
    SourceDecisionKind,
)

router = APIRouter(tags=["products"])


class SourceDecisionRequest(BaseModel):
    role: ImageRole
    ordinal: int
    sha256: str = Field(min_length=64, max_length=64)
    decision: SourceDecisionKind
    derivation_id: str | None = None


class SelectedOutputRequest(BaseModel):
    role: ImageRole
    source_role: ImageRole
    source_ordinal: int


class ImageSelectionRequest(BaseModel):
    source_revision_id: str
    decisions: list[SourceDecisionRequest]
    outputs: list[SelectedOutputRequest]
    actor: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=500)


class ImageQaRequest(BaseModel):
    asset_kind: ImageAssetKind
    sha256: str = Field(min_length=64, max_length=64)
    derivation_id: str | None = None
    validated_source_revision_id: str
    verdict: QaVerdict
    findings: list[str] = Field(default_factory=list)
    actor: str = Field(min_length=1, max_length=64)


def _selection(record: Any) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "selection_revision_id": record.selection_revision_id,
        "source_revision_id": record.source_revision_id,
        "revision_no": record.revision_no,
        "decided_by": record.decided_by,
        "decisions": [
            {
                "role": d.role.value,
                "ordinal": d.ordinal,
                "sha256": d.sha256,
                "decision": d.decision.value,
                "derivation_id": d.derivation_id,
            }
            for d in record.decisions
        ],
        "outputs": [
            {
                "position": o.position,
                "role": o.role.value,
                "source_role": o.source_role.value,
                "source_ordinal": o.source_ordinal,
                "asset_kind": o.asset_kind.value,
                "sha256": o.sha256,
                "derivation_id": o.derivation_id,
            }
            for o in record.outputs
        ],
    }


@router.get("/api/v1/products/items/{item_id}/image-candidates")
def image_candidates(item_id: str, container: ContainerDep) -> dict[str, Any]:
    found = container.images.candidates(item_id)
    return {
        "item_id": found.item_id,
        "source_revision_id": found.source_revision_id,
        "qa_rule_version": found.qa_rule_version,
        "images": [
            {
                "role": image.role,
                "ordinal": image.ordinal,
                "sha256": image.sha256,
                "qa_verdict": None if image.qa_verdict is None else image.qa_verdict.value,
            }
            for image in found.images
        ],
        "current_selection": _selection(found.current_selection),
    }


@router.get("/api/v1/products/items/{item_id}/images/{sha256}")
def item_image(item_id: str, sha256: str, container: ContainerDep) -> Response:
    """The bytes of one CONFIRMED source image of the Item's current bound revision, and of no
    other asset."""
    if not container.images.is_candidate(item_id, sha256):
        raise NotFoundError("PRODUCTS_IMAGE_NOT_A_CANDIDATE", "not a source image of this Item")
    stored = container.source_assets.get(sha256)
    if stored is None:
        raise NotFoundError("COLLECT_ASSET_UNKNOWN", "no source asset has that checksum")
    return Response(
        content=container.source_assets.read(sha256),
        media_type=stored.mime_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/api/v1/products/items/{item_id}/image-selection")
def record_image_selection(
    item_id: str, request: ImageSelectionRequest, container: ContainerDep
) -> dict[str, Any]:
    selection, _move = container.images.record_operator_selection(
        item_id,
        source_revision_id=request.source_revision_id,
        decisions=[
            SourceDecision(
                role=d.role,
                ordinal=d.ordinal,
                sha256=d.sha256,
                decision=d.decision,
                derivation_id=d.derivation_id,
            )
            for d in request.decisions
        ],
        outputs=[
            SelectedOutput(role=o.role, source_role=o.source_role, source_ordinal=o.source_ordinal)
            for o in request.outputs
        ],
        decided_by=request.actor,
        reason=request.reason,
    )
    return _selection(selection) or {}


@router.post("/api/v1/products/image-qa")
def record_image_qa(request: ImageQaRequest, container: ContainerDep) -> dict[str, Any]:
    record = container.images.record_qa(
        asset_kind=request.asset_kind,
        sha256=request.sha256,
        derivation_id=request.derivation_id,
        validated_source_revision_id=request.validated_source_revision_id,
        verdict=request.verdict,
        findings=request.findings,
        decided_by=request.actor,
    )
    return {
        "qa_result_id": record.qa_result_id,
        "asset_kind": record.asset_kind.value,
        "sha256": record.sha256,
        "derivation_id": record.derivation_id,
        "validated_source_revision_id": record.validated_source_revision_id,
        "qa_rule_version": record.qa_rule_version,
        "verdict": record.verdict.value,
        "findings": list(record.findings),
    }
