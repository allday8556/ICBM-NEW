"""The pure rules of the pre-LIVE safety owners (ADR-0018 §3, §3.4, §4, §10). No I/O.

**The ASSET replay-conflict key (§3.4, G3-28).** It is exactly four fields and nothing else:

1. the marketplace;
2. the canonical account;
3. the normalized wire endpoint identity — HTTP method, provider host and path;
4. the exact outbound content digest of the uploaded binary.

Every other value an upload carries — the multipart file name, the MIME or type metadata, the
local source-or-derived kind, the ``derivation_id``, the candidate, preparation, grant or profile,
the endpoint-mapping revision, a provider-document version and any ICBM adoption or contract label
— is **provenance only**. :func:`replay_key` does not even accept them, so no caller can narrow the
fence with one.

**Host normalization is server-owned** (Claude cross-audit 7, non-blocking item 3). A
:class:`WireHostPolicy` is built once, at wiring, from the adapter's own registry: the one canonical
provider host of each marketplace and the aliases that name the same provider. A host spelled with
another case, a trailing dot, the default port or user information normalizes to the same host; an
alias maps to the canonical host; any other host — a proxy, an IP literal, an unknown name — cannot
be proven to be the same provider, so the key is **undeterminable** and the ASSET stage stays
``BLOCKED`` (ambiguity takes the wider scope, never another key).
"""

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.errors import AppError, ErrorClass, InputValidationError

REPLAY_KEY_VERSION: Final = "asset-replay-key/v1"

# The endpoint group of each mutation stage (§3.2). The CREATE group is the REGISTER execution
# owner's (ADR-0014 §26); the ASSET group has no §26 scope row and never pretends one exists.
ASSET_ENDPOINT_GROUP: Final = "product_image_upload"
CREATE_ENDPOINT_GROUP: Final = "product_registration"

# Server-owned bounds the implementing slice fixes (ADR-0018 §13). A grant is short-lived and
# small: a canary is one unit, and a CREATE grant authorizes exactly one attempt (G3-27).
MAX_GRANT_WINDOW_S: Final = 4 * 60 * 60
MAX_ASSET_BUDGET: Final = 10
CREATE_BUDGET: Final = 1


class MutationStage(StrEnum):
    ASSET = "ASSET"
    CREATE = "CREATE"


class GrantState(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    EXHAUSTED = "EXHAUSTED"


TERMINAL_GRANT_STATES: Final = frozenset(
    {GrantState.EXPIRED, GrantState.REVOKED, GrantState.EXHAUSTED}
)


class BrakeState(StrEnum):
    ENGAGED = "ENGAGED"
    RELEASED = "RELEASED"


class UploadAttemptState(StrEnum):
    """One ASSET upload attempt (§3.4): started before transmission, terminal exactly once."""

    STARTED = "STARTED"
    APPLIED_PROVEN = "APPLIED_PROVEN"
    NOT_APPLIED_PROVEN = "NOT_APPLIED_PROVEN"
    UPLOAD_UNKNOWN = "UPLOAD_UNKNOWN"


TERMINAL_UPLOAD_STATES: Final = frozenset(
    {
        UploadAttemptState.APPLIED_PROVEN,
        UploadAttemptState.NOT_APPLIED_PROVEN,
        UploadAttemptState.UPLOAD_UNKNOWN,
    }
)
# Every state that keeps a fresh upload with the same key blocked (G3-29): an unresolved one, and
# an applied one while no reuse/rebind path is adopted. Only NOT_APPLIED_PROVEN clears the fence.
FENCING_UPLOAD_STATES: Final = frozenset(
    {
        UploadAttemptState.STARTED,
        UploadAttemptState.UPLOAD_UNKNOWN,
        UploadAttemptState.APPLIED_PROVEN,
    }
)


class Layer(StrEnum):
    """The layers of the send-time safety stack (§4.3) and of stage readiness (§10)."""

    EXECUTION_MODE = "EXECUTION_MODE"
    PROTECTED_WRITE_BRAKE = "PROTECTED_WRITE_BRAKE"
    GRANT = "GRANT"
    ENDPOINT_ADOPTED = "ENDPOINT_ADOPTED"
    SENDER_WIRED = "SENDER_WIRED"
    CANARY_NON_REGULATED = "CANARY_NON_REGULATED"
    RESTORE_PROOF = "RESTORE_PROOF"
    EVIDENCE_RETENTION = "EVIDENCE_RETENTION"
    VISUAL_ACCEPTANCE = "VISUAL_ACCEPTANCE"
    ATTEMPT_OWNER = "ATTEMPT_OWNER"
    REPLAY_FENCE = "REPLAY_FENCE"
    STAGE_GATE = "STAGE_GATE"


# Why a layer refuses. Codes only.
MODE_NOT_LIVE: Final = "LIVE_EXECUTION_MODE_NOT_LIVE"
BRAKE_ENGAGED: Final = "LIVE_PROTECTED_WRITE_BRAKE_ENGAGED"
BRAKE_UNREADABLE: Final = "LIVE_PROTECTED_WRITE_BRAKE_UNREADABLE"
GRANT_MISSING: Final = "LIVE_GRANT_NO_MATCHING_ACTIVE_GRANT"
ENDPOINT_NOT_ADOPTED: Final = "LIVE_ENDPOINT_NOT_ADOPTED"
SENDER_NOT_WIRED: Final = "LIVE_SENDER_NOT_WIRED"
ELIGIBILITY_UNPROVEN: Final = "LIVE_CANARY_ELIGIBILITY_UNPROVEN"
RESTORE_PROOF_ABSENT: Final = "LIVE_RESTORE_PROOF_ABSENT"
RETENTION_UNPROVEN: Final = "LIVE_EVIDENCE_RETENTION_UNPROVEN"
VISUAL_UNRECORDED: Final = "LIVE_VISUAL_ACCEPTANCE_UNRECORDED"
ATTEMPT_OWNER_UNREADABLE: Final = "LIVE_ASSET_ATTEMPT_OWNER_UNREADABLE"
REPLAY_UNRESOLVED: Final = "LIVE_ASSET_REPLAY_UNRESOLVED"
REPLAY_APPLIED_REUSE_NOT_ADOPTED: Final = "LIVE_ASSET_REPLAY_APPLIED_REUSE_NOT_ADOPTED"
REPLAY_KEY_UNDETERMINABLE: Final = "LIVE_ASSET_REPLAY_KEY_UNDETERMINABLE"
CANDIDATE_NOT_READY: Final = "LIVE_ASSET_CANDIDATE_NOT_READY"
CANDIDATE_DRIFT: Final = "LIVE_ASSET_CANDIDATE_DRIFT"
ARTIFACT_NOT_GRANTED: Final = "LIVE_ASSET_ARTIFACT_NOT_IN_GRANT"
CONTENT_DIGEST_MISMATCH: Final = "LIVE_ASSET_CONTENT_DIGEST_MISMATCH"


class MutationRefused(AppError):
    """A marketplace mutation refused before any transmission (§4.3). It never retries itself."""

    error_class = ErrorClass.POLICY_BLOCKED


class WireIdentityError(InputValidationError):
    """The wire endpoint identity cannot be determined: the replay key is undeterminable."""


# ---------------------------------------------------------------- the wire endpoint identity

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@-]+$")
_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _refuse(message: str, **details: str) -> WireIdentityError:
    return WireIdentityError(REPLAY_KEY_UNDETERMINABLE, message, details=dict(details))


def normalize_host(raw: str) -> str:
    """The lexical host: lower case, no user information, no trailing dot, no default port.

    An IP literal, an explicit non-default port or anything that is not a plain DNS name is
    refused: none of them can be proven to be the provider's canonical host.
    """
    host = raw.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if host.startswith("["):
        raise _refuse("an IP literal is not a provider host")
    if ":" in host:
        host, port = host.rsplit(":", 1)
        if port != "443":
            raise _refuse("only the default HTTPS port names the provider host", port=port)
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise _refuse("an IP literal is not a provider host")
    labels = host.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.match(label) for label in labels):
        raise _refuse("the host is not a plain DNS name")
    return host


def normalize_path(raw: str) -> str:
    """The wire path: absolute, no query or fragment, no empty, dot or encoded segment."""
    path = raw.strip()
    if not path.startswith("/") or "?" in path or "#" in path or "%" in path:
        raise _refuse("the wire path must be absolute with no query, fragment or escape")
    segments = [segment for segment in path.split("/") if segment != ""]
    if not segments or any(s in (".", "..") or not _PATH_SEGMENT.match(s) for s in segments):
        raise _refuse("the wire path has an empty, dot or unsafe segment")
    return "/" + "/".join(segments)


def normalize_method(raw: str) -> str:
    method = raw.strip().upper()
    if method not in _METHODS:
        raise _refuse("an upload is a mutating HTTP method", method=method)
    return method


@dataclass(frozen=True)
class WireEndpoint:
    """A normalized wire endpoint identity (§3.4 field 3)."""

    method: str
    host: str
    path: str


class WireHostPolicy:
    """The server-owned host rule of each marketplace: one canonical host and its aliases.

    It is built at wiring from the adapter registry, never from a request. A host is resolved to
    the canonical host of its marketplace or refused; nothing else can split a replay scope.
    """

    def __init__(
        self,
        canonical: Mapping[str, str],
        aliases: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self._canonical = {key: normalize_host(host) for key, host in canonical.items()}
        self._aliases: dict[str, dict[str, str]] = {}
        for key, table in (aliases or {}).items():
            canonical_host = self._canonical[key]
            self._aliases[key] = {}
            for alias, target in table.items():
                if normalize_host(target) != canonical_host:
                    raise ValueError(f"alias {alias} of {key} does not name its canonical host")
                self._aliases[key][normalize_host(alias)] = canonical_host

    def host(self, marketplace_key: str, raw: str) -> str:
        canonical = self._canonical.get(marketplace_key)
        if canonical is None:
            raise _refuse(
                "no canonical provider host for this marketplace", marketplace=marketplace_key
            )
        host = normalize_host(raw)
        if host == canonical:
            return canonical
        mapped = self._aliases.get(marketplace_key, {}).get(host)
        if mapped is None:
            # A proxy or an unknown spelling is not proven to be the provider: never a new key.
            raise _refuse("the host is not this marketplace's provider host", host=host)
        return mapped

    def endpoint(self, marketplace_key: str, *, method: str, host: str, path: str) -> WireEndpoint:
        return WireEndpoint(
            method=normalize_method(method),
            host=self.host(marketplace_key, host),
            path=normalize_path(path),
        )


# ---------------------------------------------------------------- the replay-conflict key

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReplayKey:
    """The ASSET replay-conflict key: exactly the four wire-boundary fields (G3-28)."""

    marketplace_key: str
    marketplace_account_id: str
    endpoint: WireEndpoint
    content_sha256: str

    @property
    def digest(self) -> str:
        document = {
            "version": REPLAY_KEY_VERSION,
            "marketplace_key": self.marketplace_key,
            "marketplace_account_id": self.marketplace_account_id,
            "method": self.endpoint.method,
            "host": self.endpoint.host,
            "path": self.endpoint.path,
            "content_sha256": self.content_sha256,
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def replay_key(
    *,
    marketplace_key: str,
    marketplace_account_id: str,
    endpoint: WireEndpoint,
    content_sha256: str,
) -> ReplayKey:
    """The key. It takes the four boundary fields only: provenance cannot reach it."""
    if not marketplace_key or not marketplace_account_id:
        raise _refuse("the marketplace and the canonical account are both required")
    if not _HEX64.match(content_sha256):
        raise _refuse("the outbound content digest is not a SHA-256")
    return ReplayKey(marketplace_key, marketplace_account_id, endpoint, content_sha256)


def content_digest(content: bytes) -> str:
    """The outbound content digest: computed from the exact bytes that would be sent."""
    return hashlib.sha256(content).hexdigest()


def is_hex64(value: str | None) -> bool:
    return value is not None and bool(_HEX64.match(value))
