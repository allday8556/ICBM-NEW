"""The MIME type and original dimensions of source image bytes (ADR-0010 §9, ruling on Q4).

This is the vetted decoder the source-asset store was written to take. It reads headers only: it
never decodes pixels, never resizes, re-encodes or otherwise transforms a source asset, and it
answers from the bytes themselves rather than from anything a server or a page declared.

**Supported formats** — the whole contract, and nothing outside it is stored:

==========  ==========  ====================================================
format      MIME        where the size is read
==========  ==========  ====================================================
JPEG        image/jpeg  the first start-of-frame segment
PNG         image/png   the IHDR chunk
GIF         image/gif   the logical screen descriptor
WebP        image/webp  the VP8, VP8L or VP8X chunk of the RIFF container
==========  ==========  ====================================================

Anything else — another format, a truncated header, a declared size of zero — is ``None``, and the
caller records the reference as ``UNSUPPORTED_FORMAT`` rather than storing bytes it cannot
describe. Every read is bounds-checked against the buffer it was given, so a malformed or hostile
header yields ``None`` instead of an exception or an unbounded scan.
"""

from app.collect.assets import DecodedImage

# A start-of-frame segment carries the frame's size. 0xC4, 0xC8 and 0xCC are a Huffman table, an
# extension and an arithmetic-coding table, which share the range but are not frames.
_JPEG_FRAME = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Segments that stand alone: they carry no length field to skip over.
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_WEBP_START_CODE = b"\x9d\x01\x2a"


def _u16be(data: bytes, at: int) -> int | None:
    return int.from_bytes(data[at : at + 2], "big") if at + 2 <= len(data) else None


def _u16le(data: bytes, at: int) -> int | None:
    return int.from_bytes(data[at : at + 2], "little") if at + 2 <= len(data) else None


def _u24le(data: bytes, at: int) -> int | None:
    return int.from_bytes(data[at : at + 3], "little") if at + 3 <= len(data) else None


def _u32be(data: bytes, at: int) -> int | None:
    return int.from_bytes(data[at : at + 4], "big") if at + 4 <= len(data) else None


def _sized(mime: str, width: int | None, height: int | None) -> DecodedImage | None:
    """A decode is only an answer when both dimensions were actually read and are positive."""
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    return DecodedImage(mime_type=mime, width=width, height=height)


def _png(data: bytes) -> DecodedImage | None:
    # The IHDR chunk is the first one, and its width and height open its payload.
    if data[12:16] != b"IHDR":
        return None
    return _sized("image/png", _u32be(data, 16), _u32be(data, 20))


def _gif(data: bytes) -> DecodedImage | None:
    return _sized("image/gif", _u16le(data, 6), _u16le(data, 8))


def _jpeg(data: bytes) -> DecodedImage | None:
    """Walk the segment chain to the first start-of-frame, skipping each segment by its length."""
    at = 2
    while at + 1 < len(data):
        if data[at] != 0xFF:  # not on a marker: the chain is malformed
            return None
        marker = data[at + 1]
        if marker == 0xFF:  # fill byte before the real marker
            at += 1
            continue
        if marker in _JPEG_STANDALONE:
            at += 2
            continue
        length = _u16be(data, at + 2)
        if length is None or length < 2:
            return None
        if marker in _JPEG_FRAME:
            # The frame header is precision, then height, then width.
            return _sized("image/jpeg", _u16be(data, at + 7), _u16be(data, at + 5))
        at += 2 + length
    return None


def _webp(data: bytes) -> DecodedImage | None:
    chunk, payload = data[12:16], data[20:]
    if chunk == b"VP8 ":
        # A key frame: the three-byte tag, then the start code, then 14-bit dimensions.
        if payload[3:6] != _WEBP_START_CODE:
            return None
        width, height = _u16le(payload, 6), _u16le(payload, 8)
        if width is None or height is None:
            return None
        return _sized("image/webp", width & 0x3FFF, height & 0x3FFF)
    if chunk == b"VP8L":
        if not payload or payload[0] != 0x2F:
            return None
        bits = int.from_bytes(payload[1:5], "little") if len(payload) >= 5 else None
        if bits is None:
            return None
        return _sized("image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8X":
        # The extended container states the canvas size, each as a 24-bit value less one.
        width, height = _u24le(payload, 4), _u24le(payload, 7)
        if width is None or height is None:
            return None
        return _sized("image/webp", width + 1, height + 1)
    return None


class HeaderImageDecoder:
    """Reads a source image's MIME type and original size from its own header bytes."""

    def decode(self, data: bytes) -> DecodedImage | None:
        if data.startswith(_PNG_MAGIC):
            return _png(data)
        if data.startswith(_GIF_MAGICS):
            return _gif(data)
        if data.startswith(b"\xff\xd8"):
            return _jpeg(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp(data)
        return None
