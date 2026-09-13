"""OS-native secret storage boundary (ADR-0001: keyring / Windows credential protection).

M0 defines the boundary only; nothing in M0 reads or writes a credential. Secrets never pass
through the database, logs, AI requests or UI responses — callers obtain values only through
this interface, and the first real use arrives with supplier CONNECT (M1).
"""

import contextlib
from dataclasses import dataclass
from typing import Any, Protocol

import keyring
import keyring.errors

SERVICE_NAME = "ICBM-NEW"
_NON_VIABLE_BACKENDS = ("keyring.backends.fail", "keyring.backends.null")


@dataclass(frozen=True)
class SecretStoreStatus:
    backend: str
    available: bool
    detail: str


class SecretStore(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...

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

    def status(self) -> SecretStoreStatus:
        return SecretStoreStatus(backend="memory", available=True, detail="non-persistent")


def build_secret_store(backend: str) -> SecretStore:
    if backend == "memory":
        return MemorySecretStore()
    return KeyringSecretStore()
