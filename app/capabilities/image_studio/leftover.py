"""The leftover-text QA of 「적용」 (ADR-0041 §6, I6; IT-04, IT-05).

Once a rendering is complete, 「적용」 reads the rendered PNG again with the OCR engine bound now,
through the same port the run read the source with (``ImageTranslationService.read_rendered``).
From that reading on, everything here is pure and deterministic:

- **Source script only.** A line of the reading counts when its text still holds Chinese or
  Japanese (``ocr.detect_script``: kana, else Han ideographs). The Korean just drawn reads as
  Hangul, which the port already leaves out (``SCRIPT_KOREAN``); in a mixed line only its Han or
  kana make it count.
- **Whose leftover it is.** Inside an included region's box every pixel was erased or covered by
  a label (§4), so what remains of its source text lies on or just past the box's edge: a box the
  engine read too tight, a glyph that sticks out. A counted line belongs to one region of the
  observation, included or kept:
  1. the region whose own box holds the largest share of the line, when that share is at least
     ``LEFTOVER_OVERLAP`` of the line's area;
  2. otherwise, among the regions whose box grown on every side by ``LEFTOVER_REACH`` times its
     shorter side (one line height for horizontal text, one column width for vertical text) holds
     at least ``LEFTOVER_OVERLAP`` of the line, the one nearest to the line (the gap between the
     boxes); on a tie the one whose grown box holds more of it, then the earlier in reading order.
- **What it flags.** A line that belongs to an included region flags that region
  ``LEFTOVER_SOURCE_TEXT``: it needs its own confirmation (IT-05). A line that belongs to a kept
  region is that region's own untouched text, as the operator chose. A line near no region is
  counted (``unattributed``) and flags nothing: no pixel away from an included box changed
  (IT-04), so it is text the source reading did not hold as a region.

The result names region ids and counts only, never a text (§8).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.capabilities.image_studio.contracts import OcrRegion
from app.capabilities.image_studio.ocr import detect_script

LEFTOVER_SOURCE_TEXT: Final = "LEFTOVER_SOURCE_TEXT"
# A line belongs to a region when the region's box (or its grown box) holds this share of it.
LEFTOVER_OVERLAP: Final = 0.5
# How far, in the region box's shorter side, a region's box is grown to catch text just past it.
LEFTOVER_REACH: Final = 1.0

Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class LeftoverReading:
    """The included regions with source text left, in reading order, and how many source-script
    lines the reading held, of them how many near no region."""

    regions: tuple[str, ...]
    lines: int
    unattributed: int


def _area(box: Box) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _shared(a: Box, b: Box) -> int:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, width) * max(0, height)


def grown(box: Box) -> Box:
    """The box grown on every side by ``LEFTOVER_REACH`` times its shorter side."""
    left, top, right, bottom = box
    reach = round(LEFTOVER_REACH * min(right - left, bottom - top))
    return (left - reach, top - reach, right + reach, bottom + reach)


def _gap(a: Box, b: Box) -> int:
    """The larger of the horizontal and vertical gaps between two boxes; 0 when they touch."""
    return max(0, max(a[0], b[0]) - min(a[2], b[2]), max(a[1], b[1]) - min(a[3], b[3]))


def owner(line: Box, regions: Sequence[OcrRegion]) -> str | None:
    """The id of the region a re-read line belongs to, or ``None`` when it is near none."""
    area = _area(line)
    boxes = [(order, r) for order, r in enumerate(regions) if _area(r.box) > 0]
    if area == 0 or not boxes:
        return None
    inside = [
        (_shared(line, r.box) / area, -order, r.region_id)
        for order, r in boxes
        if _shared(line, r.box) >= LEFTOVER_OVERLAP * area
    ]
    if inside:
        return max(inside)[2]
    near = [
        (-_gap(line, r.box), _shared(line, grown(r.box)) / area, -order, r.region_id)
        for order, r in boxes
        if _shared(line, grown(r.box)) >= LEFTOVER_OVERLAP * area
    ]
    return max(near)[3] if near else None


def find_leftover(
    regions: Sequence[OcrRegion], included: frozenset[str], reread: Sequence[OcrRegion]
) -> LeftoverReading:
    """The included regions of the observation whose source text the rendered image still holds,
    from the engine's reading of that image."""
    counted = [line for line in reread if detect_script(line.text).script is not None]
    flagged: set[str] = set()
    unattributed = 0
    for line in counted:
        found = owner(line.box, regions)
        if found is None:
            unattributed += 1
        elif found in included:
            flagged.add(found)
    return LeftoverReading(
        regions=tuple(r.region_id for r in regions if r.region_id in flagged),
        lines=len(counted),
        unattributed=unattributed,
    )
