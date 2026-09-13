"""A login is one secret-store record; anything partial reads as no credentials (PR #9 comments
5654839475 and 5654916026)."""

import pytest

from app.connect.credentials import SupplierCredentialStore
from app.core.secrets import MemorySecretStore
from integrations.suppliers.base import Credentials

KEY = "kmretail"
LOGIN = Credentials(username="operator-id", password="pass•word with • inside")


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore()


def test_a_login_is_persisted_as_one_record(secrets: MemorySecretStore) -> None:
    store = SupplierCredentialStore(secrets)
    store.save(KEY, LOGIN)
    assert set(secrets._values) == {f"supplier:{KEY}:credentials"}
    assert store.load(KEY) == LOGIN
    assert store.username(KEY) == "operator-id"


@pytest.mark.parametrize(
    "entries",
    [
        {"username": "operator-id"},
        {"password": "secret"},
        {"username": "new-id", "password": "old-password"},  # legacy split pair: never trusted
    ],
    ids=["username-only", "password-only", "legacy-pair"],
)
def test_split_or_partial_entries_are_not_credentials(
    secrets: MemorySecretStore, entries: dict[str, str]
) -> None:
    for part, value in entries.items():
        secrets.set(f"supplier:{KEY}:{part}", value)
    store = SupplierCredentialStore(secrets)
    assert store.load(KEY) is None
    assert not store.stored(KEY)
    assert store.username(KEY) is None


@pytest.mark.parametrize(
    "record",
    [
        "not json",
        '{"v": 2, "username": "a", "password": "b"}',
        '{"v": 1, "username": "a"}',
        '{"v": 1, "username": "", "password": "b"}',
        '{"v": 1, "username": "a", "password": 7}',
        "[1, 2]",
    ],
)
def test_malformed_records_fail_closed(secrets: MemorySecretStore, record: str) -> None:
    secrets.set(f"supplier:{KEY}:credentials", record)
    assert SupplierCredentialStore(secrets).load(KEY) is None


def test_saving_removes_legacy_split_entries(secrets: MemorySecretStore) -> None:
    secrets.set(f"supplier:{KEY}:username", "old-id")
    secrets.set(f"supplier:{KEY}:password", "old-password")
    SupplierCredentialStore(secrets).save(KEY, LOGIN)
    assert set(secrets._values) == {f"supplier:{KEY}:credentials"}


def test_a_record_needs_both_halves(secrets: MemorySecretStore) -> None:
    with pytest.raises(ValueError):
        SupplierCredentialStore(secrets).save(KEY, Credentials(username="id", password=""))
    assert secrets._values == {}
