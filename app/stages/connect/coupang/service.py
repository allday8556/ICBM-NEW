"""Coupang credential ownership for the local Settings surface.

The UI and CLI share one atomic OS-secret-store record.  This service exposes only
configured/generation status and never returns credential material to an API caller.
"""

from dataclasses import dataclass

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.errors import InputValidationError
from app.platform.core.safe_payload import safe_payload
from app.platform.core.secrets import SecretStore
from app.stages.connect.coupang.credentials import CoupangCredentialStore
from integrations.marketplaces.coupang.auth import Credentials

MARKETPLACE_KEY = "coupang"
_TARGET = "marketplace:coupang"


@dataclass(frozen=True)
class CoupangCredentialStatus:
    configured: bool
    credential_generation: int | None


class CoupangCredentialService:
    def __init__(self, secrets: SecretStore, audit: AuditLog) -> None:
        self._store = CoupangCredentialStore(secrets)
        self._audit = audit

    def status(self) -> CoupangCredentialStatus:
        current = self._store.load()
        return CoupangCredentialStatus(
            configured=current is not None,
            credential_generation=None if current is None else current.credential_generation,
        )

    def save_credentials(
        self, vendor_id: str, access_key: str, secret_key: str, *, actor: str
    ) -> CoupangCredentialStatus:
        current = self._store.load()
        generation = 1 if current is None else current.credential_generation + 1
        try:
            credentials = Credentials(access_key.strip(), secret_key, vendor_id.strip(), generation)
        except ValueError as exc:
            raise InputValidationError(
                "COUPANG_CREDENTIALS_INVALID",
                "vendor id, access key and secret key must all be valid",
            ) from exc
        self._store.save(credentials)
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.MARKETPLACE_CREDENTIALS_UPDATED,
                action="SAVE_CREDENTIALS",
                actor=actor,
                outcome=AuditOutcome.ALLOWED,
                target_ref=_TARGET,
                details=safe_payload(
                    marketplace_key=MARKETPLACE_KEY,
                    credential_generation=generation,
                ),
            )
        )
        return CoupangCredentialStatus(True, generation)
