"""The SmartStore upload-fit recipe (ADR-0037 §2): ``smartstore-upload-fit/v1``.

SmartStore's image upload accepts less than 10^7 bytes per call, measured on the whole multipart
payload. An image chosen for upload whose stored bytes are **9,500,000 or more** is not sent as it
is: this recipe re-encodes it into a derived artifact (ADR-0013 §9) that fits, and the source asset
is never touched (IC-03).

Pure: bytes in, bytes out. No database, no clock, no file and no network. The recipe:

1. decode the image with the pinned decoder (Pillow, ``pyproject.toml``); an animated image keeps
   its first frame;
2. apply the EXIF orientation, so the variant shows what the source shows, then drop every piece
   of metadata — EXIF, ICC, comments — by never passing any to the encoder;
3. convert to sRGB: an embedded ICC profile is converted with the colour-management engine, an
   image without one is taken as sRGB already; a profile the engine cannot read is not guessed
   at either — the pixels are taken as sRGB and the variant records ``ASSUMED_SRGB``;
   transparency is flattened onto white;
4. if wider than :data:`UPLOAD_FIT_MAX_WIDTH`, scale down to that width with LANCZOS, keeping the
   aspect ratio;
5. encode baseline JPEG (``optimize=False``, ``progressive=False``, 4:2:0 subsampling) at each of
   :data:`UPLOAD_FIT_QUALITIES` in order, and keep the first result under
   :data:`UPLOAD_FIT_THRESHOLD`.

**Deterministic** (IC-04): the same input bytes and recipe version give the same output bytes,
under the pinned encoder. The encoder version is recorded with every variant (``encoder``), and a
golden test pins one variant's SHA-256, so a Pillow upgrade shows in the lineage and in CI.
Nothing smaller is guessed: an image no quality step fits, or one that cannot be decoded, is a
:class:`FitRefused` and its unit is ``REVIEW_REQUIRED`` (IC-06).
"""

import io
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import PIL
from PIL import Image, ImageCms, ImageOps

UPLOAD_FIT_RECIPE: Final = "smartstore-upload-fit"
UPLOAD_FIT_VERSION: Final = "smartstore-upload-fit/v1"
# At or over this many bytes an image is never sent to SmartStore's image upload (IC-02). It leaves
# room for the multipart envelope under the 10^7-byte limit of one call.
UPLOAD_FIT_THRESHOLD: Final = 9_500_000
UPLOAD_FIT_MAX_WIDTH: Final = 1000
UPLOAD_FIT_QUALITIES: Final = (90, 85, 80, 75, 70, 65, 60)
# Pillow's subsampling code 2 is 4:2:0, fixed for every quality step.
UPLOAD_FIT_SUBSAMPLING: Final = 2
UPLOAD_FIT_MEDIA_TYPE: Final = "image/jpeg"
# The bounded capability code of the one LOCAL operation a variant records.
UPLOAD_FIT_CAPABILITY: Final = "IMAGE_UPLOAD_FIT"
# The marketplaces whose image upload this recipe fits (ADR-0037 §2).
UPLOAD_FIT_MARKETPLACES: Final = frozenset({"smartstore"})
# The pinned decoder and encoder (``pyproject.toml``), recorded with every variant.
UPLOAD_FIT_ENCODER: Final = f"pillow/{PIL.__version__}"


class Colour(StrEnum):
    """How the variant's pixels came to be sRGB."""

    SRGB = "SRGB"  # converted from the embedded profile, or no profile: sRGB by convention
    ASSUMED_SRGB = "ASSUMED_SRGB"  # an embedded profile the engine could not read


class FitFailure(StrEnum):
    """Why the recipe yields no variant."""

    UNDECODABLE = "UNDECODABLE"
    NOT_FITTED = "NOT_FITTED"


@dataclass(frozen=True)
class FitResult:
    content: bytes
    width: int
    height: int
    quality: int
    colour: Colour = Colour.SRGB


@dataclass(frozen=True)
class FitRefused:
    failure: FitFailure


def needs_fit(byte_size: int) -> bool:
    """Whether an image of this many stored bytes may not be uploaded as it is."""
    return byte_size >= UPLOAD_FIT_THRESHOLD


def transformation_spec(quality: int, colour: Colour = Colour.SRGB) -> Mapping[str, object]:
    """The recipe as recorded with a variant's derivation: short codes and integers only."""
    return {
        "recipe": UPLOAD_FIT_RECIPE,
        "encoder": UPLOAD_FIT_ENCODER,
        "threshold_bytes": UPLOAD_FIT_THRESHOLD,
        "max_width": UPLOAD_FIT_MAX_WIDTH,
        "resample": "LANCZOS",
        "format": "JPEG_BASELINE",
        "qualities": list(UPLOAD_FIT_QUALITIES),
        "quality": quality,
        "subsampling": "4:2:0",
        "colour": colour.value,
        "orientation": "APPLIED",
        "metadata": "DROPPED",
        "frame": "FIRST",
    }


_DECODE_ERRORS: Final = (
    OSError,
    ValueError,
    SyntaxError,
    EOFError,
    TypeError,
    Image.DecompressionBombError,
)


def _decoded(data: bytes) -> Image.Image | None:
    try:
        image = Image.open(io.BytesIO(data))
        image.seek(0)
        image.load()
        return ImageOps.exif_transpose(image)
    except _DECODE_ERRORS:
        return None


def _flattened(image: Image.Image) -> Image.Image:
    """Transparency onto white, in the image's own colour space (white is white in any)."""
    if image.mode == "P":
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    if image.mode in ("RGBA", "LA", "PA", "La", "RGBa"):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")
    return image


def _srgb(image: Image.Image, icc: bytes | None) -> tuple[Image.Image, Colour]:
    """sRGB, by the embedded profile when it can be read; otherwise the pixels are taken as sRGB,
    and a profile that could not be read is recorded as ``ASSUMED_SRGB``."""
    colour = Colour.SRGB
    if icc:
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            converted = ImageCms.profileToProfile(
                image,
                source,
                ImageCms.createProfile("sRGB"),
                renderingIntent=ImageCms.Intent.PERCEPTUAL,
                outputMode="RGB",
            )
            if converted is not None:
                return converted, Colour.SRGB
        except (ImageCms.PyCMSError, OSError, ValueError, TypeError):
            pass
        colour = Colour.ASSUMED_SRGB
    return (image if image.mode == "RGB" else image.convert("RGB")), colour


def fit(data: bytes) -> FitResult | FitRefused:
    """The recipe over one image's exact bytes."""
    image = _decoded(data)
    if image is None:
        return FitRefused(FitFailure.UNDECODABLE)
    try:
        icc = image.info.get("icc_profile")
        image, colour = _srgb(_flattened(image), icc if isinstance(icc, bytes) else None)
        width, height = image.size
        if width > UPLOAD_FIT_MAX_WIDTH:
            height = max(1, round(height * UPLOAD_FIT_MAX_WIDTH / width))
            width = UPLOAD_FIT_MAX_WIDTH
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        for quality in UPLOAD_FIT_QUALITIES:
            buffer = io.BytesIO()
            image.save(
                buffer,
                "JPEG",
                quality=quality,
                optimize=False,
                progressive=False,
                subsampling=UPLOAD_FIT_SUBSAMPLING,
            )
            content = buffer.getvalue()
            if len(content) < UPLOAD_FIT_THRESHOLD:
                return FitResult(content, width, height, quality, colour)
    except _DECODE_ERRORS:
        # A file whose header decoded but whose pixels do not.
        return FitRefused(FitFailure.UNDECODABLE)
    return FitRefused(FitFailure.NOT_FITTED)
