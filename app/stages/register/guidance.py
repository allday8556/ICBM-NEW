"""ADR-0033 G4: a product's Detail Guidance choice and the resolution of its notices (§5, §6).

**The choice** (§5). A preparation revision holds, per placement ``TOP`` / ``BOTTOM``, one of:

- ``DEFAULT`` (the default): the store-wide notices apply;
- ``OFF``: no notice at this placement for this product;
- ``CUSTOM``: ``{template, content}`` of this product's own standing notice. It replaces the
  store-wide standing notice; period notices still apply. Its image is rendered and stored by the
  Detail Guidance owner when the preparation is saved, and its SHA-256 is recorded with the choice
  by the server, never taken from a client.

An absent choice is ``DEFAULT`` at both placements, and a ``DEFAULT`` choice encodes as nothing, so
every revision authored before G4 keeps its exact document and digest.

**The resolution** (§6) is here and nowhere else: :func:`resolve`. At the injected clock's instant
``t``, per placement — ``OFF`` is empty; otherwise the enabled ``PERIOD`` revisions with
``starts_at ≤ t < ends_at``, ordered by ``(starts_at, guidance_id)``, then the product's ``CUSTOM``
notice, else the current enabled ``STANDING`` revision. Each entry is
``{source: STORE|PRODUCT, guidance_revision_id | preparation_revision_id, template, sha256}``.

REGISTER imports no Detail Guidance module: the owner is read and asked to record a custom image
only through :class:`GuidanceSource`, which the composition root wires (as the enrichment results
are, ADR-0026 AIF-4). Pure apart from that port: no database, no clock, no I/O.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol

from app.platform.core.errors import InputValidationError

PLACEMENTS: Final = ("TOP", "BOTTOM")
# The detail-composition section that would place each placement's images (B-DETAIL D1 reserves
# them; content v3 places them, ADR-0033 §7 — G5).
PLACEMENT_SECTIONS: Final[Mapping[str, str]] = {"TOP": "TOP_GUIDANCE", "BOTTOM": "BOTTOM_GUIDANCE"}
SOURCE_STORE: Final = "STORE"
SOURCE_PRODUCT: Final = "PRODUCT"
# A choice that is not one of the three modes, or a CUSTOM one without a template or content.
GUIDANCE_CHOICE_INVALID: Final = "REGISTER_GUIDANCE_CHOICE_INVALID"
# A CUSTOM notice was submitted but no Detail Guidance owner is wired to render it.
GUIDANCE_UNAVAILABLE: Final = "REGISTER_GUIDANCE_UNAVAILABLE"


class GuidanceMode(StrEnum):
    DEFAULT = "DEFAULT"
    OFF = "OFF"
    CUSTOM = "CUSTOM"


@dataclass(frozen=True)
class PlacementChoice:
    """One placement's choice. ``template``, ``content`` and ``sha256`` are set only for
    ``CUSTOM``; ``sha256`` is the server's rendering of exactly that content and template."""

    mode: GuidanceMode = GuidanceMode.DEFAULT
    template: str | None = None
    content: Mapping[str, Any] | None = None
    sha256: str | None = None

    def encode(self) -> dict[str, Any]:
        if self.mode is not GuidanceMode.CUSTOM:
            return {"mode": self.mode.value}
        return {
            "mode": self.mode.value,
            "template": self.template,
            "content": dict(self.content or {}),
            "sha256": self.sha256,
        }


DEFAULT_PLACEMENT: Final = PlacementChoice()


@dataclass(frozen=True)
class GuidanceChoice:
    """A product's choice at both placements."""

    top: PlacementChoice = DEFAULT_PLACEMENT
    bottom: PlacementChoice = DEFAULT_PLACEMENT

    def of(self, placement: str) -> PlacementChoice:
        return self.top if placement == "TOP" else self.bottom

    @property
    def is_default(self) -> bool:
        return self.top.mode is GuidanceMode.DEFAULT and self.bottom.mode is GuidanceMode.DEFAULT

    def encode(self) -> dict[str, Any]:
        return {"top": self.top.encode(), "bottom": self.bottom.encode()}


DEFAULT_CHOICE: Final = GuidanceChoice()


def _placement(raw: Any) -> PlacementChoice:
    if not isinstance(raw, Mapping):
        raise ValueError("a recorded guidance placement is an object")
    mode = GuidanceMode(raw["mode"])
    if mode is not GuidanceMode.CUSTOM:
        return PlacementChoice(mode)
    return PlacementChoice(mode, str(raw["template"]), dict(raw["content"]), str(raw["sha256"]))


def decode_choice(raw: Mapping[str, Any] | None) -> GuidanceChoice:
    """The recorded choice; ``None`` (a revision authored before G4, or ``DEFAULT``) is DEFAULT."""
    if raw is None:
        return DEFAULT_CHOICE
    return GuidanceChoice(top=_placement(raw["top"]), bottom=_placement(raw["bottom"]))


def requested_placement(
    mode: str, template: str | None, content: Mapping[str, Any] | None
) -> PlacementChoice:
    """What a client asked for at one placement, before the owner renders a CUSTOM notice. A
    client never names the image: the SHA-256 is the server's."""
    try:
        parsed = GuidanceMode(mode)
    except ValueError:
        raise InputValidationError(
            GUIDANCE_CHOICE_INVALID,
            "a guidance placement is DEFAULT, OFF or CUSTOM",
            details={"mode": mode},
        ) from None
    if parsed is not GuidanceMode.CUSTOM:
        if template is not None or content is not None:
            raise InputValidationError(
                GUIDANCE_CHOICE_INVALID,
                "only a CUSTOM notice carries a template and content",
                details={"mode": parsed.value},
            )
        return PlacementChoice(parsed)
    if template is None or content is None:
        raise InputValidationError(
            GUIDANCE_CHOICE_INVALID, "a CUSTOM notice names its template and its content"
        )
    return PlacementChoice(parsed, template, dict(content), None)


# ---------------------------------------------------------------- the owner, as REGISTER reads it


class GuidanceRevision(Protocol):
    """One Detail Guidance revision as the owner holds it (the newest of its notice)."""

    @property
    def revision_id(self) -> str: ...
    @property
    def guidance_id(self) -> str: ...
    @property
    def placement(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def template(self) -> str: ...
    @property
    def enabled(self) -> bool: ...
    @property
    def starts_at(self) -> datetime | None: ...
    @property
    def ends_at(self) -> datetime | None: ...
    @property
    def image_sha256(self) -> str: ...


class CustomImage(Protocol):
    """A product's own notice, validated, rendered and recorded by the owner."""

    @property
    def content(self) -> Mapping[str, Any]: ...
    @property
    def template(self) -> str: ...
    @property
    def sha256(self) -> str: ...


class GuidanceSource(Protocol):
    """The Detail Guidance owner (``DetailGuidanceStore``), as REGISTER uses it."""

    def newest(self) -> tuple[datetime, Sequence[GuidanceRevision]]:
        """The injected clock's instant and the newest revision of every notice."""
        ...

    def record_custom_image(
        self,
        content: Mapping[str, Any],
        template: str,
        *,
        actor: str,
        correlation_id: str,
    ) -> CustomImage:
        """Validate (``GUIDANCE_TEXT_INVALID``), render and record a product's own notice."""
        ...


# ---------------------------------------------------------------- the resolution (§6)


@dataclass(frozen=True)
class GuidanceEntry:
    """One resolved notice. ``kind`` (``PERIOD`` / ``STANDING``) is for display only and is not
    part of the canonical entry the fingerprint names."""

    source: str
    template: str
    sha256: str
    guidance_revision_id: str | None = None
    preparation_revision_id: str | None = None
    kind: str = "STANDING"
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    def canonical(self) -> dict[str, Any]:
        identity = (
            {"guidance_revision_id": self.guidance_revision_id}
            if self.source == SOURCE_STORE
            else {"preparation_revision_id": self.preparation_revision_id}
        )
        return {"source": self.source, **identity, "template": self.template, "sha256": self.sha256}


@dataclass(frozen=True)
class ResolvedGuidance:
    """The resolved list of each placement, in order."""

    top: tuple[GuidanceEntry, ...] = ()
    bottom: tuple[GuidanceEntry, ...] = ()

    def of(self, placement: str) -> tuple[GuidanceEntry, ...]:
        return self.top if placement == "TOP" else self.bottom

    @property
    def empty(self) -> bool:
        return not self.top and not self.bottom

    def canonical(self) -> dict[str, Any]:
        return {
            "top": [entry.canonical() for entry in self.top],
            "bottom": [entry.canonical() for entry in self.bottom],
        }


NO_GUIDANCE: Final = ResolvedGuidance()


def _active(revision: GuidanceRevision, now: datetime) -> bool:
    """A period notice applies only inside the half-open ``[starts_at, ends_at)`` (DG-04)."""
    starts, ends = revision.starts_at, revision.ends_at
    return starts is not None and ends is not None and starts <= now < ends


def resolve(
    choice: GuidanceChoice,
    now: datetime,
    revisions: Sequence[GuidanceRevision],
    preparation_revision_id: str | None,
) -> ResolvedGuidance:
    """ADR-0033 §6, deterministic for its inputs (DG-05)."""
    lists: dict[str, tuple[GuidanceEntry, ...]] = {}
    for placement in PLACEMENTS:
        pick = choice.of(placement)
        if pick.mode is GuidanceMode.OFF:
            lists[placement] = ()
            continue
        mine = [r for r in revisions if str(r.placement) == placement and r.enabled]
        periods = sorted(
            (r for r in mine if str(r.kind) == "PERIOD" and _active(r, now)),
            key=lambda r: (r.starts_at, r.guidance_id),
        )
        entries = [
            GuidanceEntry(
                source=SOURCE_STORE,
                template=str(r.template),
                sha256=r.image_sha256,
                guidance_revision_id=r.revision_id,
                kind="PERIOD",
                starts_at=r.starts_at,
                ends_at=r.ends_at,
            )
            for r in periods
        ]
        if pick.mode is GuidanceMode.CUSTOM:
            assert pick.template is not None and pick.sha256 is not None
            entries.append(
                GuidanceEntry(
                    source=SOURCE_PRODUCT,
                    template=pick.template,
                    sha256=pick.sha256,
                    preparation_revision_id=preparation_revision_id,
                )
            )
        else:
            standing = sorted(
                (r for r in mine if str(r.kind) == "STANDING"), key=lambda r: r.guidance_id
            )
            entries.extend(
                GuidanceEntry(
                    source=SOURCE_STORE,
                    template=str(r.template),
                    sha256=r.image_sha256,
                    guidance_revision_id=r.revision_id,
                )
                for r in standing[:1]
            )
        lists[placement] = tuple(entries)
    return ResolvedGuidance(top=lists["TOP"], bottom=lists["BOTTOM"])
