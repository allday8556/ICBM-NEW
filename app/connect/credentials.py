"""Supplier credentials in the OS secret store (Issue #7 §1; PR #9 comments 5654839475, 5654916026).

A login is persisted as **one** secret-store record holding the username and the password
together, so replacing it is one logical write and a half-written pair cannot exist. Anything
that is not a complete, well-formed record — including the separate username/password entries an
earlier build wrote — reads as "no credentials" and can never drive a real login.

Credentials never reach the database, logs, audit payloads or API responses; this class is the
only reader, and it hands them to an adapter only for the duration of one authentication.
"""

import contextlib
import json

from app.core.secrets import SecretStore
from integrations.suppliers.base import SUPPLIER_KEY, Credentials

_FORMAT = 1
# Written by the first M1 build as two separate entries; never trusted, removed on the next save.
_LEGACY_PARTS = ("username", "password")


def _name(supplier_key: str, part: str = "credentials") -> str:
    if not SUPPLIER_KEY.fullmatch(supplier_key):
        raise ValueError(f"invalid supplier key: {supplier_key!r}")
    return f"supplier:{supplier_key}:{part}"


class SupplierCredentialStore:
    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def save(self, supplier_key: str, credentials: Credentials) -> None:
        """Replace the login in one write. Legacy split entries are removed afterwards."""
        if not credentials.username or not credentials.password:
            raise ValueError("username and password are both required")
        record = json.dumps(
            {"v": _FORMAT, "username": credentials.username, "password": credentials.password},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._secrets.set(_name(supplier_key), record)
        for part in _LEGACY_PARTS:
            with contextlib.suppress(Exception):
                self._secrets.delete(_name(supplier_key, part))

    def load(self, supplier_key: str) -> Credentials | None:
        raw = self._secrets.get(_name(supplier_key))
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("v") != _FORMAT:
            return None
        username, password = record.get("username"), record.get("password")
        if not (isinstance(username, str) and username and isinstance(password, str) and password):
            return None
        return Credentials(username=username, password=password)

    def stored(self, supplier_key: str) -> bool:
        return self.load(supplier_key) is not None

    def username(self, supplier_key: str) -> str | None:
        """The saved login ID, read on demand for the operator's own loopback UI only."""
        credentials = self.load(supplier_key)
        return credentials.username if credentials else None
