"""ADR-0037 §2: the ``smartstore-upload-fit/v1`` recipe, pure (IC-02, IC-04, IC-06)."""

import io
import random

import pytest
from PIL import Image

from app.stages.products.image_model import ImageAssetKind, OutputRole, QaVerdict
from app.stages.register import upload_fit
from app.stages.register.preparation import (
    IMAGE_UPLOAD_FIT_FAILED,
    REASON_CODES,
    PublicationImage,
    UploadFit,
)
from app.stages.register.upload_fit import (
    UPLOAD_FIT_MAX_WIDTH,
    UPLOAD_FIT_QUALITIES,
    UPLOAD_FIT_THRESHOLD,
    UPLOAD_FIT_VERSION,
    FitFailure,
    FitRefused,
    FitResult,
    fit,
    needs_fit,
    transformation_spec,
)


def noise_jpeg(width: int, height: int, quality: int, seed: int = 11, **save: object) -> bytes:
    """Random noise compresses poorly: a JPEG of it is large for its pixel count."""
    pixels = random.Random(seed).randbytes(width * height * 3)
    buffer = io.BytesIO()
    Image.frombytes("RGB", (width, height), pixels).save(buffer, "JPEG", quality=quality, **save)
    return buffer.getvalue()


def _fitted(data: bytes) -> FitResult:
    result = fit(data)
    assert isinstance(result, FitResult), result
    return result


def test_the_recipe_constants_are_the_adrs() -> None:
    assert UPLOAD_FIT_VERSION == "smartstore-upload-fit/v1"
    assert UPLOAD_FIT_THRESHOLD == 9_500_000
    assert UPLOAD_FIT_MAX_WIDTH == 1000
    assert UPLOAD_FIT_QUALITIES == (90, 85, 80, 75, 70, 65, 60)


def test_the_threshold_is_at_or_over() -> None:
    assert not needs_fit(UPLOAD_FIT_THRESHOLD - 1)
    assert needs_fit(UPLOAD_FIT_THRESHOLD)
    assert needs_fit(25_000_000)


def test_the_same_input_gives_the_same_bytes() -> None:
    data = noise_jpeg(1600, 900, 90)
    first, second = _fitted(data), _fitted(data)
    assert first == second
    assert first.content == _fitted(bytes(data)).content


def test_a_25_mb_class_jpeg_fits_under_the_threshold_at_most_1000_px_wide() -> None:
    data = noise_jpeg(4000, 5400, 95)
    assert 20_000_000 < len(data) < 30_000_000 and needs_fit(len(data))
    result = _fitted(data)
    assert len(result.content) < UPLOAD_FIT_THRESHOLD
    assert (result.width, result.height) == (1000, 1350)
    assert result.quality == UPLOAD_FIT_QUALITIES[0]
    decoded = Image.open(io.BytesIO(result.content))
    assert (decoded.format, decoded.mode, decoded.size) == ("JPEG", "RGB", (1000, 1350))
    # Baseline, never progressive.
    assert "progressive" not in decoded.info and "progression" not in decoded.info


def test_a_narrow_image_keeps_its_width_and_steps_down_the_quality() -> None:
    # 1000 px wide, so nothing is resized; at quality 90 its noise is still over the threshold.
    data = noise_jpeg(1000, 12000, 100)
    assert needs_fit(len(data))
    result = _fitted(data)
    assert (result.width, result.height) == (1000, 12000)
    assert result.quality in UPLOAD_FIT_QUALITIES[1:]
    assert len(result.content) < UPLOAD_FIT_THRESHOLD


def test_metadata_is_dropped_and_the_orientation_applied() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise when shown
    exif[0x010F] = "invented camera"
    srgb = b"invented icc bytes are not a profile"
    data = noise_jpeg(1200, 600, 90, exif=exif.tobytes(), icc_profile=srgb)
    result = _fitted(data)
    decoded = Image.open(io.BytesIO(result.content))
    assert "exif" not in decoded.info and "icc_profile" not in decoded.info
    # Shown upright: 600 x 1200 before scaling, so it needs no resize.
    assert (result.width, result.height) == (600, 1200)


def test_an_embedded_profile_is_converted_to_srgb() -> None:
    from PIL import ImageCms

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    image = Image.new("RGB", (1200, 400), (200, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95, icc_profile=profile)
    result = _fitted(buffer.getvalue())
    decoded = Image.open(io.BytesIO(result.content))
    assert decoded.mode == "RGB" and "icc_profile" not in decoded.info


def test_transparency_is_flattened_onto_white() -> None:
    image = Image.new("RGBA", (1500, 300), (0, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    result = _fitted(buffer.getvalue())
    decoded = Image.open(io.BytesIO(result.content)).convert("RGB")
    assert (result.width, result.height) == (1000, 200)
    red, green, blue = decoded.getpixel((500, 100))  # type: ignore[misc]
    assert min(red, green, blue) >= 250


def test_an_undecodable_image_is_refused() -> None:
    assert fit(b"\x89PNG" + bytes(4096)) == FitRefused(FitFailure.UNDECODABLE)
    assert fit(b"") == FitRefused(FitFailure.UNDECODABLE)
    truncated = noise_jpeg(1200, 800, 90)[:5000]
    assert fit(truncated) == FitRefused(FitFailure.UNDECODABLE)


def test_nothing_smaller_is_guessed_when_no_quality_step_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = noise_jpeg(800, 800, 95)
    monkeypatch.setattr(upload_fit, "UPLOAD_FIT_THRESHOLD", 1000)
    assert fit(data) == FitRefused(FitFailure.NOT_FITTED)


def test_the_recorded_spec_is_short_codes_and_integers() -> None:
    spec = transformation_spec(85)
    assert spec["recipe"] == "smartstore-upload-fit" and spec["quality"] == 85
    assert spec["threshold_bytes"] == UPLOAD_FIT_THRESHOLD
    for value in spec.values():
        assert isinstance(value, str | int | list)


# ---------------------------------------------------------------- the publication image


def _image(**changes: object) -> PublicationImage:
    values: dict[str, object] = {
        "role": OutputRole.DETAIL,
        "position": 1,
        "asset_kind": ImageAssetKind.SOURCE_ASSET,
        "sha256": "a" * 64,
        "derivation_id": None,
        "qa_result_id": "qa-1",
        "qa_verdict": QaVerdict.PASS,
    }
    values.update(changes)
    return PublicationImage(**values)  # type: ignore[arg-type]


def test_an_image_without_a_variant_keeps_its_earlier_canonical_form() -> None:
    plain = _image()
    assert set(plain.canonical()) == {
        "role",
        "position",
        "asset_kind",
        "sha256",
        "derivation_id",
        "qa_result_id",
        "qa_verdict",
    }
    # A failed fit is a reason, never a change to the image's identity.
    assert _image(upload_fit_failed=True).canonical() == plain.canonical()


def test_a_variant_names_the_selected_binary_it_stands_in_for() -> None:
    lineage = UploadFit(
        UPLOAD_FIT_VERSION, ImageAssetKind.SOURCE_ASSET, "a" * 64, None, 1000, 1350, 1
    )
    variant = _image(
        asset_kind=ImageAssetKind.DERIVED_ARTIFACT,
        sha256="b" * 64,
        derivation_id="d1",
        upload_fit=lineage,
    )
    assert variant.key == ("DERIVED_ARTIFACT", "b" * 64, "d1")
    assert variant.canonical()["upload_fit"] == lineage.canonical()
    assert IMAGE_UPLOAD_FIT_FAILED in REASON_CODES
