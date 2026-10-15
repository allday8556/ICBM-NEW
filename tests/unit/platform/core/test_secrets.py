import hashlib
from typing import Any

import pytest

from app.platform.core.secrets import KeyringSecretStore, MemorySecretStore


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


class _BoundedFakeKeyring(_FakeKeyring):
    def set_password(self, service: str, key: str, value: str) -> None:
        if len(value.encode("utf-16-le")) > 2048:
            raise OSError("credential blob too large")
        super().set_password(service, key, value)


def test_keyring_store_round_trip_is_namespaced() -> None:
    backend = _FakeKeyring()
    store = KeyringSecretStore(backend=backend)
    store.set("supplier:kwholesale:password", "s3cret")
    assert backend.values == {("ICBM-NEW", "supplier:kwholesale:password"): "s3cret"}
    assert store.get("supplier:kwholesale:password") == "s3cret"
    store.delete("supplier:kwholesale:password")
    store.delete("supplier:kwholesale:password")  # idempotent
    assert store.get("supplier:kwholesale:password") is None


def test_keyring_large_record_is_chunked_and_round_trips() -> None:
    backend = _BoundedFakeKeyring()
    store = KeyringSecretStore(backend=backend)
    value = "category-metadata-" + "가나다라마바사" * 5000

    store.set_large("marketplace:coupang:category-metadata-v1", value)

    assert store.get_large("marketplace:coupang:category-metadata-v1") == value
    assert all(len(item.encode("utf-16-le")) <= 2048 for item in backend.values.values())
    store.delete_large("marketplace:coupang:category-metadata-v1")
    assert backend.values == {}


def test_keyring_large_record_accepts_and_replaces_a_legacy_direct_value() -> None:
    backend = _BoundedFakeKeyring()
    store = KeyringSecretStore(backend=backend)
    key = "marketplace:coupang:category-metadata-v1"
    legacy = '{"v":1,"credential_generation":2,"results":{},"failures":{}}'
    store.set(key, legacy)

    assert store.get_large(key) == legacy
    store.set_large(key, "replacement-" + "가나다" * 3000)
    assert store.get_large(key).startswith("replacement-")


def test_keyring_large_record_swap_keeps_the_old_value_when_a_chunk_write_fails() -> None:
    backend = _BoundedFakeKeyring()
    store = KeyringSecretStore(backend=backend)
    key = "marketplace:coupang:category-metadata-v1"
    store.set_large(key, "old-" + "가나다" * 2000)
    original = store.get_large(key)
    original_set = backend.set_password
    calls = 0

    def fail_second_chunk(service: str, item_key: str, value: str) -> None:
        nonlocal calls
        if ":large-v1:" in item_key:
            calls += 1
            if calls == 2:
                raise OSError("simulated keyring failure")
        original_set(service, item_key, value)

    backend.set_password = fail_second_chunk  # type: ignore[method-assign]
    new_value = "new-" + "".join(
        hashlib.sha256(str(index).encode()).hexdigest() for index in range(1000)
    )
    with pytest.raises(OSError, match="simulated"):
        store.set_large(key, new_value)

    assert store.get_large(key) == original


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
