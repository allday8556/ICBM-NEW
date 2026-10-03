"""B-DETAIL: the detail composition plan and the trusted REGISTER renderer (ADR-0014 §19, §27.1
amendment notes; design ``documents/reviews/B-DETAIL-detail-composition.md``).

Pure: no database, no clock, no provider and no I/O.

**Two layers, never mixed in one profile row.** The marketplace-scoped ``DETAIL_COMPOSITION``
profile (:class:`DetailProfile`) holds the section vocabulary and order, the body format and the
renderer version. The product-specific :class:`DetailPlan` holds the selected ``DETAIL`` image
asset identities, the optional ``BODY`` plain text and the exact composition revision it was
authored against. The plan is frozen in the Snapshot and holds **no URL**: an image is named by its
local asset identity only.

**The trusted renderer.** :func:`render` is the only producer of a provider detail body under a
plan. It reads the frozen plan and the uploaded provider asset identities of that same Snapshot —
each an :class:`UploadedProviderAsset`, a type a plain string is not — under the plan's pinned
renderer version, and is deterministic: the same plan, references and renderer give the same text.
``BODY`` is plain text and is HTML-escaped, never parsed as markup. A planned image without an
uploaded provider identity refuses the rendering; a source or supplier URL can never reach it,
because the plan holds none. The outbound sanitizer is unchanged.

No count or length limit is applied (owner decision D4, G-1 ``INSUFFICIENT``), and nothing here
ever edits or truncates a plan.
"""

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from app.stages.register.sanitize import safe_provider_reference

DETAIL_RENDERER_VERSION: Final = "detail-renderer/v1"
SECTION_DETAIL_IMAGES: Final = "DETAIL_IMAGES"
SECTION_BODY: Final = "BODY"
BODY_FORMAT_PLAIN_TEXT: Final = "PLAIN_TEXT"

# The renderer each pinned version names; another version is never rendered by this one.
RENDERERS: Final = frozenset({DETAIL_RENDERER_VERSION})

_PLAN_KEYS: Final = frozenset(
    {"composition_revision", "sections", "body_format", "renderer", "body", "images"}
)
_IMAGE_KEYS: Final = frozenset({"item_id", "position", "asset_kind", "sha256", "derivation_id"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


class DetailPlanError(ValueError):
    """The plan is not a canonical detail plan. Nothing is rendered."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DetailProfile:
    """One ``DETAIL_COMPOSITION`` revision, as its owner holds it. Content v1 is ``BODY``-only and
    names no renderer; content v2 orders ``DETAIL_IMAGES`` → ``BODY`` under a pinned renderer."""

    revision_id: str
    content_version: str
    sections: tuple[str, ...]
    body_format: str | None = None
    renderer: str | None = None

    @property
    def renders(self) -> bool:
        """Whether a plan is composed under this profile (content v2), rather than the v1
        ``BODY``-only body."""
        return self.renderer is not None

    @property
    def places_images(self) -> bool:
        return self.renders and SECTION_DETAIL_IMAGES in self.sections


@dataclass(frozen=True)
class PlannedImage:
    """One selected ``DETAIL`` image, by its local asset identity. Never a URL."""

    item_id: str
    position: int
    asset_kind: str
    sha256: str
    derivation_id: str | None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.asset_kind, self.sha256, self.derivation_id or "")

    def canonical(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "position": self.position,
            "asset_kind": self.asset_kind,
            "sha256": self.sha256,
            "derivation_id": self.derivation_id,
        }


@dataclass(frozen=True)
class DetailPlan:
    """The URL-free, product-specific plan a Snapshot freezes."""

    composition_revision: str
    sections: tuple[str, ...]
    body_format: str
    renderer: str
    body: str
    images: tuple[PlannedImage, ...]

    @property
    def places_images(self) -> bool:
        return SECTION_DETAIL_IMAGES in self.sections

    def canonical(self) -> dict[str, Any]:
        return {
            "composition_revision": self.composition_revision,
            "sections": list(self.sections),
            "body_format": self.body_format,
            "renderer": self.renderer,
            "body": self.body,
            "images": [image.canonical() for image in self.images],
        }


def plan_of(document: object) -> DetailPlan:
    """The plan one frozen payload holds, read strictly: an unknown or missing member — a URL
    included — refuses the whole plan."""
    if not isinstance(document, Mapping) or set(document) != _PLAN_KEYS:
        raise DetailPlanError("DETAIL_PLAN_MALFORMED", "not exactly a detail plan")
    sections = document["sections"]
    images = document["images"]
    if (
        not isinstance(document["composition_revision"], str)
        or not isinstance(sections, list)
        or not all(isinstance(s, str) for s in sections)
        or not isinstance(document["body_format"], str)
        or not isinstance(document["renderer"], str)
        or not isinstance(document["body"], str)
        or not isinstance(images, list)
    ):
        raise DetailPlanError("DETAIL_PLAN_MALFORMED", "a plan member has another type")
    planned: list[PlannedImage] = []
    for image in images:
        if not isinstance(image, Mapping) or set(image) != _IMAGE_KEYS:
            raise DetailPlanError("DETAIL_PLAN_MALFORMED", "not exactly a planned image")
        position, derivation = image["position"], image["derivation_id"]
        if (
            not isinstance(image["item_id"], str)
            or not isinstance(position, int)
            or isinstance(position, bool)
            or not isinstance(image["asset_kind"], str)
            or not isinstance(image["sha256"], str)
            or not _SHA256.fullmatch(image["sha256"])
            or not (derivation is None or isinstance(derivation, str))
        ):
            raise DetailPlanError("DETAIL_PLAN_MALFORMED", "a planned image member is invalid")
        planned.append(
            PlannedImage(
                image["item_id"], position, image["asset_kind"], image["sha256"], derivation
            )
        )
    return DetailPlan(
        composition_revision=document["composition_revision"],
        sections=tuple(sections),
        body_format=document["body_format"],
        renderer=document["renderer"],
        body=document["body"],
        images=tuple(planned),
    )


class UploadedProviderAsset:
    """The provider identity of one uploaded asset — the only reference the renderer emits.

    It is a type of its own, apart from any operator string: it is built only from a Snapshot's
    prepared ``publication_assets`` reference, and only when that reference is a safe provider
    reference (opaque, or plain https without query, fragment or user information)."""

    __slots__ = ("_reference",)

    def __init__(self, reference: object) -> None:
        if not isinstance(reference, str) or not safe_provider_reference(reference):
            raise DetailPlanError("DETAIL_ASSET_REFERENCE_UNSAFE", "not a safe provider reference")
        self._reference = reference

    @property
    def reference(self) -> str:
        return self._reference

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UploadedProviderAsset) and other._reference == self._reference

    def __hash__(self) -> int:
        return hash(self._reference)


def _paragraphs(body: str) -> list[str]:
    """``BODY`` as escaped paragraphs: a blank line separates paragraphs and a line break is kept.
    The text is never parsed: every ``&``, ``<``, ``>``, ``"`` and ``'`` is escaped."""
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    rendered: list[str] = []
    for paragraph in re.split(r"\n[ \t]*\n", text):
        lines = paragraph.split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            rendered.append(
                "<p>" + "<br>".join(html.escape(line, quote=True) for line in lines) + "</p>"
            )
    return rendered


def render(plan: DetailPlan, uploaded: Mapping[tuple[str, str, str], UploadedProviderAsset]) -> str:
    """``detail-renderer/v1``: the provider detail body of one frozen plan.

    Sections in plan order; ``DETAIL_IMAGES`` as one image element per planned image, in plan order,
    each pointing at that image's uploaded provider reference; ``BODY`` as escaped paragraphs.
    """
    if plan.renderer not in RENDERERS:
        raise DetailPlanError("DETAIL_RENDERER_UNKNOWN", plan.renderer)
    if plan.body_format != BODY_FORMAT_PLAIN_TEXT:
        raise DetailPlanError("DETAIL_PLAN_MALFORMED", "the body is not plain text")
    if len(set(plan.sections)) != len(plan.sections) or not set(plan.sections) <= {
        SECTION_DETAIL_IMAGES,
        SECTION_BODY,
    }:
        raise DetailPlanError("DETAIL_PLAN_MALFORMED", "a section is not in the vocabulary")
    if plan.images and not plan.places_images:
        raise DetailPlanError("DETAIL_PLAN_MALFORMED", "images are planned without their section")
    parts: list[str] = []
    for section in plan.sections:
        if section == SECTION_DETAIL_IMAGES:
            for image in plan.images:
                asset = uploaded.get(image.key)
                if not isinstance(asset, UploadedProviderAsset):
                    raise DetailPlanError(
                        "DETAIL_IMAGE_NOT_UPLOADED",
                        f"image {image.sha256} has no provider identity",
                    )
                parts.append(f'<img src="{html.escape(asset.reference, quote=True)}" alt="">')
        else:
            parts.extend(_paragraphs(plan.body))
    if not parts:
        raise DetailPlanError("DETAIL_CONTENT_EMPTY", "the plan renders nothing")
    return "\n".join(parts)
