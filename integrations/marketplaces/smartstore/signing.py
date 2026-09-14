"""The canonical SmartStore token request (AUTH.md §3, §4, §21).

This is the single builder of the SELF token form. It produces exactly
``client_id, timestamp, client_secret_sign, grant_type=client_credentials, type=SELF``. It never
includes ``account_id``, and the values travel in the form body, never a query string.

The signature is derived on demand and never persisted (AUTH §4, §10).
"""

import base64
from dataclasses import dataclass, field

import bcrypt

from integrations.marketplaces.smartstore.registry import AUTH_MODE

GRANT_TYPE = "client_credentials"
TOKEN_FORM_FIELDS = frozenset(
    {"client_id", "timestamp", "client_secret_sign", "grant_type", "type"}
)


@dataclass(frozen=True)
class ApplicationCredentials:
    """One committed credential bundle (AUTH §11): the client id, the client secret and the
    generation they were committed under."""

    client_id: str
    client_secret: str = field(repr=False)
    credential_generation: int

    def __post_init__(self) -> None:
        if not (self.client_id.strip() and self.client_secret.strip()):
            raise ValueError("a SmartStore credential bundle needs a client id and a client secret")
        if isinstance(self.credential_generation, bool) or self.credential_generation < 1:
            raise ValueError("a committed credential generation is a positive integer")


class SignatureError(ValueError):
    """The client secret cannot produce a signature: a local, pre-submit failure."""


def client_secret_sign(client_id: str, client_secret: str, timestamp_ms: int) -> str:
    """AUTH §4: bcrypt of ``client_id + "_" + timestamp``, using the client secret as the salt,
    Base64-encoded. The timestamp is the same millisecond value that is sent (§5)."""
    password = f"{client_id}_{timestamp_ms}".encode()
    try:
        hashed = bcrypt.hashpw(password, client_secret.encode("utf-8"))
    except ValueError as exc:
        # The message names no secret material.
        raise SignatureError("the client secret is not a usable bcrypt salt") from exc
    return base64.standard_b64encode(hashed).decode("ascii")


def token_form(credentials: ApplicationCredentials, timestamp_ms: int) -> dict[str, str]:
    """The complete SELF token form body (AUTH §3)."""
    if isinstance(timestamp_ms, bool) or timestamp_ms <= 0:
        raise ValueError("the timestamp is a positive millisecond Unix time")
    return {
        "client_id": credentials.client_id,
        "timestamp": str(timestamp_ms),
        "client_secret_sign": client_secret_sign(
            credentials.client_id, credentials.client_secret, timestamp_ms
        ),
        "grant_type": GRANT_TYPE,
        "type": AUTH_MODE,
    }
