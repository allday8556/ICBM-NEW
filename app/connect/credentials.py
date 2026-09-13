"""Supplier credentials in the OS secret store (Issue #7 §1).

Credentials never reach the database, logs, audit payloads or API responses; this class is the
only reader, and it hands them to an adapter only for the duration of one authentication.
"""

from app.core.secrets import SecretStore
from integrations.suppliers.base import SUPPLIER_KEY, Credentials


def _name(supplier_key: str, part: str) -> str:
    if not SUPPLIER_KEY.fullmatch(supplier_key):
        raise ValueError(f"invalid supplier key: {supplier_key!r}")
    return f"supplier:{supplier_key}:{part}"


class SupplierCredentialStore:
    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def save(self, supplier_key: str, credentials: Credentials) -> None:
        if not credentials.username or not credentials.password:
            raise ValueError("username and password are both required")
        self._secrets.set(_name(supplier_key, "username"), credentials.username)
        self._secrets.set(_name(supplier_key, "password"), credentials.password)

    def load(self, supplier_key: str) -> Credentials | None:
        username = self._secrets.get(_name(supplier_key, "username"))
        password = self._secrets.get(_name(supplier_key, "password"))
        if not username or not password:
            return None
        return Credentials(username=username, password=password)

    def stored(self, supplier_key: str) -> bool:
        return self.load(supplier_key) is not None

    def username(self, supplier_key: str) -> str | None:
        """The saved login ID, read on demand for the operator's own loopback UI only."""
        return self._secrets.get(_name(supplier_key, "username")) or None
