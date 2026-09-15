"""The generic COLLECT URL boundary (ADR-0010 §9; PR #57 review 5213637642, blocker 1).

Signed, expiring or tokenized URLs are credential-equivalent. This module knows no supplier: a
supplier's collection profile (PR-C) supplies only the explicit safe query keys per host, and the
default allowlist is empty, so every query key is secret-bearing until a profile says otherwise.

``sanitize_url`` canonicalizes one URL — https only, no credentials, the default port, no path
parameters, no fragment, and no query key that is not explicitly safe for its host — or refuses
it. Persistence then fails closed: a stored URL must already be its own sanitized form, and every
other persisted text is scanned for URL material, so an unsanitized URL or a secret-looking query
assignment anywhere is refused. Nothing is ever hashed in place of sanitization, and no error
message echoes the URL.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import unquote_plus, urlsplit

from app.core.errors import InputValidationError

HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")

# Query-parameter names that look secret-bearing. They serve twice: a profile can never allowlist
# such a key, and persisted text can never carry such an assignment, even outside a whole URL.
_SECRET_NAME_PARTS = (
    "token",
    "sign",
    "auth",
    "sess",
    "secret",
    "passw",
    "credential",
    "expire",
    "x-amz",
    "hmac",
    "nonce",
    "apikey",
    "api_key",
    "api-key",
    "policy",
)
_SECRET_NAMES = frozenset({"sid", "sig", "key", "hash"})
# Anything shaped like a URL: a scheme (or none) followed by ``//`` and a host-like run.
_URL_MATERIAL = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*:)?//[^\s\"'<>()\\]+")
_QUERY_ASSIGNMENT = re.compile(r"[?&;]([^=&#?;\s\"'<>]+)=")


def secret_looking(name: str) -> bool:
    lowered = unquote_plus(name).lower()
    return lowered in _SECRET_NAMES or any(part in lowered for part in _SECRET_NAME_PARTS)


def _no_keys() -> Mapping[str, frozenset[str]]:
    return MappingProxyType({})


@dataclass(frozen=True)
class UrlPolicy:
    """Explicit safe query keys per host. A host that is not listed keeps no query key."""

    safe_query_keys: Mapping[str, frozenset[str]] = field(default_factory=_no_keys)

    def __post_init__(self) -> None:
        for host, keys in self.safe_query_keys.items():
            if not HOST.fullmatch(host):
                raise ValueError("a URL policy names lowercase hosts")
            if not isinstance(keys, frozenset) or not all(isinstance(k, str) and k for k in keys):
                raise ValueError("a URL policy lists non-empty query keys as a frozenset")
            if any(secret_looking(key) for key in keys):
                raise ValueError("a secret-bearing query key can never be allowlisted")

    def safe_keys(self, host: str) -> frozenset[str]:
        return self.safe_query_keys.get(host, frozenset())


# The default: no query key is safe anywhere.
STRICT_URL_POLICY = UrlPolicy()


def _unsafe(what: str, reason: str) -> InputValidationError:
    return InputValidationError("COLLECT_URL_UNSAFE", f"{what}: {reason}")


def sanitize_url(url: str, policy: UrlPolicy = STRICT_URL_POLICY, *, what: str = "url") -> str:
    """The canonical, non-secret form of ``url``, or a refusal when it cannot be made safe."""
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 for char in url):
        raise _unsafe(what, "must be a URL without whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _unsafe(what, "is not a parseable URL") from None
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise _unsafe(what, "must be an https URL")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _unsafe(what, "must not carry credentials")
    if port not in (None, 443):
        raise _unsafe(what, "must use the default https port")
    host = parts.hostname
    if not HOST.fullmatch(host):
        raise _unsafe(what, "must name a host")
    path = parts.path or "/"
    if ";" in unquote_plus(path):
        raise _unsafe(what, "must not carry path parameters")
    safe = policy.safe_keys(host)
    kept = [
        pair
        for pair in parts.query.split("&")
        if pair and unquote_plus(pair.split("=", 1)[0]) in safe
    ]
    return f"https://{host}{path}" + (f"?{'&'.join(kept)}" if kept else "")


def require_sanitized(url: str, policy: UrlPolicy, what: str) -> None:
    """Persistence accepts a URL only in its own sanitized form."""
    if sanitize_url(url, policy, what=what) != url:
        raise _unsafe(
            what,
            "must be stored in its sanitized form (explicitly safe query keys only, no fragment)",
        )


def check_persisted_text(text: str, policy: UrlPolicy, what: str) -> None:
    """Fail closed on URL material inside any text that is about to be persisted."""
    for match in _URL_MATERIAL.finditer(text):
        require_sanitized(match.group(0), policy, what)
    for match in _QUERY_ASSIGNMENT.finditer(text):
        if secret_looking(match.group(1)):
            raise _unsafe(what, "carries secret-looking query material")
