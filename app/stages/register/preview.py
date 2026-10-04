"""B-PREVIEW: what one frozen RegistrationSnapshot would send (owner decisions ``5975647306``).

Pure: it reads one frozen payload and the provider wire projection of it, through the injected
:class:`~app.stages.register.provider.WireProjector` (REGISTER never imports the adapter), and
returns a read shape. It decides nothing, sends nothing and stores nothing.

What it shows, and what it never shows:

- the **frozen outbound values** — a preview-only exception to the screen rule that no payload
  value is carried: the REGISTER summary (name, price, category, listing identity) and every field
  of the provider document the projection would send, flattened to ``path → value``;
- **never a provider URL**: an image is shown by its role, position, local identity and whether a
  provider asset was prepared for it; any document value that is URL-shaped is redacted, and the
  provider detail body (an HTML string holding provider references) is replaced by its structure —
  the section order, the detail image slots and the body paragraphs as plain text;
- the values frozen in the Snapshot that the projection does **not** send (tags, attributes),
  marked as not sent;
- a projection the adapter refuses is shown with its own refusal code, never a guess.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel

from app.stages.register.detail import DetailPlanError, plan_of
from app.stages.register.provider import WireProjector

PREVIEW_VERSION: Final = "register-snapshot-preview/v1"
PROJECTION_REFUSED: Final = "REGISTER_PREVIEW_PROJECTION_REFUSED"
# A frozen attribute the operator left to the product detail page.
DETAIL_PAGE_REFERENCE: Final = "DETAIL_PAGE_REFERENCE"

# Document leaves that hold a provider reference or the rendered detail body.
_DETAIL_FIELD: Final = "detailContent"
_URL_SHAPED = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://|^//")


class PreviewFieldView(BaseModel):
    """One leaf of the document the projection would send. ``value`` is the exact value as text,
    or ``None`` when it is redacted (a provider reference, or the rendered detail body)."""

    path: str
    value: str | None
    redacted: bool = False


class PreviewImageView(BaseModel):
    """One frozen image, by its local identity — never its provider reference."""

    item_id: str | None
    role: str
    position: int | None
    asset_kind: str
    sha256: str
    provider_asset_prepared: bool


class PreviewDetailView(BaseModel):
    """The detail body as structure: never the HTML the renderer sends."""

    builder: str  # PLAN (B-DETAIL) or BODY_ONLY
    sections: tuple[str, ...]
    renderer: str | None
    image_slots: tuple[str, ...]  # sha256 of each planned detail image, in order
    paragraphs: tuple[str, ...]


class PreviewNotSentView(BaseModel):
    """A value frozen in the Snapshot that the projection does not send."""

    path: str
    value: str


class SnapshotPreviewView(BaseModel):
    preview_version: str
    registration_snapshot_id: str
    payload_hash: str
    builder_version: str | None
    # The REGISTER summary, read from the frozen payload.
    name: str | None
    sale_prices_krw: tuple[int, ...]
    category_id: str | None
    listing_identity: str | None
    # The projection's own outcome.
    projected: bool
    refusal_code: str | None = None
    encoding_version: str | None = None
    sendable: bool | None = None
    gaps: tuple[str, ...] = ()
    document: tuple[PreviewFieldView, ...] = ()
    images: tuple[PreviewImageView, ...] = ()
    detail: PreviewDetailView | None = None
    not_sent: tuple[PreviewNotSentView, ...] = ()


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _flatten(node: Any, path: str) -> list[PreviewFieldView]:
    if isinstance(node, Mapping):
        found: list[PreviewFieldView] = []
        for key in sorted(node):
            found.extend(_flatten(node[key], f"{path}.{key}" if path else str(key)))
        return found
    if isinstance(node, list | tuple):
        found = []
        for index, child in enumerate(node):
            found.extend(_flatten(child, f"{path}[{index}]"))
        return found
    leaf = path.rsplit(".", 1)[-1]
    if leaf == _DETAIL_FIELD or (isinstance(node, str) and _url_like(node)):
        return [PreviewFieldView(path=path, value=None, redacted=True)]
    return [PreviewFieldView(path=path, value=None if node is None else _text(node))]


def _url_like(value: str) -> bool:
    return bool(_URL_SHAPED.search(value))


def _paragraphs(body: str) -> tuple[str, ...]:
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    return tuple(p.strip() for p in re.split(r"\n[ \t]*\n", text) if p.strip())


def _detail(payload: Mapping[str, Any]) -> PreviewDetailView | None:
    detail = payload.get("detail")
    if not isinstance(detail, Mapping):
        return None
    if "renderer" in detail:
        try:
            plan = plan_of(detail)
        except DetailPlanError:
            return None
        return PreviewDetailView(
            builder="PLAN",
            sections=plan.sections,
            renderer=plan.renderer,
            image_slots=tuple(image.sha256 for image in plan.images),
            paragraphs=_paragraphs(plan.body),
        )
    body = detail.get("body")
    sections = detail.get("sections")
    return PreviewDetailView(
        builder="BODY_ONLY",
        sections=tuple(str(s) for s in sections) if isinstance(sections, list) else (),
        renderer=None,
        image_slots=(),
        paragraphs=_paragraphs(body) if isinstance(body, str) else (),
    )


def _images(items: Sequence[Any]) -> tuple[PreviewImageView, ...]:
    found: list[PreviewImageView] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for asset in item.get("publication_assets") or ():
            if not isinstance(asset, Mapping):
                continue
            position = asset.get("position")
            found.append(
                PreviewImageView(
                    item_id=item.get("item_id") if isinstance(item.get("item_id"), str) else None,
                    role=str(asset.get("role")),
                    position=position if isinstance(position, int) else None,
                    asset_kind=str(asset.get("asset_kind")),
                    sha256=str(asset.get("sha256")),
                    provider_asset_prepared=asset.get("provider_asset_ref") is not None,
                )
            )
    return tuple(found)


def _not_sent(payload: Mapping[str, Any]) -> tuple[PreviewNotSentView, ...]:
    rows: list[PreviewNotSentView] = []
    tags = payload.get("tags")
    if isinstance(tags, list):
        rows.extend(
            PreviewNotSentView(path=f"tags[{i}]", value=_text(t)) for i, t in enumerate(tags)
        )
    attributes = payload.get("attributes")
    if isinstance(attributes, Mapping):
        for key in sorted(attributes):
            field = attributes[key]
            value = field.get("value") if isinstance(field, Mapping) else None
            reference = isinstance(field, Mapping) and field.get("detail_page_reference")
            rows.append(
                PreviewNotSentView(
                    path=f"attributes.{key}",
                    value=DETAIL_PAGE_REFERENCE if reference else _text(value),
                )
            )
    return tuple(rows)


def preview(
    registration_snapshot_id: str,
    payload_hash: str,
    payload: Mapping[str, Any],
    projector: WireProjector | None,
) -> SnapshotPreviewView:
    """The preview of one frozen Snapshot payload."""
    found_items = payload.get("items")
    items: list[Any] = found_items if isinstance(found_items, list) else []
    name = payload.get("name")
    category = payload.get("category")
    summary: dict[str, Any] = {
        "preview_version": PREVIEW_VERSION,
        "registration_snapshot_id": registration_snapshot_id,
        "payload_hash": payload_hash,
        "builder_version": payload.get("builder_version"),
        "name": name.get("value") if isinstance(name, Mapping) else None,
        "sale_prices_krw": tuple(
            item["sale_price_krw"]
            for item in items
            if isinstance(item, Mapping) and isinstance(item.get("sale_price_krw"), int)
        ),
        "category_id": category.get("category_id") if isinstance(category, Mapping) else None,
        "listing_identity": payload.get("listing_identity"),
        "images": _images(items),
        "detail": _detail(payload),
        "not_sent": _not_sent(payload),
    }
    if projector is None:
        return SnapshotPreviewView(projected=False, refusal_code=PROJECTION_REFUSED, **summary)
    try:
        projection = projector(payload)
    except ValueError as refused:
        return SnapshotPreviewView(
            projected=False,
            refusal_code=str(getattr(refused, "code", PROJECTION_REFUSED)),
            **summary,
        )
    return SnapshotPreviewView(
        projected=True,
        encoding_version=str(projection.encoding_version),
        sendable=bool(projection.sendable),
        gaps=tuple(str(gap) for gap in projection.gaps),
        document=tuple(_flatten(projection.document.mapping(), "")),
        **summary,
    )
