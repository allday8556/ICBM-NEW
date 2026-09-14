"""SmartStore credential and session storage (AUTH.md §10-§12, §19; M2 PR-A)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.connect.sessions import SupplierSessionStore
from app.connect.smartstore.credentials import ApplicationCredentialStore
from app.connect.smartstore.service import CommittedSession
from app.core.secrets import MemorySecretStore
from integrations.marketplaces.smartstore import registry
from integrations.marketplaces.smartstore.signing import ApplicationCredentials

SECRET = "$2a$04$abcdefghijklmnopqrstuu"
NOW = datetime(2026, 9, 15, tzinfo=UTC)
SESSION = CommittedSession(
    access_token="fixture-token",
    token_type="Bearer",
    expires_in=10800,
    expires_at=NOW + timedelta(seconds=10800),
    credential_generation=2,
    session_generation=5,
    committed_at=NOW,
)


def test_the_provider_binding_matches_the_endpoint_registry() -> None:
    from app.connect.marketplace.attestation import SELF_AUTH_MODE, SMARTSTORE_PROVIDER

    assert (SMARTSTORE_PROVIDER, SELF_AUTH_MODE) == (registry.PROVIDER, registry.AUTH_MODE)


# ---------------------------------------------------------------- credential bundle


def test_the_credential_bundle_is_one_record() -> None:
    secrets = MemorySecretStore()
    store = ApplicationCredentialStore(secrets)
    bundle = ApplicationCredentials("fixture-client-id", SECRET, 4)
    store.save("smartstore", bundle)
    assert store.load("smartstore") == bundle
    record = json.loads(secrets.get("marketplace:smartstore:credentials") or "")
    assert (record["provider"], record["auth_mode"], record["credential_generation"]) == (
        "SMARTSTORE",
        "SELF",
        4,
    )


@pytest.mark.parametrize(
    "record",
    [
        "not json",
        "[]",
        '{"v": 2, "provider": "SMARTSTORE", "auth_mode": "SELF", "client_id": "c",'
        ' "client_secret": "s", "credential_generation": 1}',
        '{"v": 1, "provider": "SMARTSTORE", "auth_mode": "SELLER", "client_id": "c",'
        ' "client_secret": "s", "credential_generation": 1}',
        '{"v": 1, "provider": "SMARTSTORE", "auth_mode": "SELF", "client_id": "c",'
        ' "credential_generation": 1}',
        '{"v": 1, "provider": "SMARTSTORE", "auth_mode": "SELF", "client_id": "c",'
        ' "client_secret": "s", "credential_generation": 0}',
        '{"v": 1, "provider": "SMARTSTORE", "auth_mode": "SELF", "client_id": "c",'
        ' "client_secret": "s", "credential_generation": true}',
    ],
)
def test_an_incomplete_bundle_reads_as_no_credentials(record: str) -> None:
    # AUTH §19: a partial or foreign record is never a credential generation.
    secrets = MemorySecretStore()
    secrets.set("marketplace:smartstore:credentials", record)
    assert ApplicationCredentialStore(secrets).load("smartstore") is None


# ---------------------------------------------------------------- token bundle


def test_the_token_bundle_round_trips() -> None:
    assert CommittedSession.decode(SESSION.encode()) == SESSION
    assert "fixture-token" not in repr(SESSION)


@pytest.mark.parametrize(
    "change",
    [
        {"v": 2},
        {"provider": "OTHER"},
        {"auth_mode": "SELLER"},
        {"access_token": ""},
        {"token_type": 1},
        {"expires_in": 0},
        {"session_generation": 0},
        {"credential_generation": True},
        {"expires_at": "2026-09-15T03:00:00"},  # naive: not a committed timestamp
        {"committed_at": "yesterday"},
    ],
)
def test_a_partial_or_foreign_token_bundle_is_never_a_session(change: dict[str, object]) -> None:
    record = json.loads(SESSION.encode()) | change
    assert CommittedSession.decode(json.dumps(record).encode()) is None


@pytest.mark.parametrize("missing", ["access_token", "session_generation", "committed_at"])
def test_a_bundle_missing_a_field_is_never_a_session(missing: str) -> None:
    record = json.loads(SESSION.encode())
    del record[missing]
    assert CommittedSession.decode(json.dumps(record).encode()) is None


# ---------------------------------------------------------------- the shared session store


def test_supplier_and_marketplace_sessions_never_share_a_key(tmp_path: Path) -> None:
    secrets = MemorySecretStore()
    supplier = SupplierSessionStore(tmp_path, secrets)
    marketplace = SupplierSessionStore(tmp_path, secrets, namespace="marketplace")
    marketplace.save("smartstore", b"marketplace-session")
    assert secrets.get("marketplace:smartstore:session_key")
    assert secrets.get("supplier:smartstore:session_key") is None
    # The supplier namespace cannot decrypt the marketplace blob at the same path.
    assert supplier.load("smartstore") is None


def test_the_supplier_namespace_keeps_its_m1_key_name(tmp_path: Path) -> None:
    secrets = MemorySecretStore()
    SupplierSessionStore(tmp_path, secrets).save("kmretail", b"session")
    assert secrets.get("supplier:kmretail:session_key")


def test_an_unknown_session_namespace_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SupplierSessionStore(tmp_path, MemorySecretStore(), namespace="other")
