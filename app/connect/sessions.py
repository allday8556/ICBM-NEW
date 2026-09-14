"""Encrypted at-rest storage for authenticated sessions (Issue #7 §1, ADR-0007).

An authenticated session (cookies, tokens) is credential-equivalent. It exists on disk only as an
AES-256-GCM blob at ``<directory>/<key>.enc``; the 256-bit key lives in the OS secret store, never
beside the blob. The key is bound into the authenticated data, so one blob cannot be replayed as
another's. Replacement is atomic (encrypted temp file, fsync, rename). A blob that cannot be
authenticated or decrypted — tampered, truncated, or its key is gone — is discarded rather than
partially reused, which leads to safe bounded re-authentication when the session is next needed.

Supplier sessions live under ``<ICBM_DATA_DIR>/sessions`` (M1). M2 PR-A reuses this store for the
SmartStore token bundle (AUTH.md §11, §12) under the ``marketplace`` namespace: its own directory
and its own secret-store key, so a supplier blob and a marketplace blob can never be confused.
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
MARKETPLACE_SESSIONS_DIR_NAME = "marketplace_sessions"
NAMESPACES = frozenset({"supplier", "marketplace"})
_MAGIC = b"ICBMSESS1"
_NONCE_BYTES = 12


def _checked(key: str) -> str:
    # Supplier and marketplace keys share one syntax; it can never name another path.
    if not SUPPLIER_KEY.fullmatch(key):
        raise ValueError(f"invalid session key: {key!r}")
    return key


class SupplierSessionStore:
    def __init__(
        self, directory: Path, secrets: SecretStore, *, namespace: str = "supplier"
    ) -> None:
        if namespace not in NAMESPACES:
            raise ValueError(f"unknown session namespace: {namespace!r}")
        self._directory = directory
        self._secrets = secrets
        self._namespace = namespace

    def path(self, key: str) -> Path:
        return self._directory / f"{_checked(key)}.enc"

    def _key_name(self, key: str) -> str:
        return f"{self._namespace}:{key}:session_key"

    def _aad(self, key: str) -> bytes:
        return _MAGIC + key.encode("ascii")

    def _key(self, key: str, *, create: bool) -> bytes | None:
        stored = self._secrets.get(self._key_name(key))
        if stored:
            return base64.b64decode(stored)
        if not create:
            return None
        secret = AESGCM.generate_key(bit_length=256)
        self._secrets.set(self._key_name(key), base64.b64encode(secret).decode("ascii"))
        return secret

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def save(self, key: str, payload: bytes) -> None:
        path = self.path(key)
        secret = self._key(key, create=True)
        assert secret is not None
        nonce = os.urandom(_NONCE_BYTES)
        blob = _MAGIC + nonce + AESGCM(secret).encrypt(nonce, payload, self._aad(key))
        self._directory.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(".enc.tmp")
        with staging.open("wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)

    def load(self, key: str) -> bytes | None:
        path = self.path(key)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        secret = self._key(key, create=False)
        if secret is not None and blob.startswith(_MAGIC):
            nonce = blob[len(_MAGIC) : len(_MAGIC) + _NONCE_BYTES]
            with contextlib.suppress(InvalidTag, ValueError):
                return AESGCM(secret).decrypt(
                    nonce, blob[len(_MAGIC) + _NONCE_BYTES :], self._aad(key)
                )
        if self._namespace == "supplier":
            logger.warning("supplier.session.discarded", extra=safe_payload(supplier_key=key))
        else:
            logger.warning("marketplace.session.discarded", extra=safe_payload(marketplace_key=key))
        self.clear(key)
        return None

    def clear(self, key: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path(key).unlink()
