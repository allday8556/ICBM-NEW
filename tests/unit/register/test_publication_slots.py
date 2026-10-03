"""Issue #219 §2.2: a selected image is a representative, an additional (gallery) or a detail-body
image, and the preflight counts and places each accordingly. A detail-body image is never a
gallery image; until the detail composition places images, a unit carrying one is BLOCKED."""

from dataclasses import replace

from app.stages.products.image_model import ImageAssetKind, OutputRole, QaVerdict
from app.stages.products.model import ReadinessStatus
from app.stages.register.preparation import PublicationImage
from tests.unit.register.test_m5_preflight_rules import ITEMS, candidate, codes, resolved


def _image(role: OutputRole, position: int, digit: str) -> PublicationImage:
    return PublicationImage(
        role, position, ImageAssetKind.SOURCE_ASSET, digit * 64, None, f"qa-{digit}", QaVerdict.PASS
    )


def _unit_with(*images: PublicationImage) -> object:
    first = replace(ITEMS[0], images=images)
    return resolved(items=(first, *ITEMS[1:]))


def test_additional_images_are_gallery_images() -> None:
    unit = _unit_with(
        _image(OutputRole.REPRESENTATIVE, 0, "1"), _image(OutputRole.ADDITIONAL, 1, "2")
    )
    assert candidate(unit=unit).status is ReadinessStatus.READY  # type: ignore[arg-type]


def test_a_detail_body_image_blocks_until_the_detail_composition_places_it() -> None:
    unit = _unit_with(_image(OutputRole.REPRESENTATIVE, 0, "1"), _image(OutputRole.DETAIL, 1, "3"))
    result = candidate(unit=unit)  # type: ignore[arg-type]
    assert result.status is ReadinessStatus.BLOCKED
    assert "PUBLICATION_DETAIL_IMAGES_UNPLACED" in codes(result)


def test_detail_images_never_count_against_the_gallery_bound() -> None:
    details = tuple(_image(OutputRole.DETAIL, n, f"{n % 10}") for n in range(1, 15))
    unit = _unit_with(_image(OutputRole.REPRESENTATIVE, 0, "a"), *details)
    found = codes(candidate(unit=unit))  # type: ignore[arg-type]
    assert "PUBLICATION_ASSET_COUNT_EXCEEDED" not in found
    assert "PUBLICATION_DETAIL_IMAGES_UNPLACED" in found
