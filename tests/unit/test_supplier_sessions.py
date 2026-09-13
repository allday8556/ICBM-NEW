import base64
from pathlib import Path

import pytest

from app.connect.sessions import SupplierSessionStore
from app.core.secrets import MemorySecretStore

PAYLOAD = b'{"cookie":"SECRET-COOKIE-VALUE-4411"}'


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
def store(tmp_path: Path, secrets: MemorySecretStore) -> SupplierSessionStore:
    return SupplierSessionStore(tmp_path / "sessions", secrets)


def test_round_trip_leaves_no_plaintext_at_rest(
    store: SupplierSessionStore, secrets: MemorySecretStore, tmp_path: Path
) -> None:
    store.save("kmretail", PAYLOAD)
    blob = (tmp_path / "sessions" / "kmretail.enc").read_bytes()
    assert b"SECRET-COOKIE-VALUE" not in blob
    assert base64.b64encode(PAYLOAD) not in blob
    assert store.load("kmretail") == PAYLOAD
    key = secrets.get("supplier:kmretail:session_key")
    assert key is not None and key.encode() not in blob, "the key lives in the secret store only"


def test_a_blob_cannot_be_replayed_as_another_supplier(
    store: SupplierSessionStore, tmp_path: Path, secrets: MemorySecretStore
) -> None:
    store.save("kmretail", PAYLOAD)
    store.save("other", b"x")
    directory = tmp_path / "sessions"
    (directory / "other.enc").write_bytes((directory / "kmretail.enc").read_bytes())
    assert store.load("other") is None
    assert not (directory / "other.enc").exists(), "an unreadable blob is discarded"


def test_a_tampered_blob_is_discarded(store: SupplierSessionStore, tmp_path: Path) -> None:
    store.save("kmretail", PAYLOAD)
    path = tmp_path / "sessions" / "kmretail.enc"
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0x01
    path.write_bytes(bytes(blob))
    assert store.load("kmretail") is None
    assert not path.exists()


def test_a_blob_whose_key_is_gone_is_discarded(
    store: SupplierSessionStore, secrets: MemorySecretStore, tmp_path: Path
) -> None:
    store.save("kmretail", PAYLOAD)
    secrets.delete("supplier:kmretail:session_key")
    assert store.load("kmretail") is None
    assert not (tmp_path / "sessions" / "kmretail.enc").exists()


def test_replacement_is_atomic_and_leaves_no_temporary_file(
    store: SupplierSessionStore, tmp_path: Path
) -> None:
    store.save("kmretail", PAYLOAD)
    store.save("kmretail", b"second")
    assert sorted(p.name for p in (tmp_path / "sessions").iterdir()) == ["kmretail.enc"]
    assert store.load("kmretail") == b"second"


def test_absent_session_reads_as_none_and_clear_is_idempotent(
    store: SupplierSessionStore,
) -> None:
    assert store.load("kmretail") is None
    store.clear("kmretail")
    assert not store.exists("kmretail")


@pytest.mark.parametrize("key", ["../escape", "Upper", "", "a/b", "x" * 41])
def test_supplier_keys_cannot_name_other_paths(store: SupplierSessionStore, key: str) -> None:
    with pytest.raises(ValueError):
        store.save(key, PAYLOAD)
