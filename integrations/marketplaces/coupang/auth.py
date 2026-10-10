"""Coupang HMAC and request-authentication contracts (C-AUTH-1).

The official construction is intentionally represented without an HTTP client. The exact query
string signed here is the exact query string a later adopted endpoint must transmit; this module
does not sort, decode or otherwise reinterpret endpoint parameters.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Protocol

COUPANG_PROVIDER_HOST = "api-gateway.coupang.com"
COUPANG_BASE_URL = f"https://{COUPANG_PROVIDER_HOST}"
ALGORITHM = "HmacSHA256"
AUTH_SCHEME = "CEA"
_SIGNED_DATE = re.compile(r"^\d{6}T\d{6}Z$")


class Market(StrEnum):
    KR = "KR"
    TW = "TW"


class Method(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class UtcClock(Protocol):
    """The signing clock. Tests inject a fixed clock; runtime code uses an aware UTC clock."""

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class SystemUtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def _header_value(name: str, value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value)
    ):
        raise ValueError(f"{name} must be non-empty printable ASCII without whitespace")
    return value


@dataclass(frozen=True)
class Credentials:
    """One committed Wing credential/vendor bundle.

    Both keys are treated as secret-bearing wire material and therefore stay out of repr. The
    vendor id is not inferred from either key and remains an explicit provider identity.
    """

    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    vendor_id: str
    credential_generation: int

    def __post_init__(self) -> None:
        _header_value("access_key", self.access_key)
        if not isinstance(self.secret_key, str) or not self.secret_key.strip():
            raise ValueError("secret_key must be non-empty")
        _header_value("vendor_id", self.vendor_id)
        if (
            not isinstance(self.credential_generation, int)
            or isinstance(self.credential_generation, bool)
            or self.credential_generation < 1
        ):
            raise ValueError("credential_generation must be a positive integer")


@dataclass(frozen=True)
class IpAllowlistDeclaration:
    """Local setup evidence only; Coupang exposes no allowlist-introspection endpoint.

    This record never proves provider acceptance. It binds the operator-declared outbound IP to
    the credential generation whose Wing linking information was configured.
    """

    source_ip: IPv4Address | IPv6Address
    credential_generation: int
    recorded_at: datetime

    @classmethod
    def create(
        cls, source_ip: str, *, credential_generation: int, recorded_at: datetime
    ) -> IpAllowlistDeclaration:
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        if (
            not isinstance(credential_generation, int)
            or isinstance(credential_generation, bool)
            or credential_generation < 1
        ):
            raise ValueError("credential_generation must be a positive integer")
        return cls(ip_address(source_ip), credential_generation, recorded_at.astimezone(UTC))


@dataclass(frozen=True)
class AuthContext:
    credentials: Credentials
    market: Market
    ip_allowlist: IpAllowlistDeclaration | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.market, Market):
            raise ValueError("market must be an officially supported Coupang market")
        if (
            self.ip_allowlist is not None
            and self.ip_allowlist.credential_generation != self.credentials.credential_generation
        ):
            raise ValueError("IP allowlist evidence belongs to a different credential generation")

    @property
    def setup_evidence_current(self) -> bool:
        """Auth setup evidence only, not permission to make a provider call."""
        return self.ip_allowlist is not None


@dataclass(frozen=True)
class RequestTarget:
    method: Method
    path: str
    query: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.method, Method):
            raise ValueError("method must be a supported Coupang request method")
        if not isinstance(self.path, str) or not isinstance(self.query, str):
            raise ValueError("path and query must be strings")
        if not self.path.startswith("/") or self.path.startswith("//"):
            raise ValueError("path must be one absolute provider path")
        if any(char in self.path for char in ("?", "#", "\r", "\n")):
            raise ValueError("path must not contain query, fragment or control characters")
        if (
            self.query.startswith("?")
            or "#" in self.query
            or any(char in self.query for char in ("\r", "\n"))
        ):
            raise ValueError("query must be the exact transmitted query without '?' or fragment")


@dataclass(frozen=True)
class SignedHeaders:
    authorization: str = field(repr=False)
    requested_by: str
    market: Market
    signed_date: str
    credential_generation: int

    def as_http_headers(self) -> dict[str, str]:
        return {
            "Authorization": self.authorization,
            "X-Requested-By": self.requested_by,
            "X-MARKET": self.market.value,
        }


def signed_date(at: datetime) -> str:
    if not isinstance(at, datetime):
        raise ValueError("signing time must be a datetime")
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("signing time must be timezone-aware")
    return at.astimezone(UTC).strftime("%y%m%dT%H%M%SZ")


def signature(secret_key: str, date: str, target: RequestTarget) -> str:
    """HMAC-SHA256 of ``signed-date + method + path + query`` as officially documented."""
    if not isinstance(secret_key, str) or not secret_key.strip():
        raise ValueError("secret_key must be non-empty")
    if not isinstance(date, str) or _SIGNED_DATE.fullmatch(date) is None:
        raise ValueError("signed date must use yyMMdd'T'HHmmss'Z' format")
    message = f"{date}{target.method.value}{target.path}{target.query}".encode()
    return hmac.new(secret_key.encode(), message, hashlib.sha256).hexdigest()


class Authenticator:
    """Creates a fresh signature for every request. No signature or timestamp is cached."""

    def __init__(self, clock: UtcClock) -> None:
        self._clock = clock

    def headers(self, context: AuthContext, target: RequestTarget) -> SignedHeaders:
        date = signed_date(self._clock.now())
        credentials = context.credentials
        digest = signature(credentials.secret_key, date, target)
        authorization = (
            f"{AUTH_SCHEME} algorithm={ALGORITHM}, access-key={credentials.access_key}, "
            f"signed-date={date}, signature={digest}"
        )
        return SignedHeaders(
            authorization=authorization,
            requested_by=credentials.vendor_id,
            market=context.market,
            signed_date=date,
            credential_generation=credentials.credential_generation,
        )
