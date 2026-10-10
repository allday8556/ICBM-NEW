"""What Settings reads and saves for 상세페이지 공지 (ADR-0033 §4), and nothing else.

The settings API lists the current notices with each period's server-judged status, appends a
revision, previews the five templates for typed text without storing anything, and serves a
recorded image by its SHA-256. The presets (§11) are offered as editable example text only: they
reach nothing until the operator saves them as their own text (DG-08).
"""

import base64
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from app.capabilities.detail_guidance.presets import PRESETS, PRESETS_VERSION
from app.capabilities.detail_guidance.renderer import RENDERER_VERSION, Template
from app.capabilities.detail_guidance.store import (
    CurrentNotice,
    DetailGuidanceStore,
    GuidanceStatus,
    Kind,
    Placement,
    parse_content,
    render_all,
    status_at,
)

IMAGE_PATH: Final = "/api/v1/settings/detail-guidance/images/{sha256}"
TEMPLATE_LABELS: Final[dict[Template, str]] = {
    Template.CLEAN: "클린",
    Template.MODERN: "모던",
    Template.WARM: "웜",
    Template.DOMESTIC: "국내배송",
    Template.OVERSEAS: "해외배송",
}
STATUS_LABELS: Final[dict[GuidanceStatus, str]] = {
    GuidanceStatus.SCHEDULED: "예정",
    GuidanceStatus.ACTIVE: "진행 중",
    GuidanceStatus.ENDED: "종료",
}


class RevisionView(BaseModel):
    guidance_id: str
    revision_id: str
    placement: Placement
    kind: Kind
    seq: int
    template: Template
    content: dict[str, Any]
    content_fingerprint: str
    enabled: bool
    starts_at: datetime | None
    ends_at: datetime | None
    # A period notice's status at the server's instant; ``None`` for a standing notice.
    status: GuidanceStatus | None
    status_label: str | None
    image_sha256: str
    image_url: str
    image_width: int
    image_height: int
    authored_by: str
    authored_at: datetime


class PlacementView(BaseModel):
    placement: Placement
    standing: RevisionView | None
    periods: list[RevisionView]


class TemplateView(BaseModel):
    name: Template
    label: str


class PresetView(BaseModel):
    key: str
    label: str
    template: Template
    top: dict[str, Any]
    bottom: dict[str, Any]
    version: str


class GuidanceSettingsView(BaseModel):
    now: datetime
    renderer_version: str
    placements: list[PlacementView]
    templates: list[TemplateView]
    presets: list[PresetView]


class SaveRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: StrictStr = Field(min_length=1, max_length=64)
    placement: Placement
    kind: Kind
    # ``None`` creates a period notice, or names the placement's standing notice.
    guidance_id: StrictStr | None = Field(default=None, min_length=1, max_length=36)
    # The sequence the notice was read at; ``None`` for a notice with no revision yet.
    expected_current_seq: StrictInt | None = Field(default=None, ge=1)
    template: Template
    content: dict[str, Any]
    enabled: StrictBool = True
    # Instants with a UTC offset; the owner refuses a naive or a missing one for a period.
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: dict[str, Any]


class PreviewImage(BaseModel):
    template: Template
    label: str
    sha256: str
    width: int
    height: int
    png_base64: str


class DetailGuidanceService:
    def __init__(self, store: DetailGuidanceStore) -> None:
        self._store = store

    def settings(self) -> GuidanceSettingsView:
        current = self._store.current()
        return GuidanceSettingsView(
            now=current.now,
            renderer_version=RENDERER_VERSION,
            placements=[
                PlacementView(
                    placement=p.placement,
                    standing=None if p.standing is None else _view(p.standing),
                    periods=[_view(notice) for notice in p.periods],
                )
                for p in current.placements
            ],
            templates=[TemplateView(name=t, label=TEMPLATE_LABELS[t]) for t in Template],
            presets=[
                PresetView(
                    key=preset.key,
                    label=preset.label,
                    template=preset.template,
                    top=preset.top,
                    bottom=preset.bottom,
                    version=PRESETS_VERSION,
                )
                for preset in PRESETS
            ],
        )

    def save(self, request: SaveRevisionRequest, *, cid: str) -> RevisionView:
        record = self._store.save(
            placement=request.placement,
            kind=request.kind,
            guidance_id=request.guidance_id,
            template=request.template,
            content=request.content,
            enabled=request.enabled,
            starts_at=request.starts_at,
            ends_at=request.ends_at,
            expected_current_seq=request.expected_current_seq,
            actor=request.actor,
            correlation_id=cid,
        )
        # The appended revision is its notice's current one.
        return _view(CurrentNotice(record, status_at(record, self._store.now())))

    def preview(self, request: PreviewRequest) -> list[PreviewImage]:
        """The five templates for the typed text; nothing is stored."""
        return [
            PreviewImage(
                template=rendered.template,
                label=TEMPLATE_LABELS[rendered.template],
                sha256=rendered.sha256,
                width=rendered.width,
                height=rendered.height,
                png_base64=base64.b64encode(rendered.png).decode("ascii"),
            )
            for rendered in render_all(parse_content(request.content))
        ]

    def image(self, sha256: str) -> bytes:
        return self._store.image(sha256)


def _view(notice: CurrentNotice) -> RevisionView:
    record = notice.record
    return RevisionView(
        guidance_id=record.guidance_id,
        revision_id=record.revision_id,
        placement=record.placement,
        kind=record.kind,
        seq=record.seq,
        template=record.template,
        content=dict(record.content),
        content_fingerprint=record.content_fingerprint,
        enabled=record.enabled,
        starts_at=record.starts_at,
        ends_at=record.ends_at,
        status=notice.status,
        status_label=None if notice.status is None else STATUS_LABELS[notice.status],
        image_sha256=record.image_sha256,
        image_url=IMAGE_PATH.format(sha256=record.image_sha256),
        image_width=record.image_width,
        image_height=record.image_height,
        authored_by=record.authored_by,
        authored_at=record.authored_at,
    )
