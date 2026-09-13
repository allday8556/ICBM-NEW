from typing import Any

import pytest

from app.core.secrets import KeyringSecretStore, MemorySecretStore


class _FakeKeyring:
    """Stands in for a keyring backend so tests never touch the OS credential store."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.values.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.values[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        import keyring.errors

        if (service, key) not in self.values:
            raise keyring.errors.PasswordDeleteError(key)
        del self.values[(service, key)]


def test_keyring_store_round_trip_is_namespaced() -> None:
    backend = _FakeKeyring()
    store = KeyringSecretStore(backend=backend)
    store.set("supplier:kwholesale:password", "s3cret")
    assert backend.values == {("ICBM-NEW", "supplier:kwholesale:password"): "s3cret"}
    assert store.get("supplier:kwholesale:password") == "s3cret"
    store.delete("supplier:kwholesale:password")
    store.delete("supplier:kwholesale:password")  # idempotent
    assert store.get("supplier:kwholesale:password") is None


def test_non_viable_backend_is_reported_unavailable() -> None:
    fail_backend: Any = type("Keyring", (), {"__module__": "keyring.backends.fail"})()
    status = KeyringSecretStore(backend=fail_backend).status()
    assert status.available is False
    assert status.backend == "keyring.backends.fail.Keyring"


def test_viable_backend_is_reported_available() -> None:
    assert KeyringSecretStore(backend=_FakeKeyring()).status().available is True


@pytest.mark.parametrize("key", ["", "x" * 201, "line\nbreak"])
def test_invalid_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError):
        MemorySecretStore().set(key, "v")
