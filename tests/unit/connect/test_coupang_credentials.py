"""Coupang credentials are one fail-closed OS-secret-store record."""

import json

import pytest

from app.platform.core.secrets import MemorySecretStore
from app.stages.connect.coupang.credentials import (
    COUPANG_CREDENTIAL_KEY,
    CoupangCredentialStore,
)
from integrations.marketplaces.coupang.auth import Credentials

ACCESS_KEY = "fixture-access-key"
SECRET_KEY = "fixture-secret-key"
VENDOR_ID = "A00000000"


def test_bundle_round_trip_is_atomic_and_secret_safe() -> None:
    secrets = MemorySecretStore()
    store = CoupangCredentialStore(secrets)
    credentials = Credentials(ACCESS_KEY, SECRET_KEY, VENDOR_ID, 3)

    store.save(credentials)
    loaded = store.load()

    assert loaded == credentials
    assert ACCESS_KEY not in repr(loaded)
    assert SECRET_KEY not in repr(loaded)
    raw = secrets.get(COUPANG_CREDENTIAL_KEY)
    assert raw is not None
    assert json.loads(raw) == {
        "v": 1,
        "provider": "COUPANG",
        "auth_mode": "HMAC_SHA256",
        "access_key": ACCESS_KEY,
        "secret_key": SECRET_KEY,
        "vendor_id": VENDOR_ID,
        "credential_generation": 3,
    }


@pytest.mark.parametrize(
    "record",
    [
        "not-json",
        "[]",
        '{"v":1,"provider":"COUPANG","auth_mode":"HMAC_SHA256"}',
        (
            '{"v":1,"provider":"OTHER","auth_mode":"HMAC_SHA256",'
            '"access_key":"a","secret_key":"s","vendor_id":"v",'
            '"credential_generation":1}'
        ),
        (
            '{"v":1,"provider":"COUPANG","auth_mode":"HMAC_SHA256",'
            '"access_key":"a","secret_key":"s","vendor_id":"v",'
            '"credential_generation":true}'
        ),
    ],
)
def test_partial_legacy_or_malformed_record_fails_closed(record: str) -> None:
    secrets = MemorySecretStore()
    secrets.set(COUPANG_CREDENTIAL_KEY, record)

    assert CoupangCredentialStore(secrets).load() is None


def test_delete_removes_the_whole_bundle() -> None:
    secrets = MemorySecretStore()
    store = CoupangCredentialStore(secrets)
    store.save(Credentials(ACCESS_KEY, SECRET_KEY, VENDOR_ID, 1))

    store.delete()

    assert store.load() is None
    assert secrets.get(COUPANG_CREDENTIAL_KEY) is None
