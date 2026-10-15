"""The local OCR port's engine side (ADR-0041 D1, D2, §3, §4; slice I2).

The job reads a source image through ``contracts.OcrProvider``. Production binds the pinned
RapidOCR engine (I7, ``rapid``) over this provider when it is installed, and otherwise
``contracts.NoOcrProvider``, so the capability ``image_ocr`` reads ``NOT_CONFIGURED``
(``ocr_capability``), never a core failure, and the job refuses before anything is written.
``EngineOcrProvider`` is the provider an engine plugs into: the engine only recognises lines in
one prepared tile (``OcrEngine.recognise``); everything that makes a reading an observation is
here, deterministic and pure where it can be:

1. **Preprocessing** (``PREPROCESSING_VERSION``, part of the observation key): the source is decoded
   by Pillow with no EXIF orientation applied, an image with alpha is composited over white, and the
   result is converted to RGB. It is never resized: the only geometric step is tiling.
2. **Tiling** (``tile_spans``): an image taller than ``TILE_HEIGHT`` is read in full-width tiles of
   that height, each starting ``TILE_HEIGHT - TILE_OVERLAP`` below the previous one, the last one
   ending at the image's bottom (it may be shorter). ``2 * TILE_OVERLAP <= TILE_HEIGHT``, so only
   neighbouring tiles share rows. A line no taller than the overlap lies whole in some tile.
3. **Placement** (``place_line``): the engine's box or polygon, in the tile's pixel grid, is moved
   to the image's grid, rounded outwards to whole pixels and clamped to the tile. A line touching a
   tile edge that another tile continues past is *cut*.
4. **Seam merge** (``merge_seams``): two lines of neighbouring tiles whose boxes share at least
   ``SEAM_MATCH`` of the smaller box are one line. The whole one wins over a cut one, then the
   higher confidence, then the upper tile; when both are cut (a line taller than the overlap) the
   kept line's box grows to their union and its polygon is dropped. The kept line's text is the
   engine's, never a concatenation.
5. **Script** (``detect_script``): a line with kana is ``ja``, else one with Han ideographs is
   ``zh``; any other line is not translatable and is skipped with its reason (``SKIP_REASONS``).
6. **Scores**: the confidence is the engine's, clamped to 0..1 (``clamp_confidence``); the
   background-uniformity score is not the engine's but I3's ``read_background`` on the source as
   the renderer decodes it (``renderer.decode_source``), so the observation carries the very score
   the renderer erases by (§4).

Regions come out in reading order (top, then left) with ids ``r0, r1, …``. A skipped line keeps
only its box and its reason: no OCR text is ever logged or audited (§8).

A change of any constant here bumps ``PREPROCESSING_VERSION``; the engine's own version and its
pinned models (SHA-256) are its identity's (``contracts.OcrIdentity``).
"""

import io
import logging
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol

from PIL import Image

from app.capabilities.image_studio.contracts import (
    IMAGE_OCR_NOT_CONFIGURED,
    OcrIdentity,
    OcrProvider,
    OcrRegion,
    checked_regions,
)
from app.capabilities.image_studio.renderer import decode_source
from app.capabilities.image_studio.style import Box, read_background
from app.platform.core.errors import InputValidationError, PolicyBlockedError
from app.stages.connect.contracts import CapabilityReport
from app.stages.connect.state import CapabilityStatus

logger = logging.getLogger("icbm.image_studio")

PREPROCESSING_VERSION: Final = "ocr-preprocess/v1"
# Tiles: full width, this tall, each overlapping the previous one by this many rows.
TILE_HEIGHT: Final = 2048
TILE_OVERLAP: Final = 256
# A line within this many pixels of a tile edge that another tile continues past is cut there.
EDGE_MARGIN: Final = 2
# Two lines of neighbouring tiles are one line when they share this much of the smaller box.
SEAM_MATCH: Final = 0.5
CONFIDENCE_DECIMALS: Final = 4
# Alpha is composited over this colour before the conversion to RGB.
MATTE: Final = (255, 255, 255)

IMAGE_OCR_CAPABILITY_KEY: Final = "image_ocr"
NO_OCR_ENGINE: Final = "no OCR engine is installed (ADR-0041 D2)"
IMAGE_OCR_SOURCE_UNREADABLE: Final = "IMAGE_OCR_SOURCE_UNREADABLE"

# Why a recognised line is not a region of the observation.
SKIP_LINE_INVALID: Final = "LINE_INVALID"  # no usable box or polygon, or text that is not text
SKIP_BOX_EMPTY: Final = "BOX_EMPTY"  # nothing left of the box inside its tile
SKIP_TEXT_EMPTY: Final = "TEXT_EMPTY"
SKIP_SCRIPT_KOREAN: Final = "SCRIPT_KOREAN"  # already Korean: nothing to translate
SKIP_SCRIPT_NOT_CJK: Final = "SCRIPT_NOT_CJK"  # Latin, digits or any other script only
SKIP_REASONS: Final = (
    SKIP_LINE_INVALID,
    SKIP_BOX_EMPTY,
    SKIP_TEXT_EMPTY,
    SKIP_SCRIPT_KOREAN,
    SKIP_SCRIPT_NOT_CJK,
)

PixelBox = tuple[int, int, int, int]
Polygon = tuple[tuple[int, int], ...]


# ---------------------------------------------------------------- preprocessing and tiling


@dataclass(frozen=True)
class Preprocessing:
    """The pinned preprocessing: its version names every step and constant below."""

    version: str
    tile_height: int
    tile_overlap: int

    def __post_init__(self) -> None:
        if not self.version or self.tile_height < 1 or self.tile_overlap < 0:
            raise ValueError("a preprocessing has a version, a tile height and an overlap")
        if 2 * self.tile_overlap > self.tile_height:
            raise ValueError("an overlap is at most half a tile, so only neighbours share rows")


PREPROCESSING: Final = Preprocessing(PREPROCESSING_VERSION, TILE_HEIGHT, TILE_OVERLAP)


def tile_spans(
    height: int, tile_height: int = TILE_HEIGHT, overlap: int = TILE_OVERLAP
) -> tuple[tuple[int, int], ...]:
    """The rows ``(top, bottom)`` (bottom exclusive) of each tile of an image ``height`` tall."""
    if height < 1:
        raise ValueError("an image has at least one row")
    Preprocessing("tiles", tile_height, overlap)  # the same bounds as a pinned preprocessing
    step = tile_height - overlap
    spans: list[tuple[int, int]] = []
    top = 0
    while True:
        bottom = min(top + tile_height, height)
        spans.append((top, bottom))
        if bottom == height:
            return tuple(spans)
        top += step


def preprocess(source: bytes) -> Image.Image:
    """Decode with no EXIF orientation, composite alpha over white, convert to RGB; no resize."""
    try:
        image = Image.open(io.BytesIO(source))
        image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise InputValidationError(
            IMAGE_OCR_SOURCE_UNREADABLE, "the source is not a readable image"
        ) from exc
    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        matte = Image.new("RGBA", rgba.size, (*MATTE, 255))
        return Image.alpha_composite(matte, rgba).convert("RGB")
    return image.convert("RGB")


# ---------------------------------------------------------------- script and confidence

# Kana, the half-width katakana and the Japanese iteration marks. The katakana middle dot (U+30FB)
# and the prolonged sound mark (U+30FC) are left out: they appear in Chinese text too.
_KANA: Final = (
    (0x3040, 0x309F),
    (0x30A0, 0x30FA),
    (0x30FD, 0x30FF),
    (0x31F0, 0x31FF),
    (0xFF66, 0xFF6F),
    (0xFF71, 0xFF9D),
    (0x3005, 0x3006),
    (0x1B000, 0x1B16F),
)
_HAN: Final = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
    (0x30000, 0x323AF),
)
_HANGUL: Final = (
    (0x1100, 0x11FF),
    (0x3130, 0x318F),
    (0xA960, 0xA97F),
    (0xAC00, 0xD7FF),
    (0xFFA0, 0xFFDC),
)


def _any_in(text: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(lo <= ord(c) <= hi for c in text for lo, hi in ranges)


@dataclass(frozen=True)
class ScriptReading:
    """``script`` is ``zh`` or ``ja``; otherwise it is ``None`` and ``skipped`` says why."""

    script: str | None
    skipped: str | None = None


def detect_script(text: str) -> ScriptReading:
    """``ja`` when the text holds kana, else ``zh`` when it holds Han ideographs. Any other text
    is not translatable: empty, already Korean, or no CJK at all (Latin, digits, other scripts)."""
    if not text.strip():
        return ScriptReading(None, SKIP_TEXT_EMPTY)
    if _any_in(text, _KANA):
        return ScriptReading("ja")
    if _any_in(text, _HAN):
        return ScriptReading("zh")
    if _any_in(text, _HANGUL):
        return ScriptReading(None, SKIP_SCRIPT_KOREAN)
    return ScriptReading(None, SKIP_SCRIPT_NOT_CJK)


def clamp_confidence(value: object) -> float:
    """The engine's confidence in 0..1, rounded; anything that is not a finite number reads 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    number = float(value)
    if not math.isfinite(number):
        return 0.0
    return round(min(1.0, max(0.0, number)), CONFIDENCE_DECIMALS)


# ---------------------------------------------------------------- the engine


@dataclass(frozen=True)
class EngineIdentity:
    """The engine and its pinned models (SHA-256). The preprocessing is the provider's."""

    engine_id: str
    engine_version: str
    model_sha256s: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineLine:
    """One line the engine recognised in one tile: its box ``(left, top, right, bottom)`` or its
    polygon, in that tile's pixel grid, its text and its raw confidence."""

    text: str
    confidence: float
    box: tuple[float, float, float, float] | None = None
    polygon: tuple[tuple[float, float], ...] | None = None


class OcrEngine(Protocol):
    """A local OCR engine (D2: RapidOCR, I7). It reads one prepared RGB tile and sends nothing."""

    def identity(self) -> EngineIdentity | None: ...

    def recognise(self, tile: Image.Image) -> Sequence[EngineLine]: ...


# ---------------------------------------------------------------- placement and seam merge


@dataclass(frozen=True)
class PlacedLine:
    """A recognised line in the image's pixel grid: the tile it came from and whether it is cut by
    that tile's inner edge."""

    tile: int
    box: PixelBox
    text: str
    confidence: float
    cut: bool = False
    polygon: Polygon | None = None

    @property
    def area(self) -> int:
        return (self.box[2] - self.box[0]) * (self.box[3] - self.box[1])


def _whole(value: float, lo: int, hi: int, up: bool) -> int:
    return min(hi, max(lo, math.ceil(value) if up else math.floor(value)))


def place_line(
    line: EngineLine, tile: int, span: tuple[int, int], width: int, tiles: int
) -> PlacedLine | str:
    """The line in the image's grid, or the reason it is skipped (``LINE_INVALID``,
    ``BOX_EMPTY``)."""
    top, bottom = span
    height = bottom - top
    points = line.polygon if line.polygon is not None and len(line.polygon) >= 3 else None
    if line.box is not None and len(line.box) == 4:
        corners: tuple[float, ...] = tuple(line.box)
    elif points is not None and all(len(p) == 2 for p in points):
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        corners = (min(xs), min(ys), max(xs), max(ys))
    else:
        return SKIP_LINE_INVALID
    numbers = [*corners, *(v for p in points or () for v in p)]
    if not isinstance(line.text, str) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) for v in numbers
    ):
        return SKIP_LINE_INVALID
    left, upper, right, lower = corners
    if left > right or upper > lower:
        return SKIP_LINE_INVALID
    box = (
        _whole(left, 0, width, up=False),
        _whole(upper, 0, height, up=False) + top,
        _whole(right, 0, width, up=True),
        _whole(lower, 0, height, up=True) + top,
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return SKIP_BOX_EMPTY
    polygon = (
        None
        if points is None
        else tuple(
            (
                min(width, max(0, math.floor(x + 0.5))),
                min(height, max(0, math.floor(y + 0.5))) + top,
            )
            for x, y in points
        )
    )
    cut = (tile > 0 and box[1] - top <= EDGE_MARGIN) or (
        tile < tiles - 1 and bottom - box[3] <= EDGE_MARGIN
    )
    return PlacedLine(tile, box, line.text, clamp_confidence(line.confidence), cut, polygon)


def _shared(a: PixelBox, b: PixelBox) -> int:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, width) * max(0, height)


def same_line(a: PlacedLine, b: PlacedLine) -> bool:
    """Lines of neighbouring tiles that share at least ``SEAM_MATCH`` of the smaller box."""
    if abs(a.tile - b.tile) != 1:
        return False
    return _shared(a.box, b.box) >= SEAM_MATCH * min(a.area, b.area)


def _reading_order(line: PlacedLine) -> tuple[int, int, int, int, str]:
    return (line.box[1], line.box[0], line.box[3], line.box[2], line.text)


def merge_seams(lines: Sequence[PlacedLine]) -> tuple[PlacedLine, ...]:
    """One line per line across tile seams, in reading order (see the module's step 4)."""
    priority = sorted(
        lines, key=lambda line: (line.cut, -line.confidence, line.tile, _reading_order(line))
    )
    kept: list[PlacedLine] = []
    for line in priority:
        at = next((i for i, k in enumerate(kept) if same_line(k, line)), None)
        if at is None:
            kept.append(line)
            continue
        winner = kept[at]
        if winner.cut and line.cut:
            a, b = winner.box, line.box
            union = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
            kept[at] = replace(winner, box=union, polygon=None)
    return tuple(sorted(kept, key=_reading_order))


# ---------------------------------------------------------------- the provider


@dataclass(frozen=True)
class SkippedLine:
    """A recognised line left out of the observation: its box, if it had one, and why."""

    box: PixelBox | None
    reason: str


@dataclass(frozen=True)
class OcrReading:
    identity: OcrIdentity
    regions: tuple[OcrRegion, ...]
    skipped: tuple[SkippedLine, ...]
    tiles: tuple[tuple[int, int], ...]


def as_box(box: PixelBox) -> Box:
    """A ``(left, top, right, bottom)`` box as the renderer's ``Box``."""
    left, top, right, bottom = box
    return Box(left, top, right - left, bottom - top)


class EngineOcrProvider:
    """The ``OcrProvider`` over a local engine: preprocessing, tiling, seam merge, script and
    scores are here, so every engine yields observations of one shape (I7 plugs in)."""

    def __init__(self, engine: OcrEngine, preprocessing: Preprocessing = PREPROCESSING) -> None:
        self._engine = engine
        self._preprocessing = preprocessing

    def identity(self) -> OcrIdentity | None:
        engine = self._engine.identity()
        if engine is None:
            return None
        return OcrIdentity(
            engine.engine_id,
            engine.engine_version,
            self._preprocessing.version,
            engine.model_sha256s,
        )

    def read(self, image: bytes) -> tuple[OcrRegion, ...]:
        return self.observe(image).regions

    def observe(self, source: bytes) -> OcrReading:
        """The whole reading: the regions, the lines skipped with their reasons and the tiles."""
        identity = self.identity()
        if identity is None:
            raise PolicyBlockedError(IMAGE_OCR_NOT_CONFIGURED, "no OCR engine is installed")
        prepared = preprocess(source)
        spans = tile_spans(
            prepared.height, self._preprocessing.tile_height, self._preprocessing.tile_overlap
        )
        placed: list[PlacedLine] = []
        skipped: list[SkippedLine] = []
        for tile, span in enumerate(spans):
            crop = prepared.crop((0, span[0], prepared.width, span[1]))
            for recognised in self._engine.recognise(crop):
                result = place_line(recognised, tile, span, prepared.width, len(spans))
                if isinstance(result, str):
                    skipped.append(SkippedLine(None, result))
                else:
                    placed.append(result)
        scored = decode_source(source)
        if scored.size != prepared.size:  # pragma: no cover - one decoder, one pixel grid
            raise RuntimeError("the renderer and the preprocessing disagree on the pixel grid")
        regions: list[OcrRegion] = []
        for line in merge_seams(placed):
            text = line.text.strip()
            script = detect_script(text)
            if script.script is None:
                skipped.append(SkippedLine(line.box, script.skipped or SKIP_SCRIPT_NOT_CJK))
                continue
            regions.append(
                OcrRegion(
                    region_id=f"r{len(regions)}",
                    box=line.box,
                    text=text,
                    script=script.script,
                    confidence=line.confidence,
                    background_uniformity=read_background(scored, as_box(line.box)).score,
                    polygon=line.polygon,
                )
            )
        logger.info(
            "image_ocr.read",
            extra={
                "engine_id": identity.engine_id,
                "tiles": len(spans),
                "regions": len(regions),
                "skipped": dict(sorted(Counter(s.reason for s in skipped).items())),
            },
        )
        return OcrReading(identity, checked_regions(regions), tuple(skipped), spans)


def ocr_capability(provider: OcrProvider) -> CapabilityReport:
    """The ``image_ocr`` capability for readiness: ``NOT_CONFIGURED`` with no engine installed,
    never a core failure (ADR-0041 D2, §7)."""
    try:
        identity = provider.identity()
    except Exception as error:  # an engine that cannot name itself degrades, never fails core
        return CapabilityReport(
            key=IMAGE_OCR_CAPABILITY_KEY,
            status=CapabilityStatus.DEGRADED,
            detail=f"the OCR engine did not name itself ({type(error).__name__})",
        )
    if identity is None:
        return CapabilityReport(
            key=IMAGE_OCR_CAPABILITY_KEY,
            status=CapabilityStatus.NOT_CONFIGURED,
            detail=NO_OCR_ENGINE,
        )
    return CapabilityReport(
        key=IMAGE_OCR_CAPABILITY_KEY,
        status=CapabilityStatus.READY,
        detail=(
            f"engine={identity.engine_id} version={identity.key_engine_version}"
            f" preprocessing={identity.preprocessing_version}"
        ),
    )
