"""The onboarding host guard (ADR-0036 §2 D1, OB-01).

An onboarded host is an exact ``https`` host name: lower-case, IDNA-normalised, with at least two
labels and a top-level label that is not a number. It is never a wildcard, an IP literal (v4, v6
or a shortened or numeric form), ``localhost`` or another special-use name, and it carries no
port, user information, path, query or fragment. A refusal names its reason; the refused text
itself is never echoed into an error or a log.

The guard judges names only and resolves nothing: it runs before any grant exists, and the egress
guard then allows exactly the confirmed names (ADR-0036 §9).
"""

import ipaddress
import re
from typing import Final
from urllib.parse import urlsplit

from app.platform.core.errors import InputValidationError

ONBOARDING_HOST_REFUSED: Final = "ONBOARDING_HOST_REFUSED"
ONBOARDING_ORIGIN_REFUSED: Final = "ONBOARDING_ORIGIN_REFUSED"

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# Names that never denote a public storefront (RFC 6761, RFC 6762, RFC 7686, RFC 8375 and the
# common private suffixes).
_SPECIAL_SUFFIXES: Final = (
    "localhost",
    "local",
    "localdomain",
    "internal",
    "intranet",
    "lan",
    "home",
    "corp",
    "test",
    "example",
    "invalid",
    "onion",
    "arpa",
)
# Characters a host name never holds and that each mean something else in a URL.
_FOREIGN: Final = {
    "*": "wildcard",
    ":": "port",
    "@": "userinfo",
    "/": "path",
    "?": "query",
    "#": "fragment",
    "[": "ip_literal",
    "]": "ip_literal",
    "%": "encoded",
    "\\": "path",
}


def _host_refusal(reason: str) -> InputValidationError:
    return InputValidationError(
        ONBOARDING_HOST_REFUSED,
        "an onboarded host is one exact public https host name",
        details={"reason": reason},
    )


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def onboarding_host(value: object) -> str:
    """The normalised host, or ``ONBOARDING_HOST_REFUSED`` with its reason."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise _host_refusal("form")
    if "[" in value or _is_ip(value):
        raise _host_refusal("ip_literal")
    for char, reason in _FOREIGN.items():
        if char in value:
            raise _host_refusal(reason)
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise _host_refusal("form")
    text = value.lower()
    try:
        ascii_name = text.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise _host_refusal("form") from None
    labels = ascii_name.split(".")
    if len(ascii_name) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels):
        raise _host_refusal("form")
    if labels[-1].isdigit() or all(label.isdigit() for label in labels) or _is_ip(ascii_name):
        # 10.0.0.1, 127.1 and every other numeric form are IP literals, never names.
        raise _host_refusal("ip_literal")
    if labels[-1] in _SPECIAL_SUFFIXES or ascii_name.endswith(".home.arpa"):
        raise _host_refusal("special_use")
    return ascii_name


def onboarding_origin(value: object) -> tuple[str, str]:
    """``(origin, host)``: ``https://<host>`` and its host, or a refusal.

    The origin is the scheme and the host only; a single trailing ``/`` is accepted and dropped.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise InputValidationError(
            ONBOARDING_ORIGIN_REFUSED, "the storefront origin is https://<host>"
        )
    parts = urlsplit(value)
    if parts.scheme.lower() != "https":
        raise InputValidationError(
            ONBOARDING_ORIGIN_REFUSED,
            "the storefront origin is https://<host>",
            details={"reason": "scheme"},
        )
    if parts.path not in ("", "/"):
        raise _host_refusal("path")
    if parts.query or "?" in value:
        raise _host_refusal("query")
    if parts.fragment or "#" in value:
        raise _host_refusal("fragment")
    host = onboarding_host(parts.netloc)
    return f"https://{host}", host
