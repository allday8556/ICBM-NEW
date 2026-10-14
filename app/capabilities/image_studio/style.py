"""Reading one region of a source image (ADR-0041 §4, §5): its background and its text style.

Everything here only reads pixels, deterministically, with Pillow and the standard library.

- ``read_background`` scores the ring of pixels just outside a region's box. The text is erased only
  where that ring is one flat colour and the box itself is mostly that colour (``clean``); the fill
  is the ring's median colour. A gradient, a pattern or a photo is never repainted (IT-04). Its
  ``score`` is the one background-uniformity number an OCR observation records (I2).
- ``measure_style`` reads the source text against that background: its colour (the median of the
  pixels farthest from the background), its weight (stroke width relative to the line height), its
  line height and so its size, and its alignment in the box. The face is always sans: no serif
  detection is attempted, since only Noto Sans KR is bundled (``FACE_NOTE``).

Boxes are in the pixel grid of the decoded source image, with no EXIF orientation applied.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from PIL import Image

Pixel = tuple[int, ...]
Rgb = tuple[int, int, int]

# Two pixels are "the same colour" when no channel differs by more than this. JPEG noise on a flat
# background stays well inside it; a gradient that is visible across a text box does not.
SAME_COLOUR: Final = 12
# A ring is uniform when this share of its pixels is the same colour as its median.
RING_UNIFORMITY_MIN: Final = 0.95
# A region's background-uniformity score (``BackgroundReading.score``) passes from here.
CLEAN_FROM: Final = RING_UNIFORMITY_MIN
# The box must be mostly background: glyphs rarely cover half of a text box, a badge or a photo
# behind the text does.
INTERIOR_MATCH_MIN: Final = 0.5
# The ring is a quarter of the box height wide, between these bounds.
RING_MIN: Final = 2
RING_MAX: Final = 8
# No pixel differs from the background by more than this: the box holds no readable text.
INK_CONTRAST_MIN: Final = 48
# Stroke width / line height (calibrated on Noto Sans KR, Hangul and CJK lines of 20-48 px):
# wght 400 reads 0.061-0.081, wght 700 0.086-0.121, wght 900 0.10-0.15 (dense hanzi read low).
BOLD_FROM: Final = 0.085
BLACK_FROM: Final = 0.128
# A Hangul line's ink is about 0.93 of the font size, so a source line of ink height H is matched
# by Korean text of size H / 0.93.
HANGUL_INK_RATIO: Final = 0.93
FACE_NOTE: Final = "serif detection is not attempted: only Noto Sans KR is bundled (ADR-0041 §5)"


class Weight(StrEnum):
    REGULAR = "REGULAR"
    BOLD = "BOLD"
    BLACK = "BLACK"


class Face(StrEnum):
    SANS = "SANS"
    SERIF = "SERIF"


class Align(StrEnum):
    LEFT = "LEFT"
    CENTER = "CENTER"
    RIGHT = "RIGHT"


@dataclass(frozen=True)
class Box:
    """A region's box in source pixels: ``x``, ``y`` of its top-left corner, then its size."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def area(self) -> tuple[int, int, int, int]:
        """The box as Pillow's ``(left, top, right, bottom)``, right and bottom exclusive."""
        return (self.x, self.y, self.right, self.bottom)

    def inside(self, width: int, height: int) -> bool:
        return (
            self.width > 0
            and self.height > 0
            and self.x >= 0
            and self.y >= 0
            and self.right <= width
            and self.bottom <= height
        )

    def overlaps(self, other: "Box") -> bool:
        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )


@dataclass(frozen=True)
class BackgroundReading:
    """The ring around a box: its median colour, how uniform it is, and whether erasing is clean."""

    colour: Pixel
    ring_uniformity: float
    interior_match: float
    clean: bool

    @property
    def score(self) -> float:
        """The one background-uniformity score an OCR observation carries (ADR-0041 §3, I2): the
        ring's uniformity, capped by the interior match when the box is not mostly background.
        It is this reading projected to one number, not a second metric: it passes
        (``>= CLEAN_FROM``) exactly when the erase is ``clean``."""
        if self.interior_match >= INTERIOR_MATCH_MIN:
            return self.ring_uniformity
        return min(self.ring_uniformity, self.interior_match)


@dataclass(frozen=True)
class MeasuredStyle:
    """The source text's style as read from the pixels (ADR-0041 §5)."""

    weight: Weight
    face: Face
    size: int
    colour: Rgb
    align: Align
    stroke_ratio: float
    line_count: int
    ink_found: bool


def _pixels(image: Image.Image, area: tuple[int, int, int, int]) -> list[Pixel]:
    raw = image.crop(area).tobytes()
    bands = len(image.getbands())
    return [tuple(raw[i : i + bands]) for i in range(0, len(raw), bands)]


def _median(pixels: list[Pixel]) -> Pixel:
    """The per-channel lower median: an exact integer, the same on every platform."""
    return tuple(sorted(channel)[(len(channel) - 1) // 2] for channel in zip(*pixels, strict=True))


def _distance(a: Pixel, b: Pixel) -> int:
    return max(abs(p - q) for p, q in zip(a, b, strict=True))


def _ring(image: Image.Image, box: Box) -> list[Pixel]:
    width = min(RING_MAX, max(RING_MIN, round(box.height / 4)))
    left, top = max(0, box.x - width), max(0, box.y - width)
    right, bottom = min(image.width, box.right + width), min(image.height, box.bottom + width)
    pixels = _pixels(image, (left, top, right, bottom))
    span = right - left
    return [
        pixel
        for i, pixel in enumerate(pixels)
        if not (box.x <= left + i % span < box.right and box.y <= top + i // span < box.bottom)
    ]


def read_background(image: Image.Image, box: Box) -> BackgroundReading:
    """Score the background of ``box``; ``clean`` means the text may be erased (§4)."""
    interior = _pixels(image, box.area)
    ring = _ring(image, box)
    if not ring:
        # The box is the whole image: there is no outside to judge the background by.
        return BackgroundReading(_median(interior), 0.0, 0.0, False)
    colour = _median(ring)
    # Judged on the rounded values the reading carries, so its ``score`` passes exactly when clean.
    uniformity = round(sum(_distance(p, colour) <= SAME_COLOUR for p in ring) / len(ring), 4)
    match = round(sum(_distance(p, colour) <= SAME_COLOUR for p in interior) / len(interior), 4)
    return BackgroundReading(
        colour=colour,
        ring_uniformity=uniformity,
        interior_match=match,
        clean=uniformity >= RING_UNIFORMITY_MIN and match >= INTERIOR_MATCH_MIN,
    )


def _contrasting(background: Pixel) -> Rgb:
    r, g, b = background[:3]
    return (0, 0, 0) if 299 * r + 587 * g + 114 * b >= 128_000 else (255, 255, 255)


def _bands(rows: list[bool], gap: int) -> list[int]:
    """Heights of the runs of ink rows, a gap of up to ``gap`` empty rows joining two runs."""
    heights: list[int] = []
    start = last = -1
    for y, ink in enumerate(rows):
        if not ink:
            continue
        if start < 0 or y - last - 1 > gap:
            if start >= 0:
                heights.append(last - start + 1)
            start = y
        last = y
    if start >= 0:
        heights.append(last - start + 1)
    return heights


def _align(columns: list[bool]) -> Align:
    first = columns.index(True)
    last = len(columns) - 1 - columns[::-1].index(True)
    left, right = first, len(columns) - 1 - last
    if abs(left - right) <= max(2, len(columns) // 10):
        return Align.CENTER
    return Align.LEFT if left < right else Align.RIGHT


def measure_style(image: Image.Image, box: Box, background: Pixel) -> MeasuredStyle:
    """Read the style of the text in ``box`` against ``background`` (§5)."""
    pixels = _pixels(image, box.area)
    w, h = box.width, box.height
    distances = [_distance(p, background) for p in pixels]
    peak = max(distances)
    if peak < INK_CONTRAST_MIN:
        size = max(1, round(h / HANGUL_INK_RATIO))
        return MeasuredStyle(
            Weight.REGULAR, Face.SANS, size, _contrasting(background), Align.CENTER, 0.0, 0, False
        )
    core = [p for p, d in zip(pixels, distances, strict=True) if 4 * d >= 3 * peak]
    r, g, b = _median(core)[:3]
    coverage = [min(1.0, d / peak) for d in distances]
    mask = [c >= 0.5 for c in coverage]
    edges = sum(mask[i] != mask[i + 1] for i in range(len(mask) - 1) if (i + 1) % w)
    edges += sum(mask[i] != mask[i + w] for i in range(len(mask) - w))
    stroke = 2 * sum(coverage) / max(1, edges)
    rows = [any(mask[y * w : (y + 1) * w]) for y in range(h)]
    bands = _bands(rows, max(1, h // 16))
    line_height = max(bands)
    ratio = round(stroke / line_height, 4)
    weight = (
        Weight.BLACK
        if ratio >= BLACK_FROM
        else Weight.BOLD
        if ratio >= BOLD_FROM
        else Weight.REGULAR
    )
    columns = [any(mask[y * w + x] for y in range(h)) for x in range(w)]
    return MeasuredStyle(
        weight=weight,
        face=Face.SANS,
        size=max(1, round(line_height / HANGUL_INK_RATIO)),
        colour=(r, g, b),
        align=_align(columns),
        stroke_ratio=ratio,
        line_count=len(bands),
        ink_found=True,
    )
