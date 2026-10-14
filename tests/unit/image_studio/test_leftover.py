"""ADR-0041 §6, I6: which included region a source-script line read on the rendering belongs to."""

from app.capabilities.image_studio.contracts import OcrRegion
from app.capabilities.image_studio.leftover import (
    LeftoverReading,
    find_leftover,
    grown,
    owner,
)

# Two flat lines one above the other, and a tall badge to the right of the first.
REGIONS = (
    OcrRegion("r0", (20, 20, 240, 60), "纯天然芝麻油", "zh", 0.97, 1.0),
    OcrRegion("r1", (20, 80, 150, 120), "净含量350ml", "zh", 0.95, 1.0),
    OcrRegion("r2", (260, 20, 390, 120), "天然ごま油", "ja", 0.91, 0.3),
)


def line(box: tuple[int, int, int, int], text: str = "含量") -> OcrRegion:
    return OcrRegion("x", box, text, "zh", 0.9, 1.0)


def test_a_box_grows_by_its_shorter_side() -> None:
    assert grown((20, 80, 150, 120)) == (-20, 40, 190, 160)
    assert grown((0, 0, 10, 100)) == (-10, -10, 20, 110)


def test_text_left_inside_an_included_box_flags_it() -> None:
    found = find_leftover(REGIONS, frozenset({"r0", "r1"}), [line((40, 25, 120, 55))])
    assert found == LeftoverReading(regions=("r0",), lines=1, unattributed=0)


def test_text_just_past_a_too_tight_box_belongs_to_the_nearest_region() -> None:
    # Past r1's right edge: r0, r1 and the tall r2 all reach it once grown; r1 is nearest.
    assert owner((160, 90, 204, 110), REGIONS) == "r1"
    found = find_leftover(REGIONS, frozenset({"r1"}), [line((160, 90, 204, 110))])
    assert found.regions == ("r1",)


def test_a_kept_regions_own_text_flags_nothing() -> None:
    reread = [line((30, 30, 204, 50)), line((280, 40, 380, 100), "ごま")]
    found = find_leftover(REGIONS, frozenset({"r1"}), reread)
    assert found == LeftoverReading(regions=(), lines=2, unattributed=0)


def test_a_line_near_no_region_is_counted_and_flags_nothing() -> None:
    found = find_leftover(REGIONS, frozenset({"r0", "r1", "r2"}), [line((20, 400, 200, 440))])
    assert found == LeftoverReading(regions=(), lines=1, unattributed=1)


def test_only_source_script_counts_and_the_korean_drawn_never_does() -> None:
    reread = [
        line((40, 25, 120, 55), "순수 천연 참기름"),
        line((40, 85, 120, 115), "350ml"),
        line((40, 25, 120, 55), "순수 天然"),
    ]
    found = find_leftover(REGIONS, frozenset({"r0", "r1"}), reread)
    # The Hangul and the Latin lines are not source script; a mixed line counts by its Han.
    assert found == LeftoverReading(regions=("r0",), lines=1, unattributed=0)


def test_an_empty_line_or_region_belongs_to_nothing() -> None:
    assert owner((10, 10, 10, 30), REGIONS) is None
    assert owner((10, 10, 30, 30), (OcrRegion("e", (5, 5, 5, 5), "空", "zh", 1.0, 1.0),)) is None


def test_flagged_regions_come_in_reading_order() -> None:
    reread = [line((160, 90, 204, 110)), line((40, 25, 120, 55))]
    found = find_leftover(REGIONS, frozenset({"r0", "r1"}), reread)
    assert found.regions == ("r0", "r1")
