"""The Settings routes of 상세페이지 공지 (ADR-0033 §4; G2).

They list the current top and bottom notices with each period's server-judged status, the five
templates and the shipping presets; append one revision; preview the five templates for typed text
without storing anything; and serve a recorded guidance image by its SHA-256. A POST carries the
client header like every state-changing request (the global CSRF guard). Nothing here reaches a
marketplace, uploads an image or touches a product.
"""

from fastapi import APIRouter
from starlette.responses import Response

from app.capabilities.detail_guidance.service import (
    GuidanceSettingsView,
    PreviewImage,
    PreviewRequest,
    RevisionView,
    SaveRevisionRequest,
)
from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import get_correlation_id, new_correlation_id

router = APIRouter(prefix="/api/v1/settings/detail-guidance", tags=["settings"])


@router.get("")
def detail_guidance(container: ContainerDep) -> GuidanceSettingsView:
    return container.detail_guidance.settings()


@router.post("/revisions")
def save_revision(container: ContainerDep, request: SaveRevisionRequest) -> RevisionView:
    """Render, store and append one revision of a notice, or change nothing."""
    return container.detail_guidance.save(request, cid=get_correlation_id() or new_correlation_id())


@router.post("/preview")
def preview(container: ContainerDep, request: PreviewRequest) -> list[PreviewImage]:
    """The typed text in each of the five templates; nothing is stored."""
    return container.detail_guidance.preview(request)


@router.get("/images/{sha256}")
def image(container: ContainerDep, sha256: str) -> Response:
    """The bytes of a recorded guidance image, and of no other file."""
    return Response(
        content=container.detail_guidance.image(sha256),
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
