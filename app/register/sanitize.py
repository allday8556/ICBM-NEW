"""M5 PR-C: the outbound sanitation rules of the registration payload (ADR-0014 §5, §15, B4;
kickoff 5742880432 §12).

The payload builder returns the sanitized canonical representation that the durable Snapshot
payload and its ``payload_hash`` are computed from. Nothing secret-bearing is ever accepted into
it, so nothing needs to be removed from it afterwards:
- **no credential or session material**: a field named like an authorization header, a cookie, a
  token, a secret, a password, a session, a signature or an API key is refused, and so is a value
  that carries a bearer credential or a JSON web token;
- **no external URL** in a business value: no supplier hotlink is ever published (M5-19), and a
  signed or tokenized URL is credential-equivalent. Images travel only as local artifact
  identities and, later, as the provider asset reference PR-D prepares;
- a provider asset reference is an opaque reference with **no query string, fragment or user
  information**, so no signed or expiring material rides on it.

Wire-only secrets stay transient in PR-D/E and never reach this module. Pure: no I/O.
"""

import re
from collections.abc import Iterator, Mapping
from typing import Any, Final

SANITIZER_RULES_VERSION: Final = "register-outbound-sanitizer/v1"

SECRET_MATERIAL: Final = "PAYLOAD_SECRET_MATERIAL"
EXTERNAL_URL: Final = "PAYLOAD_EXTERNAL_URL"

_SECRET_KEY = re.compile(
    r"authori[sz]ation|cookie|token|secret|passw(or)?d|session|signature|bearer"
    r"|api[_-]?key|access[_-]?key|credential",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"\bbearer\s+[A-Za-z0-9._~+/-]{8,}|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|(access|refresh|id)_token\s*[=:]",
    re.I,
)
_URL = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://|(^|[\s\"'(=])//[^\s/]|\bwww\.[a-z0-9-]+\.)")
_PROVIDER_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")


def _walk(value: Any, path: str) -> Iterator[tuple[str, str | None, Any]]:
    """Every (path, key, leaf) of a nested mapping/sequence value."""
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            yield f"{path}.{key}", str(key), None
            yield from _walk(value[key], f"{path}.{key}")
    elif isinstance(value, list | tuple | set | frozenset):
        items = sorted(value, key=str) if isinstance(value, set | frozenset) else value
        for index, item in enumerate(items):
            yield from _walk(item, f"{path}[{index}]")
    else:
        yield path, None, value


def problems(value: Any, path: str = "payload") -> tuple[tuple[str, str], ...]:
    """Every sanitation problem of an outbound value, as ``(code, path)``, sorted. Empty when the
    value may enter the durable payload."""
    found: set[tuple[str, str]] = set()
    for where, key, leaf in _walk(value, path):
        if key is not None and _SECRET_KEY.search(key):
            found.add((SECRET_MATERIAL, where))
        if isinstance(leaf, str):
            if _SECRET_VALUE.search(leaf):
                found.add((SECRET_MATERIAL, where))
            if _URL.search(leaf):
                found.add((EXTERNAL_URL, where))
    return tuple(sorted(found))


_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def safe_label(value: str) -> bool:
    """Whether a version or identity label is plain: label characters only, no URL, no secret."""
    return isinstance(value, str) and bool(_LABEL.fullmatch(value)) and not problems(value)


def hex_digest(value: str) -> bool:
    """Whether a value is a lower-case hex SHA-256 digest."""
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def safe_provider_reference(reference: str) -> bool:
    """Whether a provider reference (a prepared asset or a duplicate-lookup listing) is opaque, or
    a plain https reference, and carries no signed material. Anything URL-shaped must be a plain
    https reference: an ``http:`` or other scheme, a protocol-relative URL, a query, a fragment or
    user information is never accepted as "opaque"."""
    if not isinstance(reference, str) or _SECRET_VALUE.search(reference):
        return False
    if _URL.search(reference) or ":/" in reference:
        return _safe_https(reference)
    return bool(_PROVIDER_REF.fullmatch(reference))


def _safe_https(reference: str) -> bool:
    """A plain https reference to a provider-hosted object: no userinfo, query or fragment."""
    return (
        bool(re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?", reference))
        and len(reference) <= 512
    )


class PayloadSanitationError(ValueError):
    """An outbound value the durable payload must never hold."""

    def __init__(self, found: tuple[tuple[str, str], ...]) -> None:
        super().__init__("refused: " + ", ".join(f"{code} at {where}" for code, where in found))
        self.found = found


def require_clean(value: Any, path: str = "payload") -> None:
    found = problems(value, path)
    if found:
        raise PayloadSanitationError(found)
