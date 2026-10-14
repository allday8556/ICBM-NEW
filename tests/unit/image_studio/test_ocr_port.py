"""ADR-0041 I2: the pure helpers of the OCR port — tiling, placement, seam merge, script detection,
confidence clamping and the observation identity."""

import random
from itertools import pairwise

import pytest

from app.capabilities.image_studio.contracts import OcrIdentity
from app.capabilities.image_studio.ocr import (
    EDGE_MARGIN,
    MATTE,
    PREPROCESSING,
    PREPROCESSING_VERSION,
    SEAM_MATCH,
    SKIP_BOX_EMPTY,
    SKIP_LINE_INVALID,
    SKIP_SCRIPT_KOREAN,
    SKIP_SCRIPT_NOT_CJK,
    SKIP_TEXT_EMPTY,
    TILE_HEIGHT,
    TILE_OVERLAP,
    EngineLine,
    PlacedLine,
    Preprocessing,
    ScriptReading,
    as_box,
    clamp_confidence,
    detect_script,
    merge_seams,
    place_line,
    tile_spans,
)
from app.capabilities.image_studio.style import Box

MODEL_A = "a" * 64
MODEL_B = "b" * 64

# ---------------------------------------------------------------- the pinned preprocessing


def test_the_preprocessing_constants_are_pinned_to_their_version() -> None:
    """A change of any of them is a new preprocessing: bump ``PREPROCESSING_VERSION`` with it."""
    assert Preprocessing("ocr-preprocess/v1", 2048, 256) == PREPROCESSING
    assert (PREPROCESSING_VERSION, TILE_HEIGHT, TILE_OVERLAP) == ("ocr-preprocess/v1", 2048, 256)
    assert (EDGE_MARGIN, SEAM_MATCH, MATTE) == (2, 0.5, (255, 255, 255))


@pytest.mark.parametrize(("tile", "overlap"), [(0, 0), (100, -1), (100, 51)])
def test_a_preprocessing_refuses_tiles_that_would_share_rows_beyond_neighbours(
    tile: int, overlap: int
) -> None:
    with pytest.raises(ValueError):
        Preprocessing("p", tile, overlap)
    with pytest.raises(ValueError):
        tile_spans(500, tile, overlap)


# ---------------------------------------------------------------- tiling


def test_an_image_no_taller_than_a_tile_is_one_tile() -> None:
    assert tile_spans(1) == ((0, 1),)
    assert tile_spans(800) == ((0, 800),)
    assert tile_spans(TILE_HEIGHT) == ((0, TILE_HEIGHT),)
    with pytest.raises(ValueError):
        tile_spans(0)


def test_a_tall_image_is_tiled_with_the_pinned_overlap() -> None:
    assert tile_spans(5000) == ((0, 2048), (1792, 3840), (3584, 5000))
    assert tile_spans(2049) == ((0, 2048), (1792, 2049))


def test_tiles_cover_every_row_overlap_only_their_neighbours_and_are_deterministic() -> None:
    rng = random.Random(7)
    for _ in range(300):
        tile = rng.randint(1, 120)
        overlap = rng.randint(0, tile // 2)
        height = rng.randint(1, 2000)
        spans = tile_spans(height, tile, overlap)
        assert spans == tile_spans(height, tile, overlap)
        assert spans[0][0] == 0 and spans[-1][1] == height
        assert all(0 < bottom - top <= tile for top, bottom in spans)
        for (top_a, bottom_a), (top_b, _) in pairwise(spans):
            assert top_b == top_a + tile - overlap  # a fixed step
            assert bottom_a - top_b == overlap  # no gap: exactly the overlap
        if len(spans) > 1:
            assert spans[-1][1] - spans[-1][0] > overlap  # the last tile adds rows of its own
        for a, b in zip(spans, spans[2:], strict=False):
            assert a[1] <= b[0]  # only neighbours share rows


# ---------------------------------------------------------------- placement


def test_a_box_is_moved_to_the_image_grid_rounded_outwards_and_clamped() -> None:
    line = EngineLine("纯天然", 0.9, box=(10.4, 5.6, 80.2, 30.0))
    placed = place_line(line, 1, (1792, 3840), 300, 3)
    assert isinstance(placed, PlacedLine)
    assert placed.box == (10, 1797, 81, 1822)
    assert (placed.tile, placed.text, placed.confidence, placed.cut) == (1, "纯天然", 0.9, False)
    clamped = place_line(EngineLine("x", 0.5, box=(-4, -3, 400, 9000)), 0, (0, 2048), 300, 1)
    assert isinstance(clamped, PlacedLine) and clamped.box == (0, 0, 300, 2048)


def test_a_polygon_gives_the_box_and_is_kept_in_the_image_grid() -> None:
    quad = ((10.0, 4.0), (90.6, 6.0), (90.0, 30.2), (9.4, 28.0))
    placed = place_line(EngineLine("麻油", 0.8, polygon=quad), 1, (100, 300), 200, 2)
    assert isinstance(placed, PlacedLine)
    assert placed.box == (9, 104, 91, 131)
    assert placed.polygon == ((10, 104), (91, 106), (90, 130), (9, 128))


def test_only_an_inner_tile_edge_cuts_a_line() -> None:
    span, width = (1792, 3840), 300
    top = EngineLine("x", 0.9, box=(0, EDGE_MARGIN, 50, 40))
    bottom = EngineLine("x", 0.9, box=(0, 2000, 50, 2048 - EDGE_MARGIN))
    inside = EngineLine("x", 0.9, box=(0, EDGE_MARGIN + 1, 50, 40))
    middle = place_line(top, 1, span, width, 3)
    assert isinstance(middle, PlacedLine) and middle.cut
    assert place_line(bottom, 1, span, width, 3).cut  # type: ignore[union-attr]
    assert not place_line(inside, 1, span, width, 3).cut  # type: ignore[union-attr]
    # The image's own top and bottom edges cut nothing.
    assert not place_line(top, 0, (0, 2048), width, 3).cut  # type: ignore[union-attr]
    assert not place_line(bottom, 2, (3584, 5632), width, 3).cut  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "line",
    [
        EngineLine("x", 0.9),
        EngineLine("x", 0.9, polygon=((0, 0), (5, 5))),
        EngineLine("x", 0.9, box=(10, 0, 5, 9)),
        EngineLine("x", 0.9, box=(0, 0, float("nan"), 9)),
        EngineLine("x", 0.9, polygon=((0, 0), (5, float("inf")), (0, 5))),
        EngineLine(None, 0.9, box=(0, 0, 5, 9)),  # type: ignore[arg-type]
    ],
)
def test_a_line_without_usable_geometry_or_text_is_skipped(line: EngineLine) -> None:
    assert place_line(line, 0, (0, 100), 100, 1) == SKIP_LINE_INVALID


def test_a_line_with_nothing_inside_its_tile_is_skipped() -> None:
    assert place_line(EngineLine("x", 0.9, box=(5, 5, 5, 9)), 0, (0, 100), 100, 1) == (
        SKIP_BOX_EMPTY
    )
    assert place_line(EngineLine("x", 0.9, box=(120, 5, 140, 9)), 0, (0, 100), 100, 1) == (
        SKIP_BOX_EMPTY
    )


# ---------------------------------------------------------------- seam merge


def _line(tile: int, box: tuple[int, int, int, int], text: str = "文", **kw: object) -> PlacedLine:
    return PlacedLine(tile, box, text, **{"confidence": 0.9, **kw})  # type: ignore[arg-type]


def test_a_whole_line_read_by_both_tiles_is_kept_once_at_the_higher_confidence() -> None:
    upper = _line(0, (10, 1900, 120, 1940), "芝麻油", confidence=0.81)
    lower = _line(1, (11, 1901, 121, 1941), "芝麻汕", confidence=0.93)
    assert merge_seams([upper, lower]) == (lower,)
    tie = _line(1, (11, 1901, 121, 1941), "芝麻汕", confidence=0.81)
    assert merge_seams([tie, upper]) == (upper,)  # an equal confidence keeps the upper tile


def test_a_whole_line_wins_over_its_cut_fragment_whatever_the_confidence() -> None:
    fragment = _line(0, (10, 2030, 120, 2048), "芝", confidence=0.99, cut=True)
    whole = _line(1, (10, 2030, 120, 2070), "芝麻油", confidence=0.70)
    assert merge_seams([fragment, whole]) == (whole,)


def test_a_line_cut_in_both_tiles_becomes_their_union_without_a_polygon() -> None:
    upper = _line(0, (200, 1700, 280, 2048), "大", confidence=0.6, cut=True, polygon=((0, 0),) * 3)
    lower = _line(1, (198, 1792, 281, 2100), "大字", confidence=0.8, cut=True)
    (merged,) = merge_seams([upper, lower])
    assert merged.box == (198, 1700, 281, 2100)
    assert (merged.text, merged.confidence, merged.tile, merged.polygon) == ("大字", 0.8, 1, None)


def test_only_lines_of_neighbouring_tiles_that_share_the_smaller_box_merge() -> None:
    same_tile = [_line(0, (10, 10, 100, 40)), _line(0, (12, 12, 100, 40), "字")]
    assert len(merge_seams(same_tile)) == 2
    far = [_line(0, (10, 10, 100, 40)), _line(2, (10, 10, 100, 40))]
    assert len(merge_seams(far)) == 2
    # Lines that only touch (a little shared area) are different lines.
    touching = [_line(0, (10, 1900, 100, 1940)), _line(1, (10, 1930, 100, 1970))]
    assert len(merge_seams(touching)) == 2


def test_the_merge_is_in_reading_order_whatever_the_input_order() -> None:
    lines = [
        _line(0, (10, 100, 120, 140), "一"),
        _line(0, (150, 100, 260, 140), "二"),
        _line(0, (10, 1900, 120, 1940), "三", confidence=0.5),
        _line(1, (10, 1900, 120, 1940), "三", confidence=0.7),
        _line(0, (10, 2030, 120, 2048), "四", cut=True),
        _line(1, (10, 2030, 120, 2070), "四"),
        _line(1, (10, 3000, 120, 3040), "五"),
    ]
    expected = merge_seams(lines)
    assert [line.text for line in expected] == ["一", "二", "三", "四", "五"]
    assert expected[2].confidence == 0.7 and expected[3].box == (10, 2030, 120, 2070)
    rng = random.Random(3)
    for _ in range(20):
        shuffled = list(lines)
        rng.shuffle(shuffled)
        assert merge_seams(shuffled) == expected


# ---------------------------------------------------------------- script and confidence


@pytest.mark.parametrize(
    ("text", "reading"),
    [
        ("纯天然芝麻油", ScriptReading("zh")),
        ("净含量350ml", ScriptReading("zh")),
        ("规格・型号", ScriptReading("zh")),  # the katakana middle dot is not kana evidence
        ("韩国制造 한국", ScriptReading("zh")),
        ("天然ごま油", ScriptReading("ja")),
        ("セール", ScriptReading("ja")),
        ("ｾｰﾙ", ScriptReading("ja")),
        ("人々", ScriptReading("ja")),
        ("参鶏湯のもと", ScriptReading("ja")),
        ("", ScriptReading(None, SKIP_TEXT_EMPTY)),
        ("  \n", ScriptReading(None, SKIP_TEXT_EMPTY)),
        ("참기름 100%", ScriptReading(None, SKIP_SCRIPT_KOREAN)),
        ("SALE 50%", ScriptReading(None, SKIP_SCRIPT_NOT_CJK)),
        ("350ml", ScriptReading(None, SKIP_SCRIPT_NOT_CJK)),
        ("Привет", ScriptReading(None, SKIP_SCRIPT_NOT_CJK)),
        ("・ー", ScriptReading(None, SKIP_SCRIPT_NOT_CJK)),
    ],
)
def test_the_script_is_japanese_with_kana_chinese_with_han_and_nothing_else(
    text: str, reading: ScriptReading
) -> None:
    assert detect_script(text) == reading


@pytest.mark.parametrize(
    ("raw", "clamped"),
    [
        (0.97, 0.97),
        (1, 1.0),
        (1.7, 1.0),
        (-0.2, 0.0),
        (0.123456, 0.1235),
        (float("nan"), 0.0),
        (float("inf"), 0.0),
        (float("-inf"), 0.0),
        (True, 0.0),
        ("0.9", 0.0),
        (None, 0.0),
    ],
)
def test_the_engines_confidence_is_clamped_to_the_unit_interval(
    raw: object, clamped: float
) -> None:
    assert clamp_confidence(raw) == clamped


# ---------------------------------------------------------------- identity and key


def test_the_key_names_the_engine_its_pinned_models_and_the_preprocessing() -> None:
    plain = OcrIdentity("rapidocr", "3.4.0", PREPROCESSING_VERSION)
    assert plain.key("f" * 64).engine_version == "3.4.0"
    pinned = OcrIdentity("rapidocr", "3.4.0", PREPROCESSING_VERSION, (MODEL_A, MODEL_B))
    key = pinned.key("f" * 64)
    assert key.engine_version.startswith("3.4.0+models.") and len(key.engine_version) <= 64
    assert key.preprocessing_version == PREPROCESSING_VERSION
    swapped = OcrIdentity("rapidocr", "3.4.0", PREPROCESSING_VERSION, (MODEL_B, MODEL_A))
    assert swapped.key("f" * 64) == key  # the set of models, not their order
    other = OcrIdentity("rapidocr", "3.4.0", PREPROCESSING_VERSION, (MODEL_A, "c" * 64))
    assert other.key("f" * 64) != key
    assert OcrIdentity("rapidocr", "3.4.1", PREPROCESSING_VERSION).key("f" * 64) != plain.key(
        "f" * 64
    )
    assert OcrIdentity("rapidocr", "3.4.0", "ocr-preprocess/v2").key("f" * 64) != plain.key(
        "f" * 64
    )


@pytest.mark.parametrize(
    "fields",
    [
        ("", "1", "p"),
        ("e", " 1", "p"),
        ("e", "1", ""),
        ("e", "1", "p", ("A" * 64,)),
        ("e", "1", "p", ("a" * 63,)),
        ("e", "1", "p", (MODEL_A, MODEL_A)),
        ("e", "v" * 65, "p"),
        ("e", "v" * 50, "p", (MODEL_A,)),
    ],
)
def test_a_malformed_identity_is_refused(fields: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        OcrIdentity(*fields)  # type: ignore[arg-type]


def test_a_region_box_reads_as_the_renderers_box() -> None:
    assert as_box((10, 20, 110, 60)) == Box(10, 20, 100, 40)
