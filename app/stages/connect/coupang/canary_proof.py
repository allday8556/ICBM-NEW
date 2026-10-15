"""Persist only sanitized Coupang canary success in the OS secret store."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.platform.core.secrets import SecretStore
from integrations.marketplaces.coupang.auth import (
    PROVIDER_READ_PROOF_ID,
    PROVIDER_READ_PROOF_TTL,
    Credentials,
    ProviderReadProof,
)

CANARY_PROOF_KEY = "marketplace:coupang:provider-read-proof-v1"
CANARY_ENDPOINT_ID = PROVIDER_READ_PROOF_ID
PROOF_TTL = PROVIDER_READ_PROOF_TTL
_FORMAT = 1


class CoupangCanaryProofStore:
    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    @staticmethod
    def _aware(value: datetime, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)

    def save_success(self, *, credentials: Credentials, verified_at: datetime) -> ProviderReadProof:
        proof = ProviderReadProof.create(
            CANARY_ENDPOINT_ID,
            credential_generation=credentials.credential_generation,
            verified_at=verified_at,
        )
        self._secrets.set(
            CANARY_PROOF_KEY,
            json.dumps(
                {
                    "v": _FORMAT,
                    "endpoint_id": proof.endpoint_id,
                    "credential_generation": proof.credential_generation,
                    "vendor_id": credentials.vendor_id,
                    "verified_at": proof.verified_at.isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        return proof

    def load_current(self, *, credentials: Credentials, now: datetime) -> ProviderReadProof:
        current = self._aware(now, "now")
        raw = self._secrets.get(CANARY_PROOF_KEY)
        if not raw:
            raise ValueError("COUPANG_CANARY_PROOF_MISSING")
        try:
            record = json.loads(raw)
            if (
                not isinstance(record, dict)
                or record.get("v") != _FORMAT
                or record.get("endpoint_id") != CANARY_ENDPOINT_ID
                or record.get("credential_generation") != credentials.credential_generation
                or record.get("vendor_id") != credentials.vendor_id
            ):
                raise ValueError
            proof = ProviderReadProof.create(
                record["endpoint_id"],
                credential_generation=record["credential_generation"],
                verified_at=datetime.fromisoformat(record["verified_at"]),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("COUPANG_CANARY_PROOF_INVALID") from None
        age = current - proof.verified_at
        if age < timedelta(0) or age > PROOF_TTL:
            raise ValueError("COUPANG_CANARY_PROOF_STALE")
        return proof


__all__ = [
    "CANARY_ENDPOINT_ID",
    "CANARY_PROOF_KEY",
    "PROOF_TTL",
    "CoupangCanaryProofStore",
]
