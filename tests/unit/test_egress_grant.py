"""The egress guard's scoped supplier grant, driven through the audit hook itself (no network)."""

import threading

import pytest

from app.core import egress
from app.core.egress import EgressBlockedError, EgressGuard

SUPPLIER_IP = "203.0.113.7"


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> EgressGuard:
    monkeypatch.setattr(egress, "_resolve", lambda hosts: {SUPPLIER_IP} if hosts else set())
    return EgressGuard()


def _lookup(guard: EgressGuard, host: str) -> None:
    guard._hook("socket.getaddrinfo", (host, 443, 0, 0, 0, 0))


def _connect(guard: EgressGuard, address: str) -> None:
    guard._hook("socket.connect", (object(), (address, 443)))


def test_without_a_grant_every_external_destination_is_blocked(guard: EgressGuard) -> None:
    for attempt in (lambda: _lookup(guard, "supplier.test"), lambda: _connect(guard, SUPPLIER_IP)):
        with pytest.raises(EgressBlockedError):
            attempt()
    assert guard.external_attempts == 2


def test_loopback_is_always_allowed(guard: EgressGuard) -> None:
    _lookup(guard, "localhost")
    _connect(guard, "127.0.0.1")
    assert guard.external_attempts == 0


def test_a_grant_allows_only_its_own_hosts_and_their_addresses(guard: EgressGuard) -> None:
    with guard.grant("supplier:test", {"Supplier.Test."}):
        _lookup(guard, "supplier.test")
        _connect(guard, SUPPLIER_IP)
        with pytest.raises(EgressBlockedError):
            _lookup(guard, "tracker.example")
        with pytest.raises(EgressBlockedError):
            _connect(guard, "198.51.100.9")
    with pytest.raises(EgressBlockedError):
        _lookup(guard, "supplier.test")
    snapshot = guard.snapshot()
    assert snapshot["external_attempts"] == 3
    assert snapshot["granted_events"] == {"supplier:test": 2}
    assert snapshot["policy"] == "BLOCK_EXTERNAL_EXCEPT_GRANTED"


def test_a_grant_does_not_reach_other_threads(guard: EgressGuard) -> None:
    outcome: list[BaseException | None] = []

    def elsewhere() -> None:
        try:
            _lookup(guard, "supplier.test")
            outcome.append(None)
        except EgressBlockedError as exc:
            outcome.append(exc)

    with guard.grant("supplier:test", {"supplier.test"}):
        thread = threading.Thread(target=elsewhere)
        thread.start()
        thread.join(5)
    assert isinstance(outcome[0], EgressBlockedError)


@pytest.mark.parametrize("hosts", [set(), {"localhost"}, {""}])
def test_a_grant_needs_real_external_hosts(guard: EgressGuard, hosts: set[str]) -> None:
    with pytest.raises(ValueError), guard.grant("supplier:test", hosts):
        pass
