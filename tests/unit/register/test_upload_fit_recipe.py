"""ADR-0037 §2: the ``smartstore-upload-fit/v1`` recipe, pure (IC-02, IC-04, IC-06)."""

import hashlib
import io
import random
import struct

import PIL
import pytest
from PIL import Image, ImageCms

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
    Colour,
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


# The golden variant of one seeded input (IC-04 across processes and machines). It changes
# together with ``pillow==12.3.0`` in ``pyproject.toml``: a Pillow upgrade re-pins it, and the
# recorded ``encoder`` of a variant made since then names the new version.
GOLDEN_INPUT_SHA256 = "3fde62684f227b6abcc9a85d9cce0d155579621a6d507247819e689df00bb57c"
GOLDEN_VARIANT_SHA256 = "782fdd42c53d4af55ff3098613d7d43f7e5fcc9c4ab5c5d82a86ddb46429435e"


def test_the_golden_variant_of_a_seeded_input_is_pinned() -> None:
    assert PIL.__version__ == "12.3.0"
    data = noise_jpeg(1600, 900, 90, seed=2026)
    assert hashlib.sha256(data).hexdigest() == GOLDEN_INPUT_SHA256
    result = _fitted(data)
    assert (result.width, result.height, result.quality) == (1000, 562, 90)
    assert hashlib.sha256(result.content).hexdigest() == GOLDEN_VARIANT_SHA256


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


def test_an_image_between_the_threshold_and_10_mb_is_fitted() -> None:
    # Under SmartStore's 10^7-byte call limit, yet with no room for the multipart envelope.
    data = noise_jpeg(2400, 3450, 95)
    assert UPLOAD_FIT_THRESHOLD <= len(data) < 10_000_000 and needs_fit(len(data))
    result = _fitted(data)
    assert len(result.content) < UPLOAD_FIT_THRESHOLD and result.width == 1000


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
    # A profile the engine cannot read is never claimed as converted.
    assert result.colour is Colour.ASSUMED_SRGB
    assert transformation_spec(result.quality, result.colour)["colour"] == "ASSUMED_SRGB"


def _s15(value: float) -> bytes:
    return struct.pack(">i", round(value * 65536))


def _xyz(x: float, y: float, z: float) -> bytes:
    return b"XYZ " + bytes(4) + _s15(x) + _s15(y) + _s15(z)


def _curve(gamma: float) -> bytes:
    return b"curv" + bytes(4) + struct.pack(">IH", 1, round(gamma * 256)) + bytes(2)


def _desc(text: str) -> bytes:
    raw = text.encode("ascii") + b"\x00"
    body = b"desc" + bytes(4) + struct.pack(">I", len(raw)) + raw
    body += struct.pack(">IIHB", 0, 0, 0, 0) + bytes(67)
    return body + bytes(-len(body) % 4)


def adobe_rgb_like_profile() -> bytes:
    """A minimal ICC v2 RGB matrix/TRC display profile with Adobe RGB (1998) primaries adapted to
    the D50 PCS and a 2.2 gamma: a real non-sRGB RGB profile. Pillow can build only sRGB, LAB and
    XYZ profiles, so it is written out here, tag by tag."""
    tags = [
        (b"desc", _desc("Adobe RGB-like test profile")),
        (b"wtpt", _xyz(0.9642, 1.0, 0.8249)),
        (b"rXYZ", _xyz(0.6097, 0.3111, 0.0195)),
        (b"gXYZ", _xyz(0.2053, 0.6257, 0.0609)),
        (b"bXYZ", _xyz(0.1492, 0.0632, 0.7446)),
        (b"rTRC", _curve(2.2)),
        (b"gTRC", _curve(2.2)),
        (b"bTRC", _curve(2.2)),
    ]
    start = 128 + 4 + 12 * len(tags)
    table, data = struct.pack(">I", len(tags)), b""
    for signature, body in tags:
        table += signature + struct.pack(">II", start + len(data), len(body))
        data += body
    header = (
        struct.pack(">I", start + len(data))
        + b"lcms"
        + struct.pack(">I", 0x02100000)
        + b"mntrRGB XYZ "
        + bytes(12)
        + b"acsp"
        + bytes(24)
        + struct.pack(">I", 0)
        + _s15(0.9642)
        + _s15(1.0)
        + _s15(0.8249)
    )
    return header + bytes(128 - len(header)) + table + data


def _flat(colour: tuple[int, int, int], **save: object) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1200, 400), colour).save(buffer, "PNG", **save)
    return buffer.getvalue()


def test_an_embedded_non_srgb_profile_is_converted_to_srgb() -> None:
    profile = adobe_rgb_like_profile()
    assert "Adobe RGB-like" in ImageCms.getProfileDescription(
        ImageCms.ImageCmsProfile(io.BytesIO(profile))
    )
    colour = (60, 200, 60)
    converted = _fitted(_flat(colour, icc_profile=profile))
    untouched = _fitted(_flat(colour))
    assert converted.colour is Colour.SRGB and untouched.colour is Colour.SRGB
    decoded = Image.open(io.BytesIO(converted.content))
    assert decoded.mode == "RGB" and "icc_profile" not in decoded.info
    shown = decoded.getpixel((500, 100))
    plain = Image.open(io.BytesIO(untouched.content)).getpixel((500, 100))
    assert isinstance(shown, tuple) and isinstance(plain, tuple)
    # The same pixel values in a wider gamut are a more saturated green in sRGB.
    assert abs(plain[0] - colour[0]) <= 3
    assert shown[0] < plain[0] - 30 and shown[2] < plain[2] - 15


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
    assert spec["colour"] == "SRGB"
    # A later Pillow upgrade shows in every new variant's lineage.
    assert spec["encoder"] == f"pillow/{PIL.__version__}"
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
