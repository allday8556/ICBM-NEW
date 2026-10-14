"""ADR-0041 I3: the deterministic image-translation renderer (§4, §5, §6; IT-03, IT-04).

Every image here is synthetic, drawn with Pillow and the bundled Noto Sans KR: a flat background
whose text may be erased, a noisy gradient that must never be repainted, bold and regular text for
the style reading. No OCR engine, provider or network is involved.
"""

import hashlib
import io
import random

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.capabilities.detail_guidance.renderer import FONT_FILE, FONT_SHA256
from app.capabilities.image_studio.renderer import (
    COMPLEX_BACKGROUND,
    FACE_FALLBACK_SANS,
    IMAGE_TRANSLATE_INPUT_INVALID,
    LAYOUT_OVERFLOW,
    MIN_FONT_SIZE,
    MISSING_GLYPHS,
    OVERDRAWN,
    OVERLAPS_KEPT_REGION,
    RENDERER_VERSION,
    CompletenessFailure,
    Placement,
    RegionStatus,
    RenderRegion,
    StyleOverride,
    render,
)
from app.capabilities.image_studio.style import (
    Box,
    Face,
    Weight,
    measure_style,
    read_background,
)
from app.platform.core.errors import InputValidationError

WHITE = (255, 255, 255)
INK = (34, 34, 34)
# The decoded pixels of the flat scenario (``_flat_scene`` + ``_FLAT_REGION``), pinned for
# image-translate-renderer/v1 with the bundled font and the pinned Pillow (constraints.txt).
FLAT_PIXELS = "d76640df0092aa8707895c0901a100dabf3d24ee1f9579ab306eb7a119b9d8e7"


def _font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(
        io.BytesIO(FONT_FILE.read_bytes()), size, layout_engine=ImageFont.Layout.BASIC
    )
    font.set_variation_by_axes([weight])
    return font


def _text_box(
    image: Image.Image,
    at: tuple[int, int],
    text: str,
    *,
    size: int = 32,
    weight: int = 400,
    fill: tuple[int, int, int] = INK,
    margin: int = 4,
) -> Box:
    """Draw ``text`` and return its ink box grown by ``margin``, as an OCR engine would report."""
    draw = ImageDraw.Draw(image)
    font = _font(size, weight)
    draw.text(at, text, font=font, fill=fill)
    left, top, right, bottom = (int(v) for v in draw.textbbox(at, text, font=font))
    return Box(left - margin, top - margin, right - left + 2 * margin, bottom - top + 2 * margin)


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _decoded(png: bytes, mode: str = "RGB") -> Image.Image:
    return Image.open(io.BytesIO(png)).convert(mode)


def _outside(image: Image.Image, boxes: list[Box]) -> bytes:
    """The image's bytes with every box blanked: equal bytes mean equal pixels outside the boxes."""
    copy = image.copy()
    draw = ImageDraw.Draw(copy)
    for box in boxes:
        draw.rectangle((box.x, box.y, box.right - 1, box.bottom - 1), fill=(0,) * len(copy.mode))
    return copy.tobytes()


def _flat_scene() -> tuple[Image.Image, Box]:
    image = Image.new("RGB", (420, 160), WHITE)
    ImageDraw.Draw(image).rectangle((0, 120, 419, 159), fill=(240, 200, 40))
    return image, _text_box(image, (40, 40), "中文字体", size=36)


def _noisy_scene() -> tuple[Image.Image, Box]:
    """A photo-like background: a diagonal gradient with seeded noise, text drawn over it."""
    rng = random.Random(41)
    image = Image.new("RGB", (420, 160))
    image.putdata(
        [
            tuple(
                max(0, min(255, base + rng.randint(-30, 30)))
                for base in ((x * 255) // 420, (y * 255) // 160, 128)
            )
            for y in range(160)
            for x in range(420)
        ]
    )
    return image, _text_box(image, (40, 40), "新品上市", size=36, fill=(250, 250, 250))


def _region(box: Box, korean: str = "한국어 글꼴", **fields: object) -> RenderRegion:
    values: dict[str, object] = {
        "region_id": "r1",
        "box": box,
        "source_text": "中文字体",
        "korean_text": korean,
        "include": True,
        "erase": True,
        "override": None,
    }
    values.update(fields)
    return RenderRegion(**values)  # type: ignore[arg-type]


_FLAT_REGION = "한국어 글꼴"


def test_a_flat_background_is_erased_and_the_korean_text_drawn_in_the_matched_style() -> None:
    source, box = _flat_scene()
    result = render(_png(source), [_region(box, _FLAT_REGION)])
    (outcome,) = result.regions
    assert outcome.status is RegionStatus.RENDERED
    assert outcome.erased and outcome.placement is Placement.INLINE
    assert outcome.background.clean and outcome.background.colour == WHITE
    assert outcome.flags == ()
    assert outcome.style is not None and outcome.style.colour == INK
    assert outcome.style.weight is Weight.REGULAR and outcome.face_matched is Face.SANS
    assert outcome.lines == (_FLAT_REGION,)
    assert result.completeness.complete and result.completeness.failures == ()
    out = _decoded(result.png)
    assert _outside(out, [box]) == _outside(source, [box])
    drawn = out.crop(box.area)
    assert drawn.tobytes() != source.crop(box.area).tobytes()
    # The source glyphs are gone: inside the box every pixel is the fill, the text colour or the
    # anti-aliased blend of the two (never a third colour).
    colours = set(drawn.get_flattened_data())
    assert WHITE in colours and INK in colours
    assert all(min(p) >= 34 and max(p) - min(p) <= 2 for p in colours)


def test_the_pixels_are_pinned_and_the_same_input_gives_the_same_bytes() -> None:
    source, box = _flat_scene()
    first = render(_png(source), [_region(box, _FLAT_REGION)])
    again = render(_png(source), [_region(box, _FLAT_REGION)])
    assert first.png == again.png and first.sha256 == again.sha256
    assert first.sha256 == hashlib.sha256(first.png).hexdigest()
    assert (first.renderer_version, first.font_sha256s) == (RENDERER_VERSION, (FONT_SHA256,))
    assert RENDERER_VERSION == "image-translate-renderer/v1"
    image = Image.open(io.BytesIO(first.png))
    assert image.format == "PNG" and image.size == (source.width, source.height)
    assert "tIME" not in image.info and not image.info.get("dpi")
    assert hashlib.sha256(image.tobytes()).hexdigest() == FLAT_PIXELS
    other = render(_png(source), [_region(box, "다른 문구")])
    assert other.sha256 != first.sha256


def test_a_complex_background_is_never_repainted_and_an_included_region_gets_a_label() -> None:
    source, box = _noisy_scene()
    reading = read_background(source, box)
    assert not reading.clean and reading.ring_uniformity < 0.95
    result = render(_png(source), [_region(box, "신상품 출시")])
    (outcome,) = result.regions
    assert outcome.status is RegionStatus.NOT_ERASED_COMPLEX_BACKGROUND
    assert COMPLEX_BACKGROUND in outcome.flags
    assert not outcome.erased and outcome.placement is Placement.LABEL
    # It is still drawn, so the region is complete; the flag asks for the operator's own
    # confirmation (§6, IT-05) in a later slice.
    assert result.completeness.complete
    out = _decoded(result.png)
    assert _outside(out, [box]) == _outside(source, [box])
    # The light source text is read as light, so the label is dark and solid.
    label = out.getpixel((box.x, box.y))
    assert label == (17, 17, 17)


def test_a_kept_region_on_any_background_is_untouched() -> None:
    for source, box in (_flat_scene(), _noisy_scene()):
        result = render(_png(source), [_region(box, include=False)])
        (outcome,) = result.regions
        assert outcome.status is RegionStatus.KEPT and outcome.style is None
        assert _decoded(result.png).tobytes() == source.tobytes()
        assert result.completeness.complete


def test_a_region_kept_beside_a_rendered_one_and_an_overlap_fails_completeness() -> None:
    source = Image.new("RGB", (480, 200), WHITE)
    first = _text_box(source, (20, 20), "中文字体", size=32)
    second = _text_box(source, (20, 110), "送料無料", size=32)
    keep = RenderRegion("r2", second, "送料無料", "", include=False, erase=False)
    result = render(_png(source), [_region(first), keep])
    assert [o.status for o in result.regions] == [RegionStatus.RENDERED, RegionStatus.KEPT]
    out = _decoded(result.png)
    assert out.crop(second.area).tobytes() == source.crop(second.area).tobytes()
    assert _outside(out, [first]) == _outside(source, [first])
    assert result.completeness.complete

    overlapping = Box(first.x + 10, first.y + 5, first.width, first.height)
    kept = RenderRegion("r2", overlapping, "中文", "", include=False, erase=False)
    clash = render(_png(source), [_region(first), kept])
    out = _decoded(clash.png)
    assert out.crop(overlapping.area).tobytes() == source.crop(overlapping.area).tobytes()
    assert _outside(out, [first]) == _outside(source, [first])
    assert not clash.completeness.complete
    assert clash.completeness.failures == (CompletenessFailure("r1", OVERLAPS_KEPT_REGION),)


def test_a_later_region_drawn_over_an_earlier_regions_text_fails_completeness() -> None:
    source, box = _flat_scene()
    earlier = _region(box, "한국어 글꼴")
    beside = Box(box.right, box.y, 40, box.height)
    over = Box(box.x + box.width // 2, box.y, box.width // 2, box.height)
    apart = render(_png(source), [earlier, _region(beside, "가", region_id="r2")])
    assert apart.completeness.complete
    clash = render(_png(source), [earlier, _region(over, "가", region_id="r2")])
    assert [o.status for o in clash.regions] == [RegionStatus.RENDERED] * 2
    assert clash.completeness.failures == (CompletenessFailure("r1", OVERDRAWN),)


def test_the_text_shrinks_and_wraps_to_fit_and_never_leaves_its_box() -> None:
    source, box = _flat_scene()
    long = "고품질 정품 보장 무료 배송 당일 발송"
    result = render(_png(source), [_region(box, long)])
    (outcome,) = result.regions
    assert outcome.status is RegionStatus.RENDERED
    assert outcome.style is not None and outcome.measured.size > outcome.style.size
    assert outcome.style.size >= MIN_FONT_SIZE
    assert " ".join(outcome.lines) == long
    assert outcome.ink is not None
    left, top, right, bottom = outcome.ink
    assert left >= 0 and top >= 0 and right <= box.width and bottom <= box.height
    out = _decoded(result.png)
    assert _outside(out, [box]) == _outside(source, [box])
    # A box that is two lines high wraps instead of shrinking to one line.
    tall = Box(box.x, box.y, box.width, box.height * 2 + 20)
    wrapped = render(_png(source), [_region(tall, long)]).regions[0]
    assert wrapped.style is not None and len(wrapped.lines) >= 2
    assert wrapped.style.size > outcome.style.size


def test_text_that_cannot_fit_at_the_floor_is_layout_overflow_and_never_cut() -> None:
    source, box = _flat_scene()
    result = render(_png(source), [_region(box, "아주 긴 번역 문구 " * 30)])
    (outcome,) = result.regions
    assert outcome.status is RegionStatus.OVERFLOW
    assert LAYOUT_OVERFLOW in outcome.flags and outcome.lines == ()
    # Nothing is erased or drawn: the image is the source.
    assert _decoded(result.png).tobytes() == source.tobytes()
    assert not result.completeness.complete
    assert result.completeness.failures == (CompletenessFailure("r1", LAYOUT_OVERFLOW),)
    # An operator's size is exact: it is not shrunk, so a size that does not fit overflows.
    exact = render(_png(source), [_region(box, override=StyleOverride(size=60))])
    assert exact.regions[0].status is RegionStatus.OVERFLOW


def test_a_glyph_the_font_lacks_fails_completeness_and_the_region_stays_untouched() -> None:
    source, box = _flat_scene()
    result = render(_png(source), [_region(box, "무료 🚚 배송")])
    (outcome,) = result.regions
    assert outcome.status is RegionStatus.MISSING_GLYPHS
    assert outcome.missing_glyphs == ("🚚",) and MISSING_GLYPHS in outcome.flags
    assert _decoded(result.png).tobytes() == source.tobytes()
    assert result.completeness.failures == (CompletenessFailure("r1", MISSING_GLYPHS),)


@pytest.mark.parametrize(
    ("weight", "expected"),
    [(400, Weight.REGULAR), (700, Weight.BOLD), (900, Weight.BLACK)],
)
def test_the_weight_and_the_colour_are_read_from_the_glyph_pixels(
    weight: int, expected: Weight
) -> None:
    colour = (180, 30, 40)
    image = Image.new("RGB", (420, 140), (250, 248, 240))
    box = _text_box(image, (30, 30), "中文字体", size=48, weight=weight, fill=colour)
    background = read_background(image, box)
    assert background.clean and background.colour == (250, 248, 240)
    measured = measure_style(image, box, background.colour)
    assert measured.weight is expected
    assert measured.face is Face.SANS and measured.ink_found and measured.line_count == 1
    assert max(abs(a - b) for a, b in zip(measured.colour, colour, strict=True)) <= 6
    # The ink of a 48 px line is about 46 px high; the matched Korean size follows it.
    assert 44 <= measured.size <= 54


def test_the_operator_override_wins_and_a_serif_request_falls_back_to_sans() -> None:
    source, box = _flat_scene()
    override = StyleOverride(weight=Weight.BLACK, face=Face.SERIF, size=20, colour=(0, 90, 200))
    result = render(_png(source), [_region(box, "무료 배송", override=override)])
    (outcome,) = result.regions
    assert outcome.status is RegionStatus.RENDERED
    assert outcome.style is not None
    assert (outcome.style.weight, outcome.style.size, outcome.style.colour) == (
        Weight.BLACK,
        20,
        (0, 90, 200),
    )
    assert outcome.style.face is Face.SANS and outcome.face_matched is Face.SANS
    assert FACE_FALLBACK_SANS in outcome.flags
    assert any("serif" in note for note in outcome.notes)
    assert (0, 90, 200) in set(_decoded(result.png).crop(box.area).get_flattened_data())


def test_without_an_erase_request_the_text_goes_on_a_label_even_on_a_clean_background() -> None:
    source, box = _flat_scene()
    result = render(_png(source), [_region(box, erase=False)])
    (outcome,) = result.regions
    assert outcome.status is RegionStatus.RENDERED
    assert not outcome.erased and outcome.placement is Placement.LABEL
    assert _decoded(result.png).getpixel((box.x, box.y)) == WHITE


def test_transparency_outside_the_boxes_is_kept() -> None:
    source = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    box = _text_box(source, (20, 30), "中文字体", size=32, fill=(10, 10, 10))
    result = render(_png(source), [_region(box, "한국어")])
    out = _decoded(result.png, "RGBA")
    assert out.mode == "RGBA"
    assert _outside(out, [box]) == _outside(source, [box])
    assert result.regions[0].erased and result.completeness.complete


@pytest.mark.parametrize(
    "regions",
    [
        [_region(Box(400, 100, 60, 40))],
        [_region(Box(0, 0, 0, 10))],
        [_region(Box(10, 10, 50, 30)), _region(Box(100, 10, 50, 30))],
        [_region(Box(10, 10, 50, 30), "  ")],
        [_region(Box(10, 10, 50, 30), override=StyleOverride(size=MIN_FONT_SIZE - 1))],
    ],
)
def test_an_invalid_region_is_refused(regions: list[RenderRegion]) -> None:
    source, _ = _flat_scene()
    with pytest.raises(InputValidationError) as caught:
        render(_png(source), regions)
    assert caught.value.code == IMAGE_TRANSLATE_INPUT_INVALID


def test_a_source_that_is_not_an_image_is_refused() -> None:
    with pytest.raises(InputValidationError) as caught:
        render(b"not an image", [])
    assert caught.value.code == IMAGE_TRANSLATE_INPUT_INVALID
