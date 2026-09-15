"""Encrypted local reconnaissance captures (ADR-0010 §5, §8; Issue #52 §6, §13).

The authenticated product page can hold member data and page tokens, so its raw bytes are
treated as credential-equivalent. A capture exists only inside the campaign directory, as an
AES-256-GCM blob:
* the 256-bit key lives in the campaign's secret store;
* the campaign ID and the capture label are bound into the authenticated data;
* files are replaced atomically (temp file, fsync, rename).

A capture is never committed, logged or copied into findings. Parser fixtures made from it are
synthetic or sanitized separately.
"""

import base64
import os
import re
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.secrets import SecretStore

KEY_NAME = "m3-recon:capture-key"
_MAGIC = b"ICBM-M3-CAPTURE-1\n"
_NONCE_BYTES = 12
LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


class CaptureError(RuntimeError):
    """A capture cannot be written or authenticated."""


class CaptureStore:
    def __init__(self, directory: Path, secrets: SecretStore, *, campaign_id: str) -> None:
        self._directory = directory
        self._secrets = secrets
        self._campaign_id = campaign_id

    def path(self, label: str) -> Path:
        if not LABEL.fullmatch(label):
            raise CaptureError("a capture label is lowercase letters, digits and dashes")
        return self._directory / f"{label}.enc"

    def _key(self, *, create: bool) -> bytes:
        stored = self._secrets.get(KEY_NAME)
        if stored:
            return base64.b64decode(stored)
        if not create:
            raise CaptureError("the campaign's capture key is gone")
        key = AESGCM.generate_key(bit_length=256)
        self._secrets.set(KEY_NAME, base64.b64encode(key).decode("ascii"))
        return key

    def _aad(self, label: str) -> bytes:
        return _MAGIC + f"{self._campaign_id}/{label}".encode("ascii")

    def save(self, label: str, data: bytes) -> Path:
        path = self.path(label)
        nonce = os.urandom(_NONCE_BYTES)
        blob = (
            _MAGIC + nonce + AESGCM(self._key(create=True)).encrypt(nonce, data, self._aad(label))
        )
        self._directory.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(".enc.tmp")
        with staging.open("wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
        return path

    def load(self, label: str) -> bytes:
        blob = self.path(label).read_bytes()
        if not blob.startswith(_MAGIC):
            raise CaptureError("not a reconnaissance capture")
        nonce = blob[len(_MAGIC) : len(_MAGIC) + _NONCE_BYTES]
        try:
            return AESGCM(self._key(create=False)).decrypt(
                nonce, blob[len(_MAGIC) + _NONCE_BYTES :], self._aad(label)
            )
        except InvalidTag:
            raise CaptureError("the capture does not authenticate") from None

    def labels(self) -> list[str]:
        if not self._directory.is_dir():
            return []
        return sorted(path.stem for path in self._directory.glob("*.enc"))
