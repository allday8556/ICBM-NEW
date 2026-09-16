"""The MIME type and original size a source image states about itself (ADR-0010 §9).

The decoder reads headers only, from the bytes themselves — never from a declared content type.
Its supported set is the whole contract: JPEG, PNG, GIF and WebP. Anything else is ``None``, and
the caller records the reference as UNSUPPORTED_FORMAT rather than storing bytes it cannot
describe.
"""

import struct

import pytest

from app.collect.imagedecode import HeaderImageDecoder

decoder = HeaderImageDecoder()


def png(width: int, height: int) -> bytes:
    ihdr = b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + b"\x00" * 4


def gif(width: int, height: int, version: bytes = b"GIF89a") -> bytes:
    return version + struct.pack("<HH", width, height) + b"\x70\x00\x00"


def jpeg(width: int, height: int, *, marker: int = 0xC0, before: bytes = b"") -> bytes:
    frame = struct.pack(">BHH", 8, height, width) + b"\x03"
    segment = bytes([0xFF, marker]) + struct.pack(">H", 2 + len(frame)) + frame
    return b"\xff\xd8" + before + segment + b"\xff\xd9"


def webp_lossy(width: int, height: int) -> bytes:
    payload = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return _riff(b"VP8 ", payload)


def webp_lossless(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    return _riff(b"VP8L", b"\x2f" + struct.pack("<I", bits))


def webp_extended(width: int, height: int) -> bytes:
    payload = b"\x00" * 4 + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return _riff(b"VP8X", payload)


def _riff(chunk: bytes, payload: bytes) -> bytes:
    body = b"WEBP" + chunk + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize(
    ("data", "mime", "size"),
    [
        (png(1920, 1080), "image/png", (1920, 1080)),
        (gif(320, 240), "image/gif", (320, 240)),
        (gif(8, 8, version=b"GIF87a"), "image/gif", (8, 8)),
        (jpeg(1200, 900), "image/jpeg", (1200, 900)),
        (jpeg(640, 480, marker=0xC2), "image/jpeg", (640, 480)),  # progressive
        (webp_lossy(500, 400), "image/webp", (500, 400)),
        (webp_lossless(64, 32), "image/webp", (64, 32)),
        (webp_extended(4096, 2160), "image/webp", (4096, 2160)),
    ],
)
def test_the_supported_formats_state_their_own_size(
    data: bytes, mime: str, size: tuple[int, int]
) -> None:
    decoded = decoder.decode(data)
    assert decoded is not None
    assert (decoded.mime_type, decoded.width, decoded.height) == (mime, *size)


def test_a_jpeg_frame_is_found_past_the_segments_before_it() -> None:
    # Real files put APPn and comment segments first; each is skipped by its own length.
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    # A segment's length field counts itself and its payload: two bytes plus seven.
    comment = b"\xff\xfe" + struct.pack(">H", 9) + b"padding"
    decoded = decoder.decode(jpeg(300, 200, before=app0 + comment))
    assert decoded is not None and (decoded.width, decoded.height) == (300, 200)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an image at all",
        b"%PDF-1.7\n%\xe2\xe3\xcf\xd3",  # another format entirely
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,  # PNG magic, truncated before IHDR
        png(0, 10),  # a declared size of zero
        png(10, 0),
        b"\xff\xd8\xff\xe0",  # JPEG magic, no frame
        jpeg(10, 10)[:6],  # truncated mid-header
        gif(0, 0),
        _riff(b"VP8 ", b"\x00" * 6),  # WebP without its start code
        _riff(b"XXXX", b"\x00" * 16),  # a RIFF that is not one of the WebP chunks
        b"RIFF" + struct.pack("<I", 4) + b"WAVE",  # RIFF, but not WebP at all
    ],
)
def test_anything_it_cannot_read_is_not_an_answer(data: bytes) -> None:
    assert decoder.decode(data) is None


def test_a_truncated_header_never_raises_or_scans_without_a_bound() -> None:
    # Every prefix of a real file either decodes or answers None; none of them throws.
    full = jpeg(64, 48, before=b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9)
    for cut in range(len(full) + 1):
        decoded = decoder.decode(full[:cut])
        assert decoded is None or (decoded.width, decoded.height) == (64, 48)


def test_the_bytes_decide_the_mime_type_not_a_declared_one() -> None:
    # A PNG body is a PNG whatever a server called it; nothing here reads a header field.
    decoded = decoder.decode(png(2, 3))
    assert decoded is not None and decoded.mime_type == "image/png"
