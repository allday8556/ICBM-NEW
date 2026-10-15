"""OS-native secret storage boundary (ADR-0001: keyring / Windows credential protection).

M0 defines the boundary only; nothing in M0 reads or writes a credential. Secrets never pass
through the database, logs, AI requests or UI responses — callers obtain values only through
this interface, and the first real use arrives with supplier CONNECT (M1).
"""

import base64
import contextlib
import json
import re
import secrets as stdlib_secrets
import zlib
from dataclasses import dataclass
from typing import Any, Protocol

import keyring
import keyring.errors

SERVICE_NAME = "ICBM-NEW"
_NON_VIABLE_BACKENDS = ("keyring.backends.fail", "keyring.backends.null")
_LARGE_SECRET_CHUNK_CHARS = 768
_LARGE_SECRET_MAX_CHUNKS = 4096
_LARGE_SECRET_MAX_BYTES = 2 * 1024 * 1024
_LARGE_SECRET_TOKEN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class SecretStoreStatus:
    backend: str
    available: bool
    detail: str


class SecretStore(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def get_large(self, key: str) -> str | None: ...

    def set_large(self, key: str, value: str) -> None: ...

    def delete_large(self, key: str) -> None: ...

    def status(self) -> SecretStoreStatus: ...


def _check_key(key: str) -> str:
    if not key or len(key) > 200 or not key.isprintable():
        raise ValueError("secret key must be 1-200 printable characters")
    return key


class KeyringSecretStore:
    """Secrets held by the OS credential store (Windows Credential Manager on the v1 target)."""

    def __init__(self, service: str = SERVICE_NAME, backend: Any | None = None) -> None:
        self._service = service
        self._backend = backend

    def _keyring(self) -> Any:
        return self._backend if self._backend is not None else keyring.get_keyring()

    def get(self, key: str) -> str | None:
        value = self._keyring().get_password(self._service, _check_key(key))
        return value if isinstance(value, str) else None

    def set(self, key: str, value: str) -> None:
        self._keyring().set_password(self._service, _check_key(key), value)

    def delete(self, key: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            self._keyring().delete_password(self._service, _check_key(key))

    @staticmethod
    def _chunk_key(key: str, token: str, index: int) -> str:
        return _check_key(f"{key}:large-v1:{token}:{index}")

    @staticmethod
    def _manifest(raw: str | None) -> tuple[str, int] | None:
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(value, dict) or "codec" not in value:
            return None
        token, chunks = value.get("token"), value.get("chunks")
        if (
            value.get("v") != 1
            or value.get("codec") != "zlib+base64"
            or not isinstance(token, str)
            or _LARGE_SECRET_TOKEN.fullmatch(token) is None
            or not isinstance(chunks, int)
            or isinstance(chunks, bool)
            or not 1 <= chunks <= _LARGE_SECRET_MAX_CHUNKS
        ):
            raise ValueError("large secret manifest is invalid")
        return token, chunks

    def _delete_chunks(self, key: str, manifest: tuple[str, int] | None) -> None:
        if manifest is None:
            return
        token, chunks = manifest
        for index in range(chunks):
            with contextlib.suppress(keyring.errors.KeyringError, OSError):
                self.delete(self._chunk_key(key, token, index))

    def get_large(self, key: str) -> str | None:
        raw = self.get(key)
        manifest = self._manifest(raw)
        if manifest is None:
            return raw
        token, chunks = manifest
        encoded_parts: list[str] = []
        for index in range(chunks):
            part = self.get(self._chunk_key(key, token, index))
            if part is None or len(part) > _LARGE_SECRET_CHUNK_CHARS:
                raise ValueError("large secret chunks are incomplete")
            encoded_parts.append(part)
        try:
            compressed = base64.b64decode("".join(encoded_parts), validate=True)
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(compressed, _LARGE_SECRET_MAX_BYTES + 1)
            if len(decoded) > _LARGE_SECRET_MAX_BYTES or decoder.unconsumed_tail:
                raise ValueError("large secret exceeds the safe size limit")
            decoded += decoder.flush(_LARGE_SECRET_MAX_BYTES + 1 - len(decoded))
            if len(decoded) > _LARGE_SECRET_MAX_BYTES or not decoder.eof:
                raise ValueError("large secret exceeds the safe size limit")
            return decoded.decode("utf-8")
        except (UnicodeDecodeError, ValueError, zlib.error) as exc:
            raise ValueError("large secret payload is invalid") from exc

    def set_large(self, key: str, value: str) -> None:
        _check_key(key)
        decoded = value.encode("utf-8")
        if len(decoded) > _LARGE_SECRET_MAX_BYTES:
            raise ValueError("large secret exceeds the safe size limit")
        encoded = base64.b64encode(zlib.compress(decoded, level=9)).decode("ascii")
        chunks = [
            encoded[index : index + _LARGE_SECRET_CHUNK_CHARS]
            for index in range(0, len(encoded), _LARGE_SECRET_CHUNK_CHARS)
        ]
        if not chunks or len(chunks) > _LARGE_SECRET_MAX_CHUNKS:
            raise ValueError("large secret exceeds the safe size limit")
        token = stdlib_secrets.token_hex(16)
        old_manifest = self._manifest(self.get(key))
        written = 0
        try:
            for index, part in enumerate(chunks):
                self.set(self._chunk_key(key, token, index), part)
                written += 1
            self.set(
                key,
                json.dumps(
                    {"v": 1, "codec": "zlib+base64", "token": token, "chunks": len(chunks)},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        except Exception:
            self._delete_chunks(key, (token, written) if written else None)
            raise
        self._delete_chunks(key, old_manifest)

    def delete_large(self, key: str) -> None:
        manifest = self._manifest(self.get(key))
        self.delete(key)
        self._delete_chunks(key, manifest)

    def status(self) -> SecretStoreStatus:
        backend = self._keyring()
        name = f"{type(backend).__module__}.{type(backend).__qualname__}"
        available = not name.startswith(_NON_VIABLE_BACKENDS)
        detail = "OS credential store" if available else "no viable OS credential store"
        return SecretStoreStatus(backend=name, available=available, detail=detail)


class MemorySecretStore:
    """Non-persistent store for tests and CI hosts without an OS credential store."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._values.get(_check_key(key))

    def set(self, key: str, value: str) -> None:
        self._values[_check_key(key)] = value

    def delete(self, key: str) -> None:
        self._values.pop(_check_key(key), None)

    def get_large(self, key: str) -> str | None:
        return self.get(key)

    def set_large(self, key: str, value: str) -> None:
        self.set(key, value)

    def delete_large(self, key: str) -> None:
        self.delete(key)

    def status(self) -> SecretStoreStatus:
        return SecretStoreStatus(backend="memory", available=True, detail="non-persistent")


def build_secret_store(backend: str) -> SecretStore:
    if backend == "memory":
        return MemorySecretStore()
    return KeyringSecretStore()
