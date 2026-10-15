"""Coupang provider-read proof is secret-held, generation-bound and short-lived."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.platform.core.secrets import MemorySecretStore
from app.stages.connect.coupang.canary_proof import (
    CANARY_ENDPOINT_ID,
    CANARY_PROOF_KEY,
    PROOF_TTL,
    CoupangCanaryProofStore,
)
from integrations.marketplaces.coupang.auth import Credentials

AT = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)


def _credentials(*, generation: int = 3, vendor_id: str = "A00000000") -> Credentials:
    return Credentials("access", "secret", vendor_id, generation)


def test_success_is_one_sanitized_generation_bound_record() -> None:
    secrets = MemorySecretStore()
    store = CoupangCanaryProofStore(secrets)

    proof = store.save_success(credentials=_credentials(), verified_at=AT)

    assert proof.endpoint_id == CANARY_ENDPOINT_ID
    assert store.load_current(credentials=_credentials(), now=AT + timedelta(minutes=1)) == proof
    assert json.loads(secrets.get(CANARY_PROOF_KEY) or "null") == {
        "v": 1,
        "endpoint_id": CANARY_ENDPOINT_ID,
        "credential_generation": 3,
        "vendor_id": "A00000000",
        "verified_at": AT.isoformat(),
    }


@pytest.mark.parametrize(
    "credentials",
    [_credentials(generation=4), _credentials(vendor_id="A11111111")],
)
def test_other_generation_or_vendor_fails_closed(credentials: Credentials) -> None:
    secrets = MemorySecretStore()
    store = CoupangCanaryProofStore(secrets)
    store.save_success(credentials=_credentials(), verified_at=AT)
    with pytest.raises(ValueError, match="CANARY_PROOF_INVALID"):
        store.load_current(credentials=credentials, now=AT)


def test_missing_malformed_future_and_stale_proofs_fail_closed() -> None:
    secrets = MemorySecretStore()
    store = CoupangCanaryProofStore(secrets)
    with pytest.raises(ValueError, match="CANARY_PROOF_MISSING"):
        store.load_current(credentials=_credentials(), now=AT)
    secrets.set(CANARY_PROOF_KEY, "not-json")
    with pytest.raises(ValueError, match="CANARY_PROOF_INVALID"):
        store.load_current(credentials=_credentials(), now=AT)
    store.save_success(credentials=_credentials(), verified_at=AT)
    with pytest.raises(ValueError, match="CANARY_PROOF_STALE"):
        store.load_current(credentials=_credentials(), now=AT - timedelta(seconds=1))
    with pytest.raises(ValueError, match="CANARY_PROOF_STALE"):
        store.load_current(credentials=_credentials(), now=AT + PROOF_TTL + timedelta(seconds=1))
