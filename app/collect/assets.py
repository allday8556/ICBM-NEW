"""Content-addressed COLLECT source assets (M3 PR-B, ADR-0010 §9).

The original bytes are stored exactly once under ``<directory>/sha256/<aa>/<sha256>``, keyed by
their own SHA-256, which is always computed here and never taken from a caller. MIME, width and
height come from an injected decoder over the same bytes (ruling on Q4); bytes it cannot decode
are refused and nothing is written, so the caller records the reference as UNSUPPORTED_FORMAT.

The file is written atomically (temp file, fsync, rename) before its row is inserted, so a stored
row always had its bytes. Rows are append-only, and a file is only ever replaced by bytes whose
checksum matches its name; every read verifies the checksum again. Nothing here resizes,
re-encodes or otherwise transforms a source asset (ADR-0010 §9).
"""

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.collect.models import SourceAsset
from app.core.clock import Clock
from app.core.errors import AppError, ErrorClass, NotFoundError
from app.db.database import Database


@dataclass(frozen=True)
class DecodedImage:
    mime_type: str
    width: int
    height: int


class ImageDecoder(Protocol):
    """Derives MIME and dimensions from the observed bytes (ADR-0010 §9, ruling on Q4). The vetted
    implementation is chosen in PR-C after reconnaissance."""

    def decode(self, data: bytes) -> DecodedImage | None:
        """The image's MIME type and dimensions, or ``None`` for an unknown or unsupported
        format."""
        ...


@dataclass(frozen=True)
class StoredAsset:
    sha256: str
    mime_type: str
    byte_size: int
    width: int
    height: int


class UnsupportedSourceImageError(AppError):
    """The bytes are not an image the decoder can read; the reference is REVIEW_REQUIRED."""

    error_class = ErrorClass.REVIEW_REQUIRED


class SourceAssetIntegrityError(AppError):
    """Stored bytes or metadata no longer match their checksum."""

    error_class = ErrorClass.FATAL


class SourceAssetStore:
    def __init__(self, directory: Path, db: Database, decoder: ImageDecoder, clock: Clock) -> None:
        self._directory = directory
        self._db = db
        self._decoder = decoder
        self._clock = clock

    def path(self, sha256: str) -> Path:
        return self._directory / "sha256" / sha256[:2] / sha256

    def put(self, data: bytes) -> StoredAsset:
        """Store observed bytes once and return what was stored."""
        if not data:
            raise UnsupportedSourceImageError("COLLECT_IMAGE_EMPTY", "an empty body is no image")
        sha256 = hashlib.sha256(data).hexdigest()
        decoded = self._decoder.decode(data)
        if (
            decoded is None
            or not decoded.mime_type.startswith("image/")
            or decoded.width <= 0
            or decoded.height <= 0
        ):
            raise UnsupportedSourceImageError(
                "COLLECT_IMAGE_UNSUPPORTED", "the bytes are not an image the decoder can read"
            )
        asset = StoredAsset(sha256, decoded.mime_type, len(data), decoded.width, decoded.height)
        self._write_file(sha256, data)
        with self._db.write() as session:
            existing = session.get(SourceAsset, sha256)
            if existing is None:
                session.add(
                    SourceAsset(
                        sha256=sha256,
                        mime_type=asset.mime_type,
                        byte_size=asset.byte_size,
                        width=asset.width,
                        height=asset.height,
                        stored_at=self._clock.now(),
                    )
                )
            elif _row(existing) != asset:
                raise SourceAssetIntegrityError(
                    "COLLECT_ASSET_METADATA_CONFLICT",
                    "stored metadata differs from a new decode of the same bytes",
                )
        return asset

    def get(self, sha256: str) -> StoredAsset | None:
        with self._db.read() as session:
            row = session.get(SourceAsset, sha256)
            return None if row is None else _row(row)

    def read(self, sha256: str) -> bytes:
        """The stored bytes, verified against their checksum."""
        if self.get(sha256) is None:
            raise NotFoundError("COLLECT_ASSET_UNKNOWN", "no source asset has that checksum")
        try:
            data = self.path(sha256).read_bytes()
        except FileNotFoundError:
            raise SourceAssetIntegrityError(
                "COLLECT_ASSET_MISSING", "a stored source asset's file is missing"
            ) from None
        if hashlib.sha256(data).hexdigest() != sha256:
            raise SourceAssetIntegrityError(
                "COLLECT_ASSET_CORRUPT", "a stored source asset no longer matches its checksum"
            )
        return data

    def _write_file(self, sha256: str, data: bytes) -> None:
        path = self.path(sha256)
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha256:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f".{sha256}.{uuid.uuid4().hex}.tmp")
        try:
            with staging.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)


def _row(row: SourceAsset) -> StoredAsset:
    return StoredAsset(row.sha256, row.mime_type, row.byte_size, row.width, row.height)
