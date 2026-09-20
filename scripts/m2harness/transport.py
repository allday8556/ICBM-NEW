"""The acceptance-only transport gate under the real SmartStore caller (Issue #46 §2.1).

``SmartStoreEndpointCaller`` stays the only SmartStore client. It resolves the adopted endpoint,
composes and validates the request, opens the egress grant for the provider host, applies the
contract's timeouts and success predicate, and logs the call. The harness only passes it this
transport. For every request the caller hands over, the gate

1. maps the request to an adopted endpoint by exact scheme, host, port, method and path taken from
   the endpoint registry. Anything else is a forbidden target, whose cap is 0;
2. checks and durably reserves the request in the campaign ledger;
3. only then hands the request to the inner transport, and records the response's HTTP status and
   latency, or that no response arrived.

A refused request never reaches the inner transport. It is raised as an ``EgressBlockedError``, so
the caller classifies it as transmission precluded (ERRORS §15.1), which is what it is.

The inner transport is the fake provider in DRY mode. In REAL mode it is a fresh
``httpx.HTTPTransport`` configured exactly like the one ``httpx.Client(trust_env=False)`` builds
for the caller when no transport is passed. The live factory refuses to exist under CI or pytest.
"""

import os
import re
import sys
import time
from collections.abc import Callable, Mapping

import httpx

from app.core.egress import EgressBlockedError
from integrations.marketplaces.smartstore.registry import ADOPTED, BASE_URL
from scripts.m2harness.ledger import UNRECOGNIZED, Ledger, Phase, ReservationRefused

# Builds the inner transport for one reserved request; it receives the request's step label.
InnerFactory = Callable[[str], httpx.BaseTransport]
# Environment markers of CI and test runs: the live provider transport never exists under them.
CI_MARKERS = ("CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST")


class BudgetGateRefused(EgressBlockedError):
    """The campaign ledger refused the request before any transport received it."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"the M2 campaign ledger refused this request before send ({reason})")
        self.reason = reason


class LiveProviderRefused(RuntimeError):
    """The live SmartStore transport was requested where it may never exist."""


def _path_pattern(base_path: str, template: str) -> re.Pattern[bytes]:
    """The exact path of one adopted endpoint. A templated segment matches one conservative
    segment only — the same shape the caller allows — so nothing wider is ever recognized."""
    parts = [re.escape(part) for part in re.split(r"\{[A-Za-z][A-Za-z0-9]*\}", template)]
    pattern = "[A-Za-z0-9_-]{1,64}".join(parts)
    return re.compile(f"^{re.escape(base_path)}{pattern}$".encode("ascii"))


def resolve_target(request: httpx.Request) -> str:
    """The adopted endpoint id this exact request is, or ``UNRECOGNIZED``."""
    base = httpx.URL(BASE_URL)
    url = request.url
    if (url.scheme, url.host, url.port, url.userinfo) != (base.scheme, base.host, base.port, b""):
        return UNRECOGNIZED
    for contract in ADOPTED.values():
        pattern = _path_pattern(base.path, contract.path)
        if request.method == contract.method.value and pattern.fullmatch(url.raw_path):
            return contract.endpoint_id.value
    return UNRECOGNIZED


def ci_or_test(environ: Mapping[str, str] | None = None) -> str | None:
    """Why this process is a CI or test run, or None."""
    env = os.environ if environ is None else environ
    for name in CI_MARKERS:
        if env.get(name):
            return name
    return "pytest" if "pytest" in sys.modules else None


def live_inner(environ: Mapping[str, str] | None = None) -> InnerFactory:
    """The REAL-mode inner transport factory. It refuses under CI or pytest, now and per call."""
    blocker = ci_or_test(environ)
    if blocker is not None:
        raise LiveProviderRefused(f"the live SmartStore transport never exists under {blocker}")

    def build(label: str) -> httpx.BaseTransport:
        del label
        late = ci_or_test()
        if late is not None:
            raise LiveProviderRefused(f"the live SmartStore transport never exists under {late}")
        # What httpx.Client(trust_env=False) builds for the caller when it is given no transport:
        # TLS verification, HTTP/1.1, default pool limits, no environment proxy, no retries.
        return httpx.HTTPTransport(trust_env=False)

    return build


class BudgetedTransport(httpx.BaseTransport):
    """Reserve in the ledger, then delegate. Used only as the caller's transport."""

    def __init__(self, ledger: Ledger, *, phases: frozenset[Phase], inner: InnerFactory) -> None:
        self._ledger = ledger
        self._phases = phases
        self._inner = inner
        self._open: list[httpx.BaseTransport] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        endpoint = resolve_target(request)
        try:
            reservation = self._ledger.reserve(endpoint, phases=self._phases, pid=os.getpid())
        except ReservationRefused as exc:
            raise BudgetGateRefused(exc.reason.value) from exc
        started = time.monotonic()
        try:
            inner = self._inner(reservation.label)
            self._open.append(inner)
            response = inner.handle_request(request)
        except BaseException:
            self._ledger.complete(reservation.seq, http_status=None, latency_ms=_ms(started))
            raise
        self._ledger.complete(
            reservation.seq, http_status=response.status_code, latency_ms=_ms(started)
        )
        return response

    def close(self) -> None:
        while self._open:
            self._open.pop().close()


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
