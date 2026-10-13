"""Atomic Coupang credential bundles held only by the OS secret store.

Access Key, Secret Key and vendor identity are committed as one record. A partial,
legacy or malformed record reads as absent, so a hybrid credential generation can
never reach the signer.
"""

from __future__ import annotations

import json

from app.platform.core.secrets import SecretStore
from integrations.marketplaces.coupang.auth import Credentials

COUPANG_PROVIDER = "COUPANG"
COUPANG_AUTH_MODE = "HMAC_SHA256"
COUPANG_CREDENTIAL_KEY = "marketplace:coupang:credentials"
_FORMAT = 1


class CoupangCredentialStore:
    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def save(self, credentials: Credentials) -> None:
        record = json.dumps(
            {
                "v": _FORMAT,
                "provider": COUPANG_PROVIDER,
                "auth_mode": COUPANG_AUTH_MODE,
                "access_key": credentials.access_key,
                "secret_key": credentials.secret_key,
                "vendor_id": credentials.vendor_id,
                "credential_generation": credentials.credential_generation,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._secrets.set(COUPANG_CREDENTIAL_KEY, record)

    def load(self) -> Credentials | None:
        raw = self._secrets.get(COUPANG_CREDENTIAL_KEY)
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(record, dict) or (
            record.get("v"),
            record.get("provider"),
            record.get("auth_mode"),
        ) != (_FORMAT, COUPANG_PROVIDER, COUPANG_AUTH_MODE):
            return None
        access_key = record.get("access_key")
        secret_key = record.get("secret_key")
        vendor_id = record.get("vendor_id")
        generation = record.get("credential_generation")
        if not (
            isinstance(access_key, str)
            and isinstance(secret_key, str)
            and isinstance(vendor_id, str)
            and isinstance(generation, int)
            and not isinstance(generation, bool)
        ):
            return None
        try:
            return Credentials(access_key, secret_key, vendor_id, generation)
        except ValueError:
            return None

    def delete(self) -> None:
        self._secrets.delete(COUPANG_CREDENTIAL_KEY)
