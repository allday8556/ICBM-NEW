"""The Image Studio routes: translating the text inside product images (ADR-0041 §6, §7; I5).

They read the capability (the registration editor's 「이미지 자동번역」 buttons are live only
while it is READY), request one translation run for the operator's chosen images of one product
(D4: only the operator's request starts one, at most 30 images), and serve the Studio panel of one
image of one Item: its review state, a stateless preview (a PNG; nothing is stored), the save of
an edit revision, 「적용」, the operator's QA verdict, the selection of the translated image and
「되돌리기」.
Every write is the owners' own: the edit revision is ``ImageStudioStore``'s, the derived artifact,
its QA and the selection are the M4 image owner's.

A POST carries the client header like every state-changing request (the global CSRF guard). A
refusal names ids and codes only and never echoes OCR text or a Korean text; a request that fails
validation names where and why, never the value. Nothing here reaches a marketplace or uploads an
image, and execution stays as it is (DRY_RUN).
"""

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from app.capabilities.image_studio.contracts import LANGUAGES, MAX_IMAGES_PER_RUN
from app.capabilities.image_studio.review import DraftItem
from app.capabilities.image_studio.store import EraseDecision
from app.capabilities.image_studio.task import MAX_KOREAN_CHARS
from app.capabilities.image_studio.translation import TranslateRequest
from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.stages.products.image_model import QaVerdict

router = APIRouter(prefix="/api/v1/image-studio", tags=["image-studio"])

LANGUAGE = r"^(zh|ja)$"


class TranslationRunRequest(BaseModel):
    images: list[str] = Field(min_length=1, max_length=MAX_IMAGES_PER_RUN)
    source_language: str = Field(pattern=LANGUAGE)
    # Per image, the regions the operator keeps (never sent) and the contact details it includes.
    excluded: dict[str, list[str]] = Field(default_factory=dict)
    included: dict[str, list[str]] = Field(default_factory=dict)
    actor: str = Field(min_length=1, max_length=64)


class DraftItemRequest(BaseModel):
    region_id: str = Field(min_length=1, max_length=64)
    include: bool
    text: str | None = Field(default=None, max_length=MAX_KOREAN_CHARS)
    erase: EraseDecision = EraseDecision.OVERLAY


class PreviewRequest(BaseModel):
    observation_id: str = Field(min_length=1, max_length=36)
    source_language: str = Field(pattern=LANGUAGE)
    items: list[DraftItemRequest] = Field(max_length=500)


class SaveRequest(PreviewRequest):
    expected_seq: int = Field(ge=0)
    actor: str = Field(min_length=1, max_length=64)


class ApplyRequest(BaseModel):
    revision_id: str = Field(min_length=1, max_length=36)
    source_language: str = Field(pattern=LANGUAGE)
    # The flagged regions the operator confirmed, one by one (IT-05).
    confirmed: list[str] = Field(default_factory=list, max_length=500)
    actor: str = Field(min_length=1, max_length=64)


class QaRequest(BaseModel):
    derivation_id: str = Field(min_length=1, max_length=36)
    verdict: QaVerdict
    actor: str = Field(min_length=1, max_length=64)


class SelectRequest(BaseModel):
    derivation_id: str = Field(min_length=1, max_length=36)
    actor: str = Field(min_length=1, max_length=64)


class RevertRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=64)


def _cid() -> str:
    return get_correlation_id() or new_correlation_id()


def _drafts(items: list[DraftItemRequest]) -> list[DraftItem]:
    return [DraftItem(i.region_id, i.include, i.text, i.erase) for i in items]


def _selection(record: Any) -> dict[str, Any]:
    return {
        "selection_revision_id": record.selection_revision_id,
        "revision_no": record.revision_no,
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
    }


@router.get("/capability")
def capability(container: ContainerDep) -> dict[str, Any]:
    """READY, or NOT_CONFIGURED with what is missing (``ocr``, ``translation task``,
    ``ai provider``): the editor's buttons stay inert, naming it, until it is READY (§7)."""
    service = container.image_translation
    report = service.capability()
    return {
        "key": report.key,
        "status": report.status.value,
        "missing": service.missing(),
        "languages": list(LANGUAGES),
        "max_images_per_run": MAX_IMAGES_PER_RUN,
    }


@router.post("/products/{product_group_id}/translations")
def request_translation(
    product_group_id: str, request: TranslationRunRequest, container: ContainerDep
) -> dict[str, Any]:
    """One run for the operator's chosen images of the product: the job id (D4, D5)."""
    job_id = container.image_translation.request(
        TranslateRequest(
            product_group_id=product_group_id,
            images=tuple(request.images),
            source_language=request.source_language,
            actor=request.actor,
            excluded={sha: frozenset(ids) for sha, ids in request.excluded.items()},
            included={sha: frozenset(ids) for sha, ids in request.included.items()},
        )
    )
    return {"job_id": job_id}


@router.get("/items/{item_id}/images")
def item_images(item_id: str, container: ContainerDep) -> dict[str, Any]:
    """Every CONFIRMED source image of the Item (all roles, D3) and what is done with it now."""
    return asdict(container.image_studio_review.images(item_id))


@router.get("/items/{item_id}/images/{sha256}")
def translation_state(
    item_id: str,
    sha256: str,
    container: ContainerDep,
    source_language: str = Query(default="zh", pattern=LANGUAGE),
) -> dict[str, Any]:
    """The panel's read of one image: regions, flags and defaults, the candidate on offer, the
    current edit and the selection."""
    return asdict(container.image_studio_review.translation_state(item_id, sha256, source_language))


@router.post("/items/{item_id}/images/{sha256}/preview")
def preview(
    item_id: str, sha256: str, request: PreviewRequest, container: ContainerDep
) -> Response:
    """The draft rendered as a PNG; nothing is stored. The header ``X-Image-Studio-Review``
    carries the completeness verdict and each region's flags, as ids and codes only."""
    result = container.image_studio_review.preview(
        item_id,
        sha256,
        observation_id=request.observation_id,
        source_language=request.source_language,
        items=_drafts(request.items),
    )
    review = json.dumps(
        {
            "complete": result.complete,
            "failures": [list(f) for f in result.failures],
            "flags": {rid: list(flags) for rid, flags in result.flags.items()},
        },
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return Response(
        content=result.png,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Image-Studio-Review": review},
    )


@router.post("/items/{item_id}/images/{sha256}/revisions")
def save_revision(
    item_id: str, sha256: str, request: SaveRequest, container: ContainerDep
) -> dict[str, Any]:
    """Append the operator's edit of the image against the sequence it was read at."""
    revision = container.image_studio_review.save(
        item_id,
        sha256,
        observation_id=request.observation_id,
        source_language=request.source_language,
        items=_drafts(request.items),
        expected_seq=request.expected_seq,
        actor=request.actor,
        correlation_id=_cid(),
    )
    return {
        "revision_id": revision.revision_id,
        "seq": revision.seq,
        "candidate_id": revision.candidate_id,
        "content_fingerprint": revision.content_fingerprint,
        "items": [item.canonical() for item in revision.items],
    }


@router.post("/items/{item_id}/images/{sha256}/apply")
def apply(
    item_id: str, sha256: str, request: ApplyRequest, container: ContainerDep
) -> dict[str, Any]:
    """「적용」: render the current revision and record the translated image (M4 owner)."""
    return asdict(
        container.image_studio_review.apply(
            item_id,
            sha256,
            revision_id=request.revision_id,
            source_language=request.source_language,
            confirmed=frozenset(request.confirmed),
            actor=request.actor,
            correlation_id=_cid(),
        )
    )


@router.get("/items/{item_id}/images/{sha256}/derivations/{derivation_id}/image")
def derived_image(
    item_id: str, sha256: str, derivation_id: str, container: ContainerDep
) -> Response:
    """The exact bytes of one translated image of this image, for the operator's QA."""
    return Response(
        content=container.image_studio_review.derived_image(item_id, sha256, derivation_id),
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/items/{item_id}/images/{sha256}/qa")
def record_qa(
    item_id: str, sha256: str, request: QaRequest, container: ContainerDep
) -> dict[str, Any]:
    """The operator's verdict on exactly the translated image (M4 QA owner)."""
    record = container.image_studio_review.qa(
        item_id,
        sha256,
        derivation_id=request.derivation_id,
        verdict=request.verdict,
        actor=request.actor,
        correlation_id=_cid(),
    )
    return {
        "qa_result_id": record.qa_result_id,
        "sha256": record.sha256,
        "derivation_id": record.derivation_id,
        "qa_rule_version": record.qa_rule_version,
        "verdict": record.verdict.value,
    }


@router.post("/items/{item_id}/images/{sha256}/select")
def select(
    item_id: str, sha256: str, request: SelectRequest, container: ContainerDep
) -> dict[str, Any]:
    """Use the translated image (``USE_DERIVED``, operator origin) after the operator's PASS."""
    return _selection(
        container.image_studio_review.select(
            item_id,
            sha256,
            derivation_id=request.derivation_id,
            actor=request.actor,
            correlation_id=_cid(),
        )
    )


@router.post("/items/{item_id}/images/{sha256}/revert")
def revert(
    item_id: str, sha256: str, request: RevertRequest, container: ContainerDep
) -> dict[str, Any]:
    """「되돌리기」: use the source image again (``USE_SOURCE``). Nothing is deleted."""
    return _selection(
        container.image_studio_review.revert(
            item_id, sha256, actor=request.actor, correlation_id=_cid()
        )
    )
