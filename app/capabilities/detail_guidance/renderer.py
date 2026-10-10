"""The one Detail Guidance renderer (ADR-0033 §3, DG-02).

``render(content, template)`` draws a validated notice into a PNG with the bundled Noto Sans KR
(SIL OFL 1.1) and one of the five official templates. The settings preview, the editor preview
and the registration all use it. The bytes depend only on ``(content, template, RENDERER_VERSION)``:
fixed fonts, sizes and encoder parameters, and no time, metadata or randomness. A change of the
font, the Pillow pin or any drawing below bumps ``RENDERER_VERSION``.

Text is never shrunk, wrapped or cut: a line wider than the template's text box is refused with
``GUIDANCE_TEXT_TOO_WIDE`` (DG-09). A template adds no text of its own.
"""

import hashlib
import io
import struct
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from app.capabilities.detail_guidance.content import GuidanceContent
from app.platform.core.errors import InputValidationError

RENDERER_VERSION: Final = "guidance-renderer/v1"
FONT_FILE: Final = Path(__file__).parent / "fonts" / "NotoSansKR-wght.ttf"
FONT_SHA256: Final = "194018e6b2b293a7964f037b25c0249ce1418bc9ab3c971060a03aa57861e252"
GUIDANCE_TEXT_TOO_WIDE: Final = "GUIDANCE_TEXT_TOO_WIDE"

CANVAS_WIDTH: Final = 860
OUTER: Final = 22
PADDING: Final = 22
TEXT_BOX_WIDTH: Final = CANVAS_WIDTH - 2 * OUTER - 2 * PADDING
HEADING_SIZE: Final = 26
HEADING_WEIGHT: Final = 700
HEADING_LINE: Final = 40
HEADING_GAP: Final = 6
BODY_SIZE: Final = 19
BODY_WEIGHT: Final = 400
BODY_LINE: Final = 30
BLOCK_GAP: Final = 24
ACCENT_WIDTH: Final = 6
BAND_HEIGHT: Final = 96
MARK_HEIGHT: Final = 56
SUPERSAMPLE: Final = 4


class Template(StrEnum):
    CLEAN = "CLEAN"
    MODERN = "MODERN"
    WARM = "WARM"
    DOMESTIC = "DOMESTIC"
    OVERSEAS = "OVERSEAS"


@dataclass(frozen=True)
class _Style:
    background: str
    panel: str
    border: str | None
    radius: int
    heading: str
    text: str
    rule: str
    accent: str | None
    band: str | None = None
    mark: str | None = None


_STYLES: Final[dict[Template, _Style]] = {
    Template.CLEAN: _Style(
        "#ffffff", "#ffffff", "#d1d5db", 16, "#111827", "#374151", "#e5e7eb", None
    ),
    Template.MODERN: _Style(
        "#111827", "#1f2937", None, 20, "#f9fafb", "#d1d5db", "#374151", "#38bdf8"
    ),
    Template.WARM: _Style(
        "#fff7ed", "#ffffff", "#fdba74", 24, "#c2410c", "#431407", "#fed7aa", None
    ),
    Template.DOMESTIC: _Style(
        "#eff6ff",
        "#ffffff",
        "#93c5fd",
        20,
        "#1d4ed8",
        "#1f2937",
        "#dbeafe",
        None,
        band="#2563eb",
        mark="truck",
    ),
    Template.OVERSEAS: _Style(
        "#f0f9ff",
        "#ffffff",
        "#7dd3fc",
        20,
        "#0369a1",
        "#0f172a",
        "#e0f2fe",
        None,
        band="#0c4a6e",
        mark="plane",
    ),
}


@dataclass(frozen=True)
class RenderedGuidance:
    png: bytes
    sha256: str
    width: int
    height: int
    template: Template
    renderer_version: str
    font_sha256: str


@cache
def _font_bytes() -> bytes:
    data = FONT_FILE.read_bytes()
    if hashlib.sha256(data).hexdigest() != FONT_SHA256:
        raise RuntimeError("the bundled guidance font is not the pinned file")
    return data


@cache
def _font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(io.BytesIO(_font_bytes()), size)
    font.set_variation_by_axes([weight])
    return font


def _cmap_codepoints(data: bytes) -> frozenset[int]:
    """The code points the font maps to a real glyph (``cmap`` format 12, else format 4)."""
    (num_tables,) = struct.unpack_from(">H", data, 4)
    tables = {
        data[12 + 16 * i : 16 + 16 * i]: struct.unpack_from(">I", data, 20 + 16 * i)[0]
        for i in range(num_tables)
    }
    cmap = tables[b"cmap"]
    (_, count) = struct.unpack_from(">HH", data, cmap)
    subtables = {}
    for i in range(count):
        platform, encoding, offset = struct.unpack_from(">HHI", data, cmap + 4 + 8 * i)
        subtables[(platform, encoding)] = cmap + offset
    points: set[int] = set()
    if (3, 10) in subtables:
        at = subtables[(3, 10)]
        (groups,) = struct.unpack_from(">I", data, at + 12)
        for g in range(groups):
            start, end, glyph = struct.unpack_from(">III", data, at + 16 + 12 * g)
            points.update(c for c in range(start, end + 1) if glyph + (c - start))
        return frozenset(points)
    at = subtables[(3, 1)]
    (seg_x2,) = struct.unpack_from(">H", data, at + 6)
    segs = seg_x2 // 2
    ends = struct.unpack_from(f">{segs}H", data, at + 14)
    starts = struct.unpack_from(f">{segs}H", data, at + 16 + seg_x2)
    deltas = struct.unpack_from(f">{segs}h", data, at + 16 + 2 * seg_x2)
    range_at = at + 16 + 3 * seg_x2
    offsets = struct.unpack_from(f">{segs}H", data, range_at)
    for s in range(segs):
        for c in range(starts[s], ends[s] + 1):
            if c == 0xFFFF:
                continue
            if offsets[s] == 0:
                glyph = (c + deltas[s]) & 0xFFFF
            else:
                (glyph,) = struct.unpack_from(
                    ">H", data, range_at + 2 * s + offsets[s] + 2 * (c - starts[s])
                )
                glyph = (glyph + deltas[s]) & 0xFFFF if glyph else 0
            if glyph:
                points.add(c)
    return frozenset(points)


@cache
def drawable_codepoints() -> frozenset[int]:
    """Every code point the bundled font can draw; ``content.guidance_content`` refuses the rest."""
    return _cmap_codepoints(_font_bytes())


def _check_widths(content: GuidanceContent) -> None:
    heading, body = _font(HEADING_SIZE, HEADING_WEIGHT), _font(BODY_SIZE, BODY_WEIGHT)
    for b, block in enumerate(content.blocks):
        texts = [(f"blocks[{b}].heading", block.heading, heading)]
        texts += [(f"blocks[{b}].lines[{i}]", line, body) for i, line in enumerate(block.lines)]
        for where, text, font in texts:
            if text and font.getlength(text) > TEXT_BOX_WIDTH:
                raise InputValidationError(
                    GUIDANCE_TEXT_TOO_WIDE,
                    "a line is wider than the template's text box",
                    details={"where": where, "width": TEXT_BOX_WIDTH},
                )


def _mark(kind: str, band: str) -> Image.Image:
    """A shipping mark (ADR-0033 §11): shapes only, drawn large and reduced for smooth edges."""
    k = SUPERSAMPLE * MARK_HEIGHT / 72
    mark = Image.new("RGBA", (round(120 * k), round(72 * k)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mark)

    def box(*xy: float) -> tuple[float, ...]:
        return tuple(v * k for v in xy)

    white = "#ffffff"
    if kind == "truck":
        draw.rounded_rectangle(box(2, 8, 72, 52), radius=4 * k, fill=white)
        draw.polygon(box(74, 20, 96, 20, 114, 38, 114, 52, 74, 52), fill=white)
        draw.polygon(box(80, 26, 94, 26, 104, 38, 80, 38), fill=band)
        for cx in (22, 92):
            draw.ellipse(box(cx - 11, 47, cx + 11, 69), fill=white)
            draw.ellipse(box(cx - 4, 54, cx + 4, 62), fill=band)
    else:
        draw.ellipse(box(8, 30, 116, 42), fill=white)
        draw.polygon(box(54, 32, 34, 2, 46, 2, 80, 32), fill=white)
        draw.polygon(box(54, 40, 34, 70, 46, 70, 80, 40), fill=white)
        draw.polygon(box(14, 32, 4, 14, 12, 14, 28, 32), fill=white)
        draw.polygon(box(14, 40, 4, 58, 12, 58, 28, 40), fill=white)
    size = (round(mark.width / SUPERSAMPLE), round(mark.height / SUPERSAMPLE))
    return mark.resize(size, Image.Resampling.LANCZOS)


def _band(image: Image.Image, style: _Style) -> None:
    assert style.band and style.mark
    draw = ImageDraw.Draw(image)
    top, left, right = OUTER, OUTER, CANVAS_WIDTH - OUTER - 1
    draw.rounded_rectangle(
        (left, top, right, top + BAND_HEIGHT),
        radius=style.radius,
        fill=style.band,
        corners=(True, True, False, False),
    )
    mark = _mark(style.mark, style.band)
    mark_x = OUTER + PADDING
    mark_y = top + (BAND_HEIGHT - mark.height) // 2
    image.paste(mark, (mark_x, mark_y), mark)
    middle = top + BAND_HEIGHT // 2
    dash, gap = (18, 12) if style.mark == "truck" else (6, 10)
    x = mark_x + mark.width + 28
    while x + dash <= right - PADDING:
        draw.rounded_rectangle((x, middle - 2, x + dash, middle + 2), radius=2, fill="#ffffff")
        x += dash + gap


def _height(content: GuidanceContent, style: _Style) -> int:
    inner = BAND_HEIGHT if style.band else 0
    for b, block in enumerate(content.blocks):
        if b:
            inner += BLOCK_GAP
        if block.heading:
            inner += HEADING_LINE + HEADING_GAP
        inner += BODY_LINE * len(block.lines)
    return 2 * OUTER + 2 * PADDING + inner


def render(content: GuidanceContent, template: Template) -> RenderedGuidance:
    """Draw ``content`` with ``template`` into a PNG."""
    _check_widths(content)
    style = _STYLES[template]
    height = _height(content, style)
    image = Image.new("RGB", (CANVAS_WIDTH, height), style.background)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (OUTER, OUTER, CANVAS_WIDTH - OUTER - 1, height - OUTER - 1),
        radius=style.radius,
        fill=style.panel,
        outline=style.border,
        width=2 if style.border else 0,
    )
    if style.band:
        _band(image, style)
    heading, body = _font(HEADING_SIZE, HEADING_WEIGHT), _font(BODY_SIZE, BODY_WEIGHT)
    x, y = OUTER + PADDING, OUTER + PADDING + (BAND_HEIGHT if style.band else 0)
    for b, block in enumerate(content.blocks):
        if b:
            rule_y = y + BLOCK_GAP // 2
            draw.line((x, rule_y, x + TEXT_BOX_WIDTH, rule_y), fill=style.rule, width=1)
            y += BLOCK_GAP
        if block.heading:
            if style.accent:
                draw.rectangle(
                    (
                        x - PADDING // 2,
                        y + 8,
                        x - PADDING // 2 + ACCENT_WIDTH - 1,
                        y + HEADING_LINE - 8,
                    ),
                    fill=style.accent,
                )
            draw.text((x, y), block.heading, font=heading, fill=style.heading)
            y += HEADING_LINE + HEADING_GAP
        for line in block.lines:
            draw.text((x, y), line, font=body, fill=style.text)
            y += BODY_LINE
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=6)
    png = buffer.getvalue()
    return RenderedGuidance(
        png=png,
        sha256=hashlib.sha256(png).hexdigest(),
        width=CANVAS_WIDTH,
        height=height,
        template=template,
        renderer_version=RENDERER_VERSION,
        font_sha256=FONT_SHA256,
    )


def preview(content: GuidanceContent) -> tuple[RenderedGuidance, ...]:
    """The five official templates for one notice, in template order; nothing is stored."""
    return tuple(render(content, template) for template in Template)
