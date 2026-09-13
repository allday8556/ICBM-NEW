"""Supplier-generic CONNECT port (Issue #7 and its architect addenda, ADR-0007).

A supplier implementation is *site knowledge only*: an immutable ``SupplierDefinition`` holding
its profile, its protected-read probe (a target plus pure predicates over an immutable response
view) and a declarative description of its login form. It receives no client, page, browser
context or transport handle — nothing it could use to make a request (Issue #7 comment
5653615136). Everything that acts — pacing, HTTP and browser execution, request attribution,
single-flight authentication, session lifecycle, the proof procedure, the loop guard and safe
auditing — is common infrastructure that consumes these definitions through ``SupplierGateway``.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

SUPPLIER_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class SupplierTransport(StrEnum):
    HTTP = "HTTP"
    BROWSER = "BROWSER"


class RequestKind(StrEnum):
    CONTROL_READ = "CONTROL_READ"  # the protected target without a session (negative control)
    PROTECTED_READ = "PROTECTED_READ"  # the same target with the authenticated session
    AUTHENTICATE = "AUTHENTICATE"


@dataclass(frozen=True)
class RequestPolicy:
    """Per-supplier request-policy inputs (Issue #7 §9), enforced by the common transport."""

    max_concurrency: int = 1
    minimum_request_interval_s: float = 1.0
    auth_retry_limit: int = 3
    request_timeout_s: float = 20.0

    def __post_init__(self) -> None:
        if self.max_concurrency < 1 or self.auth_retry_limit < 1:
            raise ValueError("max_concurrency and auth_retry_limit must be >= 1")
        if self.minimum_request_interval_s < 0 or self.request_timeout_s <= 0:
            raise ValueError("invalid request interval or timeout")


@dataclass(frozen=True)
class SupplierProfile:
    supplier_key: str
    display_name: str
    base_url: str
    auth_required: bool
    # Every external host the supplier's pages may reach, over HTTP or in the browser.
    egress_hosts: frozenset[str]
    request_policy: RequestPolicy = field(default_factory=RequestPolicy)

    def __post_init__(self) -> None:
        if not SUPPLIER_KEY.fullmatch(self.supplier_key):
            raise ValueError(f"invalid supplier key: {self.supplier_key!r}")
        if not self.base_url.startswith("https://") or self.base_url.endswith("/"):
            raise ValueError("supplier base_url must be https:// without a trailing slash")
        if not self.egress_hosts:
            raise ValueError("a supplier profile must declare its egress hosts")


@dataclass(frozen=True)
class ProbeResponse:
    """The only thing a supplier predicate ever sees: an immutable view of one response.

    ``body`` is used for recognition in memory only; it is never logged, persisted or returned.
    """

    status: int
    path: str
    location: str | None  # redirect target (path only) for a 3xx response
    body: str = field(repr=False)


# (predicate holds, names of the markers that decided it) — marker names, never page content.
Verdict = tuple[bool, tuple[str, ...]]
Predicate = Callable[[ProbeResponse], Verdict]


@dataclass(frozen=True)
class ProtectedReadProbe:
    """Site knowledge for the protected-read proof. The proof procedure itself is common."""

    target: str
    # Holds for the unauthenticated control: redirect to login, login-state markers, denial.
    unauthenticated_expectation: Predicate
    # Holds only for content that exists solely for an authenticated member.
    authenticated_predicate: Predicate

    def __post_init__(self) -> None:
        if not self.target.startswith("/") or "?" in self.target:
            raise ValueError("probe target must be an absolute path without a query")


@dataclass(frozen=True)
class LoginFormSpec:
    """Declarative browser login; the common browser transport fills and submits it."""

    path: str
    username_selector: str
    password_selector: str
    submit_selector: str
    # Selectors whose presence on the login page after submission means "credentials rejected".
    rejection_selectors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            raise ValueError("login path must be absolute")


@dataclass(frozen=True)
class SupplierDefinition:
    profile: SupplierProfile
    probe: ProtectedReadProbe
    login: LoginFormSpec


@dataclass(frozen=True)
class Credentials:
    """Operator credentials, held in memory only for the duration of one authentication."""

    username: str = field(repr=False)
    password: str = field(repr=False)


class SupplierGateway(Protocol):
    """The common, policy-enforcing transport. Core CONNECT consumes it; suppliers never do."""

    def fetch(
        self, definition: SupplierDefinition, *, kind: RequestKind, session: bytes | None
    ) -> ProbeResponse:
        """Read ``definition.probe.target`` — the only resource CONNECT may request."""
        ...

    def login(self, definition: SupplierDefinition, credentials: Credentials) -> bytes:
        """Submit the login form once and return the resulting session payload.

        Raises ``AuthError`` when the supplier rejects the credentials, and ``TransientError`` or
        ``RateLimitedError`` for site or network trouble. Never retries a rejected login.
        """
        ...
