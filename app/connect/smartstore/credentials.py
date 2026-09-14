"""SmartStore application credentials in the OS secret store (AUTH.md §10, §11, §18, §19).

The bundle — provider, auth mode, client_id, client_secret and the credential generation it was
committed under — is **one** secret-store record, so replacing it is one write and a hybrid bundle
cannot exist (§19). Anything that is not a complete, well-formed record reads as "no credentials".
The store is the shared M1 ``SecretStore``; only this record shape is new.

Credentials never reach the database, logs, audit payloads or API responses.
"""

import json

from app.connect.marketplace.attestation import SELF_AUTH_MODE, SMARTSTORE_PROVIDER
from app.core.secrets import SecretStore
from integrations.marketplaces.smartstore.signing import ApplicationCredentials

_FORMAT = 1


def _name(marketplace_key: str) -> str:
    return f"marketplace:{marketplace_key}:credentials"


class ApplicationCredentialStore:
    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def save(self, marketplace_key: str, credentials: ApplicationCredentials) -> None:
        record = json.dumps(
            {
                "v": _FORMAT,
                "provider": SMARTSTORE_PROVIDER,
                "auth_mode": SELF_AUTH_MODE,
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "credential_generation": credentials.credential_generation,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._secrets.set(_name(marketplace_key), record)

    def load(self, marketplace_key: str) -> ApplicationCredentials | None:
        raw = self._secrets.get(_name(marketplace_key))
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
        ) != (_FORMAT, SMARTSTORE_PROVIDER, SELF_AUTH_MODE):
            return None
        client_id, client_secret = record.get("client_id"), record.get("client_secret")
        generation = record.get("credential_generation")
        if not (
            isinstance(client_id, str)
            and isinstance(client_secret, str)
            and isinstance(generation, int)
            and not isinstance(generation, bool)
        ):
            return None
        try:
            return ApplicationCredentials(client_id, client_secret, generation)
        except ValueError:
            return None
