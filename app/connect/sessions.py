"""Encrypted at-rest storage for authenticated supplier sessions (Issue #7 §1, ADR-0007).

An authenticated session (cookies, tokens) is credential-equivalent. It exists on disk only as an
AES-256-GCM blob at ``<ICBM_DATA_DIR>/sessions/<supplier_key>.enc``; the 256-bit key lives in the
OS secret store, never beside the blob. The supplier key is bound into the authenticated data, so
one supplier's blob cannot be replayed as another's. Replacement is atomic (encrypted temp file,
fsync, rename). A blob that cannot be authenticated or decrypted — tampered, truncated, or its key
is gone — is discarded rather than partially reused, which leads to safe bounded
re-authentication when the session is next needed.
"""

import base64
import contextlib
import logging
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.safe_payload import safe_payload
from app.core.secrets import SecretStore
from integrations.suppliers.base import SUPPLIER_KEY

logger = logging.getLogger("icbm.connect.sessions")

SESSIONS_DIR_NAME = "sessions"
_MAGIC = b"ICBMSESS1"
_NONCE_BYTES = 12


def _key_name(supplier_key: str) -> str:
    return f"supplier:{supplier_key}:session_key"


def _checked(supplier_key: str) -> str:
    if not SUPPLIER_KEY.fullmatch(supplier_key):
        raise ValueError(f"invalid supplier key: {supplier_key!r}")
    return supplier_key


class SupplierSessionStore:
    def __init__(self, directory: Path, secrets: SecretStore) -> None:
        self._directory = directory
        self._secrets = secrets

    def path(self, supplier_key: str) -> Path:
        return self._directory / f"{_checked(supplier_key)}.enc"

    def _aad(self, supplier_key: str) -> bytes:
        return _MAGIC + supplier_key.encode("ascii")

    def _key(self, supplier_key: str, *, create: bool) -> bytes | None:
        stored = self._secrets.get(_key_name(supplier_key))
        if stored:
            return base64.b64decode(stored)
        if not create:
            return None
        key = AESGCM.generate_key(bit_length=256)
        self._secrets.set(_key_name(supplier_key), base64.b64encode(key).decode("ascii"))
        return key

    def exists(self, supplier_key: str) -> bool:
        return self.path(supplier_key).is_file()

    def save(self, supplier_key: str, payload: bytes) -> None:
        path = self.path(supplier_key)
        key = self._key(supplier_key, create=True)
        assert key is not None
        nonce = os.urandom(_NONCE_BYTES)
        blob = _MAGIC + nonce + AESGCM(key).encrypt(nonce, payload, self._aad(supplier_key))
        self._directory.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(".enc.tmp")
        with staging.open("wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)

    def load(self, supplier_key: str) -> bytes | None:
        path = self.path(supplier_key)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        key = self._key(supplier_key, create=False)
        if key is not None and blob.startswith(_MAGIC):
            nonce = blob[len(_MAGIC) : len(_MAGIC) + _NONCE_BYTES]
            with contextlib.suppress(InvalidTag, ValueError):
                return AESGCM(key).decrypt(
                    nonce, blob[len(_MAGIC) + _NONCE_BYTES :], self._aad(supplier_key)
                )
        logger.warning("supplier.session.discarded", extra=safe_payload(supplier_key=supplier_key))
        self.clear(supplier_key)
        return None

    def clear(self, supplier_key: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path(supplier_key).unlink()
