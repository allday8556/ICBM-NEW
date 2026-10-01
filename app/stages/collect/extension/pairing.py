"""The extension pairing owner (ADR-0019 §3, AC-06; ruling ``5906290729`` B-2).

Pairing is explicit and operator-initiated, and it is **added to** the loopback binding and the
``X-ICBM-Client`` check; it replaces neither. The server owns one per-extension secret in the OS
keyring. There is no database table and no file: a pairing that is not in the keyring does not
exist.

- ``pair`` issues a new pairing for one pinned extension identity and returns the one-time pairing
  code the operator pastes into the extension. It is refused while a pairing exists.
- ``rotate`` creates a new generation with a new secret; the prior generation stops verifying.
- ``revoke`` removes the pairing.

Every request is verified by :meth:`ExtensionPairing.verify`: the pinned extension identity, the
pairing id and generation, a timestamp inside the accepted window, a nonce that was not seen
before, the body digest and an HMAC-SHA256 over the canonical request tuple. The secret and the
HMAC never reach a log, an audit payload, an error message or evidence.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets as random_source
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.platform.core.clock import Clock
from app.platform.core.errors import AuthError, InputValidationError, PolicyBlockedError
from app.platform.core.secrets import SecretStore
from app.stages.collect.extension.nonces import NonceCache

PAIRING_SECRET_KEY = "collect-extension-pairing"
SIGNATURE_SCHEME = "ICBM-EXT-1"
PAIRING_CODE_VERSION = 1
# How far a request's own timestamp may be from the server clock, either way.
TIMESTAMP_WINDOW_S = 120
EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()

EXTENSION_ID_HEADER = "X-ICBM-Extension-Id"
PAIRING_ID_HEADER = "X-ICBM-Pairing-Id"
GENERATION_HEADER = "X-ICBM-Pairing-Generation"
TIMESTAMP_HEADER = "X-ICBM-Timestamp"
NONCE_HEADER = "X-ICBM-Nonce"
BODY_DIGEST_HEADER = "X-ICBM-Body-SHA256"
SIGNATURE_HEADER = "X-ICBM-Signature"
SIGNED_HEADERS = (
    EXTENSION_ID_HEADER,
    PAIRING_ID_HEADER,
    GENERATION_HEADER,
    TIMESTAMP_HEADER,
    NONCE_HEADER,
    BODY_DIGEST_HEADER,
    SIGNATURE_HEADER,
)

# A Chrome extension identity: 32 characters of a–p.
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DIGITS = re.compile(r"^[0-9]{1,12}$")


class PairingRefused(AuthError):
    """The request is not from the paired extension. It says which check failed by code only, and
    never echoes what was presented."""


class PairingConflict(PolicyBlockedError):
    """A pairing command that the current pairing state does not allow."""


@dataclass(frozen=True)
class PairingRecord:
    """What the keyring holds. ``secret`` never leaves this module except inside a pairing code."""

    pairing_id: str
    generation: int
    extension_id: str
    secret: str

    def describe(self) -> dict[str, Any]:
        """The pairing without its secret, for the operator."""
        return {
            "pairing_id": self.pairing_id,
            "generation": self.generation,
            "extension_id": self.extension_id,
        }


@dataclass(frozen=True)
class IssuedPairing:
    record: PairingRecord
    # What the operator pastes into the extension, once. It carries the secret.
    code: str


@dataclass(frozen=True)
class VerifiedSender:
    pairing_id: str
    generation: int
    extension_id: str
    body_sha256: str

    @property
    def origin(self) -> str:
        return f"chrome-extension://{self.extension_id}"


def canonical_request(
    *,
    method: str,
    path: str,
    extension_id: str,
    pairing_id: str,
    generation: str,
    timestamp: str,
    nonce: str,
    body_sha256: str,
) -> bytes:
    """The one tuple a request's HMAC is computed over, identically on both sides."""
    return "\n".join(
        (
            SIGNATURE_SCHEME,
            method.upper(),
            path,
            extension_id,
            pairing_id,
            generation,
            timestamp,
            nonce,
            body_sha256,
        )
    ).encode("utf-8")


def sign(secret: str, message: bytes) -> str:
    return hmac.new(_secret_bytes(secret), message, hashlib.sha256).hexdigest()


def _secret_bytes(secret: str) -> bytes:
    return base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))


def _encode(record: PairingRecord) -> str:
    return json.dumps(
        {
            "pairing_id": record.pairing_id,
            "generation": record.generation,
            "extension_id": record.extension_id,
            "secret": record.secret,
        },
        sort_keys=True,
    )


def _decode(stored: str) -> PairingRecord | None:
    try:
        value = json.loads(stored)
        record = PairingRecord(
            pairing_id=str(value["pairing_id"]),
            generation=int(value["generation"]),
            extension_id=str(value["extension_id"]),
            secret=str(value["secret"]),
        )
    except (ValueError, KeyError, TypeError):
        return None
    if record.generation < 1 or not _EXTENSION_ID.fullmatch(record.extension_id):
        return None
    return record


def pairing_code(record: PairingRecord, *, origin: str) -> str:
    """The one-time code: everything the extension needs, base64url-encoded JSON."""
    document = {
        "v": PAIRING_CODE_VERSION,
        "origin": origin,
        "pairing_id": record.pairing_id,
        "generation": record.generation,
        "secret": record.secret,
    }
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class ExtensionPairing:
    def __init__(self, secrets: SecretStore, clock: Clock, nonces: NonceCache) -> None:
        self._secrets = secrets
        self._clock = clock
        self._nonces = nonces

    # ------------------------------------------------------------------ operator commands

    def current(self) -> PairingRecord | None:
        stored = self._secrets.get(PAIRING_SECRET_KEY)
        return None if stored is None else _decode(stored)

    def pair(self, extension_id: str, *, origin: str) -> IssuedPairing:
        """Issue the pairing of one pinned extension identity. Refused while one exists."""
        if not _EXTENSION_ID.fullmatch(extension_id):
            raise InputValidationError(
                "EXTENSION_ID_INVALID", "an extension identity is 32 characters of a to p"
            )
        if self._secrets.get(PAIRING_SECRET_KEY) is not None:
            raise PairingConflict(
                "EXTENSION_ALREADY_PAIRED", "a pairing exists; rotate it or revoke it first"
            )
        return self._issue(PairingRecord(str(uuid.uuid4()), 1, extension_id, _new_secret()), origin)

    def rotate(self, *, origin: str) -> IssuedPairing:
        """A new generation and a new secret for the same pairing. The prior one stops working."""
        record = self.current()
        if record is None:
            raise PairingConflict("EXTENSION_NOT_PAIRED", "there is no pairing to rotate")
        return self._issue(
            PairingRecord(
                record.pairing_id, record.generation + 1, record.extension_id, _new_secret()
            ),
            origin,
        )

    def revoke(self) -> bool:
        """Remove the pairing. Returns whether one existed."""
        existed = self._secrets.get(PAIRING_SECRET_KEY) is not None
        self._secrets.delete(PAIRING_SECRET_KEY)
        self._nonces.clear()
        return existed

    def _issue(self, record: PairingRecord, origin: str) -> IssuedPairing:
        self._secrets.set(PAIRING_SECRET_KEY, _encode(record))
        self._nonces.clear()
        return IssuedPairing(record, pairing_code(record, origin=origin))

    # ------------------------------------------------------------------ request verification

    def verify(
        self, *, method: str, path: str, origin: str | None, headers: Mapping[str, str]
    ) -> VerifiedSender:
        """Prove a request's sender from its headers alone, before its body is read.

        The order is fixed and every failure is one code. A valid HMAC consumes the nonce, so the
        same request can never be accepted twice. The body is bound by its digest: the caller
        compares the digest named here with the body it then reads.
        """
        record = self.current()
        if record is None:
            raise PairingRefused("EXTENSION_NOT_PAIRED", "no extension is paired")
        presented = {name: headers.get(name, "") for name in SIGNED_HEADERS}
        extension_id = presented[EXTENSION_ID_HEADER]
        if extension_id != record.extension_id:
            raise PairingRefused("EXTENSION_IDENTITY_MISMATCH", "not the paired extension")
        if origin is not None and origin != f"chrome-extension://{record.extension_id}":
            raise PairingRefused("EXTENSION_ORIGIN_MISMATCH", "not the paired extension's origin")
        if presented[PAIRING_ID_HEADER] != record.pairing_id or presented[GENERATION_HEADER] != str(
            record.generation
        ):
            raise PairingRefused("EXTENSION_PAIRING_MISMATCH", "not the current pairing")
        timestamp, nonce = presented[TIMESTAMP_HEADER], presented[NONCE_HEADER]
        body_sha256, signature = presented[BODY_DIGEST_HEADER], presented[SIGNATURE_HEADER]
        if (
            not _DIGITS.fullmatch(timestamp)
            or not _NONCE.fullmatch(nonce)
            or not _HEX64.fullmatch(body_sha256)
            or not _HEX64.fullmatch(signature)
        ):
            raise PairingRefused(
                "EXTENSION_SIGNATURE_MALFORMED", "the signed headers are malformed"
            )
        expected = sign(
            record.secret,
            canonical_request(
                method=method,
                path=path,
                extension_id=extension_id,
                pairing_id=record.pairing_id,
                generation=str(record.generation),
                timestamp=timestamp,
                nonce=nonce,
                body_sha256=body_sha256,
            ),
        )
        if not hmac.compare_digest(expected, signature):
            raise PairingRefused("EXTENSION_SIGNATURE_INVALID", "the request signature is invalid")
        now = int(self._clock.now().timestamp())
        if abs(now - int(timestamp)) > TIMESTAMP_WINDOW_S:
            raise PairingRefused("EXTENSION_TIMESTAMP_STALE", "the request timestamp is stale")
        self._nonces.consume(record.pairing_id, nonce)
        return VerifiedSender(record.pairing_id, record.generation, extension_id, body_sha256)


def _new_secret() -> str:
    return base64.urlsafe_b64encode(random_source.token_bytes(32)).decode("ascii").rstrip("=")
