"""The one image-translation renderer ``image-translate-renderer/v2`` (ADR-0041 §4–§6, IT-03/04).

``render(source, regions)`` draws the operator's Korean text over a source image and returns a new
PNG; the source bytes are only read. Per included region, in the given order:

1. the background is read (``style.read_background``); the source text is erased — the box filled
   with the ring's median colour — only when the operator asks for it and the erase is clean;
2. the style is matched (``style.measure_style``), then the operator's override applies field by
   field; a sans face is drawn with Noto Sans KR and a serif face with Noto Serif KR (D7, I7),
   each at its own weight on its ``wght`` axis and checked against its own glyph set;
3. the text is wrapped to the box's width and shrunk from the matched size down to
   ``MIN_FONT_SIZE``; text that still does not fit is ``LAYOUT_OVERFLOW`` and is never cut or drawn;
4. where the text was not erased, the Korean text is placed on a solid label filling the box: an
   overlay, never a repaint of the picture.

No pixel outside an included region's box changes: each region is drawn on a crop of its own box
and pasted back, and a kept region's box is restored from the source last. A region whose text the
font cannot draw, or that does not fit, is left untouched. The completeness verdict (§6) fails
unless every included region is drawn, within its box, with every glyph in the font, and neither a
kept region's box nor a later region's fill covers it.

The bytes depend only on the source, the regions, the two bundled fonts (each pinned by SHA-256:
Noto Sans KR, shared with Detail Guidance, and Noto Serif KR, ``fonts/``) and the Pillow pin:
BASIC text layout, fixed encoder parameters, no time, metadata or randomness (only the source's
ICC profile is carried over). A change of a font, the Pillow pin or any drawing below bumps
``RENDERER_VERSION``. v2 (I7) added the serif face: a sans region draws the same bytes as in v1.
"""

import hashlib
import io
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import cache, lru_cache
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from app.capabilities.detail_guidance.renderer import FONT_FILE, FONT_SHA256, cmap_codepoints
from app.capabilities.image_studio.style import (
    FACE_NOTE,
    Align,
    BackgroundReading,
    Box,
    Face,
    MeasuredStyle,
    Rgb,
    Weight,
    measure_style,
    read_background,
)
from app.platform.core.errors import InputValidationError

RENDERER_VERSION: Final = "image-translate-renderer/v2"
# Noto Serif KR (SIL OFL 1.1, ``fonts/OFL.txt``), the variable-weight file of google/fonts
# ``ofl/notoserifkr/NotoSerifKR[wght].ttf`` (git blob cc85b383766708c9291f72baeeab05760774f492).
SERIF_FONT_FILE: Final = Path(__file__).parent / "fonts" / "NotoSerifKR-wght.ttf"
SERIF_FONT_SHA256: Final = "11f8d5de6f1b79195efba3828aaa2ec95c1178f5ae976fb23c8d53250a9938f3"
FONT_SHA256S: Final = (FONT_SHA256, SERIF_FONT_SHA256)
IMAGE_TRANSLATE_INPUT_INVALID: Final = "IMAGE_TRANSLATE_INPUT_INVALID"

MIN_FONT_SIZE: Final = 10
MAX_FONT_SIZE: Final = 400
LINE_SPACING: Final = 1.2
WEIGHT_AXIS: Final[dict[Weight, int]] = {Weight.REGULAR: 400, Weight.BOLD: 700, Weight.BLACK: 900}
LABEL_LIGHT: Final[Rgb] = (255, 255, 255)
LABEL_DARK: Final[Rgb] = (17, 17, 17)

COMPLEX_BACKGROUND: Final = "COMPLEX_BACKGROUND"
LAYOUT_OVERFLOW: Final = "LAYOUT_OVERFLOW"
MISSING_GLYPHS: Final = "MISSING_GLYPHS"
OVERLAPS_KEPT_REGION: Final = "OVERLAPS_KEPT_REGION"
OUT_OF_BOX: Final = "OUT_OF_BOX"
OVERDRAWN: Final = "OVERDRAWN"


class RegionStatus(StrEnum):
    RENDERED = "RENDERED"
    KEPT = "KEPT"
    OVERFLOW = "OVERFLOW"
    NOT_ERASED_COMPLEX_BACKGROUND = "NOT_ERASED_COMPLEX_BACKGROUND"
    MISSING_GLYPHS = "MISSING_GLYPHS"


class Placement(StrEnum):
    INLINE = "INLINE"  # on the erased box, in the matched style
    LABEL = (
        "LABEL"  # on a solid label filling the box; the source pixels are covered, not repainted
    )


@dataclass(frozen=True)
class StyleOverride:
    """The operator's per-region style; a ``None`` field keeps the matched value (§5)."""

    weight: Weight | None = None
    face: Face | None = None
    size: int | None = None
    colour: Rgb | None = None


@dataclass(frozen=True)
class RenderRegion:
    """One region of an edit revision as the renderer reads it.

    ``include`` False keeps the original pixels untouched. ``erase`` is the operator's erase
    decision; it is honoured only where the erase is clean. ``korean_text`` may hold ``\\n`` line
    breaks; other lines wrap at the box's width."""

    region_id: str
    box: Box
    source_text: str
    korean_text: str
    include: bool
    erase: bool
    override: StyleOverride | None = None


@dataclass(frozen=True)
class TextStyle:
    weight: Weight
    face: Face
    size: int
    colour: Rgb


@dataclass(frozen=True)
class RegionOutcome:
    """What the renderer did with one region; ``ink`` is the drawn text's extent in the box."""

    region_id: str
    status: RegionStatus
    background: BackgroundReading
    measured: MeasuredStyle
    flags: tuple[str, ...]
    erased: bool = False
    placement: Placement | None = None
    style: TextStyle | None = None
    face_matched: Face | None = None
    lines: tuple[str, ...] = ()
    ink: tuple[int, int, int, int] | None = None
    missing_glyphs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompletenessFailure:
    region_id: str
    code: str


@dataclass(frozen=True)
class Completeness:
    complete: bool
    failures: tuple[CompletenessFailure, ...]


@dataclass(frozen=True)
class RenderResult:
    png: bytes
    sha256: str
    width: int
    height: int
    renderer_version: str
    font_sha256s: tuple[str, ...]
    regions: tuple[RegionOutcome, ...]
    completeness: Completeness


@dataclass(frozen=True)
class _Line:
    text: str
    x: int  # the anchor ("ls": left, baseline) relative to the box
    baseline: int
    ink: tuple[int, int, int, int]  # relative to the box; right and bottom exclusive


@dataclass(frozen=True)
class _Plan:
    region: RenderRegion
    outcome: RegionOutcome
    lines: tuple[_Line, ...] = ()
    label: Rgb | None = None


# Each face's bundled font file and its pinned SHA-256.
FACE_FONTS: Final[dict[Face, tuple[Path, str]]] = {
    Face.SANS: (FONT_FILE, FONT_SHA256),
    Face.SERIF: (SERIF_FONT_FILE, SERIF_FONT_SHA256),
}
# The font files an edit revision names (IT-03): a revision saved under another set is refused by
# 「적용」 and saved again.
FONT_HASHES: Final = {path.name: sha256 for path, sha256 in FACE_FONTS.values()}


@cache
def _font_bytes(face: Face) -> bytes:
    path, sha256 = FACE_FONTS[face]
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise RuntimeError(f"the bundled Korean {face.value.lower()} font is not the pinned file")
    return data


@cache
def drawable_codepoints(face: Face) -> frozenset[int]:
    """Every code point the face's bundled font can draw; a region needing any other is
    ``MISSING_GLYPHS``. The two fonts differ by a few dozen Hanja."""
    return cmap_codepoints(_font_bytes(face))


@lru_cache(maxsize=128)
def _font(size: int, weight: Weight, face: Face) -> ImageFont.FreeTypeFont:
    # BASIC layout on every platform, as Detail Guidance: raqm would place glyphs differently.
    data = io.BytesIO(_font_bytes(face))
    font = ImageFont.truetype(data, size, layout_engine=ImageFont.Layout.BASIC)
    font.set_variation_by_axes([WEIGHT_AXIS[weight]])
    return font


def _refuse(message: str, **details: object) -> InputValidationError:
    return InputValidationError(IMAGE_TRANSLATE_INPUT_INVALID, message, details=dict(details))


def decode_source(source: bytes) -> Image.Image:
    """The source as the renderer reads it: decoded with no EXIF orientation, RGB (RGBA when it
    has alpha). OCR scores a region's background on exactly this image (I2)."""
    try:
        image = Image.open(io.BytesIO(source))
        image.load()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise _refuse("the source is not a readable image") from exc
    alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    converted = image.convert("RGBA" if alpha else "RGB")
    icc = image.info.get("icc_profile")
    converted.info = {"icc_profile": icc} if icc else {}
    return converted


def _check(image: Image.Image, regions: Sequence[RenderRegion]) -> None:
    ids = [r.region_id for r in regions]
    if len(set(ids)) != len(ids) or not all(ids):
        raise _refuse("every region needs its own id")
    for region in regions:
        if not region.box.inside(image.width, image.height):
            raise _refuse("a region's box is outside the image", region_id=region.region_id)
        if region.include and not region.korean_text.strip():
            raise _refuse("an included region has no Korean text", region_id=region.region_id)
        size = region.override.size if region.override else None
        if size is not None and not MIN_FONT_SIZE <= size <= MAX_FONT_SIZE:
            raise _refuse("an override size is out of range", region_id=region.region_id)


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str] | None:
    """Greedy wrap at spaces, breaking a word that is wider than ``width`` between characters."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if font.getlength(candidate) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            for char in word:
                if font.getlength(current + char) <= width:
                    current += char
                elif not current:
                    return None
                else:
                    lines.append(current)
                    current = char
        lines.append(current)
    return lines


def _layout(
    text: str, style: TextStyle, align: Align, width: int, height: int, pad: int
) -> tuple[_Line, ...] | None:
    """The lines at ``style.size`` in a ``width`` x ``height`` box, or None when they overflow."""
    font = _font(style.size, style.weight, style.face)
    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    if inner_w <= 0 or inner_h <= 0:
        return None
    wrapped = _wrap(text, font, inner_w)
    if wrapped is None:
        return None
    pitch = round(style.size * LINE_SPACING)
    inked = [
        (i, tuple(int(v) for v in font.getbbox(line, anchor="ls")))
        for i, line in enumerate(wrapped)
        if line
    ]
    if not inked:
        return None
    top = min(i * pitch + b[1] for i, b in inked)
    bottom = max(i * pitch + b[3] for i, b in inked)
    if bottom - top > inner_h or any(b[2] - b[0] > inner_w for _, b in inked):
        return None
    first_baseline = pad + (inner_h - (bottom - top)) // 2 - top
    placed = []
    for i, b in inked:
        ink_w = b[2] - b[0]
        if align is Align.LEFT:
            left = pad
        elif align is Align.RIGHT:
            left = pad + inner_w - ink_w
        else:
            left = pad + (inner_w - ink_w) // 2
        x, baseline = left - b[0], first_baseline + i * pitch
        ink = (x + b[0], baseline + b[1], x + b[2], baseline + b[3])
        placed.append(_Line(wrapped[i], x, baseline, ink))
    return tuple(placed)


def _style(measured: MeasuredStyle, override: StyleOverride | None) -> TextStyle:
    o = override or StyleOverride()
    return TextStyle(
        weight=o.weight or measured.weight,
        face=o.face or measured.face,
        size=o.size or min(MAX_FONT_SIZE, max(MIN_FONT_SIZE, measured.size)),
        colour=o.colour or measured.colour,
    )


def _label(colour: Rgb) -> Rgb:
    r, g, b = colour
    return LABEL_LIGHT if 299 * r + 587 * g + 114 * b < 140_000 else LABEL_DARK


def _fit(
    region: RenderRegion, style: TextStyle, align: Align, pad: int
) -> tuple[TextStyle, tuple[_Line, ...]] | None:
    """Shrink from ``style.size`` down to the floor; an operator's size is tried alone."""
    exact = region.override is not None and region.override.size is not None
    sizes = [style.size] if exact else range(style.size, MIN_FONT_SIZE - 1, -1)
    box = region.box
    for size in sizes:
        fitted = replace(style, size=size)
        lines = _layout(region.korean_text, fitted, align, box.width, box.height, pad)
        if lines is not None:
            return fitted, lines
    return None


def _plan(source: Image.Image, region: RenderRegion) -> _Plan:
    background = read_background(source, region.box)
    measured = measure_style(source, region.box, background.colour)
    flags = set() if background.clean else {COMPLEX_BACKGROUND}
    base = RegionOutcome(region.region_id, RegionStatus.KEPT, background, measured, ())
    if not region.include:
        return _Plan(region, replace(base, flags=tuple(sorted(flags))))
    # The matched face, or the operator's: each face is drawn with its own font, so the glyph
    # check is that font's.
    style = _style(measured, region.override)
    drawable = drawable_codepoints(style.face)
    missing = sorted({c for c in region.korean_text if c != "\n" and ord(c) not in drawable})
    if missing:
        flags.add(MISSING_GLYPHS)
        return _Plan(
            region,
            replace(
                base,
                status=RegionStatus.MISSING_GLYPHS,
                flags=tuple(sorted(flags)),
                missing_glyphs=tuple(missing),
            ),
        )
    notes = (FACE_NOTE,)
    erased = region.erase and background.clean
    pad = 0 if erased else max(1, min(region.box.width, region.box.height) // 10)
    fit = _fit(region, style, measured.align, pad)
    if fit is None:
        flags.add(LAYOUT_OVERFLOW)
        return _Plan(
            region,
            replace(
                base,
                status=RegionStatus.OVERFLOW,
                flags=tuple(sorted(flags)),
                style=style,
                notes=notes,
            ),
        )
    fitted, lines = fit
    status = (
        RegionStatus.NOT_ERASED_COMPLEX_BACKGROUND
        if region.erase and not background.clean
        else RegionStatus.RENDERED
    )
    ink = (
        min(line.ink[0] for line in lines),
        min(line.ink[1] for line in lines),
        max(line.ink[2] for line in lines),
        max(line.ink[3] for line in lines),
    )
    done = replace(
        base,
        status=status,
        flags=tuple(sorted(flags)),
        erased=erased,
        placement=Placement.INLINE if erased else Placement.LABEL,
        style=fitted,
        face_matched=fitted.face,
        lines=tuple(line.text for line in lines),
        ink=ink,
        notes=notes,
    )
    return _Plan(region, done, lines, None if erased else _label(fitted.colour))


def _fill(image: Image.Image, colour: Rgb) -> tuple[int, ...]:
    return (*colour[:3], 255) if image.mode == "RGBA" else tuple(colour[:3])


def _draw(canvas: Image.Image, plan: _Plan) -> None:
    box, outcome = plan.region.box, plan.outcome
    assert outcome.style is not None
    tile = canvas.crop(box.area)
    draw = ImageDraw.Draw(tile)
    if outcome.erased:
        draw.rectangle((0, 0, box.width - 1, box.height - 1), fill=outcome.background.colour)
    elif plan.label is not None:
        draw.rectangle((0, 0, box.width - 1, box.height - 1), fill=_fill(canvas, plan.label))
    font = _font(outcome.style.size, outcome.style.weight, outcome.style.face)
    for line in plan.lines:
        draw.text(
            (line.x, line.baseline),
            line.text,
            font=font,
            fill=_fill(canvas, outcome.style.colour),
            anchor="ls",
        )
    canvas.paste(tile, (box.x, box.y))


def _overdrawn(plan: _Plan, later: Sequence[_Plan]) -> bool:
    """A later drawn region fills its whole box, so it covers this region's text where they meet."""
    if not plan.lines or plan.outcome.ink is None:
        return False
    box = plan.region.box
    left, top, right, bottom = plan.outcome.ink
    ink = Box(box.x + left, box.y + top, right - left, bottom - top)
    return any(other.lines and ink.overlaps(other.region.box) for other in later)


def _completeness(plans: Sequence[_Plan]) -> Completeness:
    """§6: every included region drawn, inside its box, with every glyph, and nothing over it."""
    failures: list[CompletenessFailure] = []
    kept = [p.region.box for p in plans if not p.region.include]
    for at, plan in enumerate(plans):
        region, outcome = plan.region, plan.outcome
        if not region.include:
            continue
        rid = region.region_id
        if outcome.status is RegionStatus.OVERFLOW:
            failures.append(CompletenessFailure(rid, LAYOUT_OVERFLOW))
        elif outcome.status is RegionStatus.MISSING_GLYPHS:
            failures.append(CompletenessFailure(rid, MISSING_GLYPHS))
        else:
            left, top, right, bottom = outcome.ink or (0, 0, 0, 0)
            inside = left >= 0 and top >= 0
            inside = inside and right <= region.box.width and bottom <= region.box.height
            if not plan.lines or not inside:
                failures.append(CompletenessFailure(rid, OUT_OF_BOX))
        if any(region.box.overlaps(box) for box in kept):
            failures.append(CompletenessFailure(rid, OVERLAPS_KEPT_REGION))
        if _overdrawn(plan, plans[at + 1 :]):
            failures.append(CompletenessFailure(rid, OVERDRAWN))
    return Completeness(not failures, tuple(failures))


def render(source: bytes, regions: Sequence[RenderRegion]) -> RenderResult:
    """Draw the included regions' Korean text over ``source`` into a new PNG (ADR-0041 §4–§6)."""
    original = decode_source(source)
    _check(original, regions)
    plans = [_plan(original, region) for region in regions]
    canvas = original.copy()
    for plan in plans:
        if plan.lines:
            _draw(canvas, plan)
    for plan in plans:
        if not plan.region.include:
            box = plan.region.box
            canvas.paste(original.crop(box.area), (box.x, box.y))
    buffer = io.BytesIO()
    icc = original.info.get("icc_profile")
    options: dict[str, object] = {"icc_profile": icc} if icc else {}
    canvas.save(buffer, format="PNG", optimize=False, compress_level=6, **options)
    png = buffer.getvalue()
    return RenderResult(
        png=png,
        sha256=hashlib.sha256(png).hexdigest(),
        width=canvas.width,
        height=canvas.height,
        renderer_version=RENDERER_VERSION,
        font_sha256s=FONT_SHA256S,
        regions=tuple(p.outcome for p in plans),
        completeness=_completeness(plans),
    )
