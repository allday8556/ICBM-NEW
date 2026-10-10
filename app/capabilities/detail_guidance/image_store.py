"""The content-addressed byte store of rendered guidance images (ADR-0033 §2).

The bytes of each rendered PNG live at ``<data>/guidance/sha256/<aa>/<sha256>``, following
``DerivedImageStore``: the SHA-256 is computed here from the bytes and never taken from a caller;
the file is written atomically (a staged file, then a replace); a file already at the address is
reused only when its bytes match it, and anything else there fails closed and is left untouched;
every read verifies the checksum again. A file left behind by a rolled-back save is not a record:
the same bytes land at the same path, and a retry reuses it. Rows are the store owner's
(``store.DetailGuidanceStore``); this class never touches the database.
"""

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Final

from app.platform.core.errors import AppError, ErrorClass, NotFoundError

GUIDANCE_IMAGE_UNKNOWN: Final = "GUIDANCE_IMAGE_UNKNOWN"
GUIDANCE_IMAGE_EMPTY: Final = "GUIDANCE_IMAGE_EMPTY"
GUIDANCE_IMAGE_PATH_CONFLICT: Final = "GUIDANCE_IMAGE_PATH_CONFLICT"
GUIDANCE_IMAGE_MISSING: Final = "GUIDANCE_IMAGE_MISSING"
GUIDANCE_IMAGE_CORRUPT: Final = "GUIDANCE_IMAGE_CORRUPT"
_SHA256: Final = re.compile(r"[0-9a-f]{64}")


class GuidanceImageIntegrityError(AppError):
    """Stored guidance bytes no longer match their address, or another object occupies it."""

    error_class = ErrorClass.FATAL


def is_sha256(value: str) -> bool:
    return _SHA256.fullmatch(value) is not None


class GuidanceImageStore:
    """Content-addressed guidance image bytes, in their own namespace."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def path(self, sha256: str) -> Path:
        if not is_sha256(sha256):
            raise NotFoundError(GUIDANCE_IMAGE_UNKNOWN, "a guidance image is named by its SHA-256")
        return self._root / "sha256" / sha256[:2] / sha256

    def put(self, png: bytes) -> str:
        """Place ``png`` at its content address, atomically, and return its SHA-256.

        Equal bytes already at the address are reused. Anything else there is evidence that
        storage integrity is broken, so the write fails closed and leaves it exactly as it is."""
        if not png:
            raise GuidanceImageIntegrityError(GUIDANCE_IMAGE_EMPTY, "an empty image is not stored")
        sha256 = hashlib.sha256(png).hexdigest()
        path = self.path(sha256)
        if path.exists():
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha256:
                return sha256
            raise GuidanceImageIntegrityError(
                GUIDANCE_IMAGE_PATH_CONFLICT,
                "a different object already occupies this content address; it is left untouched",
                details={"sha256": sha256},
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f".{sha256}.{uuid.uuid4().hex}.tmp")
        try:
            with staging.open("wb") as handle:
                handle.write(png)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)
        return sha256

    def get(self, sha256: str) -> bytes:
        """The stored bytes, verified against their address."""
        path = self.path(sha256)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise GuidanceImageIntegrityError(
                GUIDANCE_IMAGE_MISSING,
                "a guidance image's file is missing",
                details={"sha256": sha256},
            ) from None
        if hashlib.sha256(data).hexdigest() != sha256:
            raise GuidanceImageIntegrityError(
                GUIDANCE_IMAGE_CORRUPT,
                "a guidance image no longer matches its checksum",
                details={"sha256": sha256},
            )
        return data
