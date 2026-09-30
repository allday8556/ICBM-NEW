import json
import subprocess
import sys

import pytest

from app.platform.core.egress import EgressBlockedError, EgressGuard


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("socket.connect", (None, ("127.0.0.1", 8765))),
        ("socket.connect", (None, ("::1", 8765, 0, 0))),
        ("socket.connect", (None, "/tmp/socket")),
        ("socket.getaddrinfo", ("localhost", 80, 0, 0, 0)),
        ("socket.getaddrinfo", (None, 80, 0, 0, 0)),
        ("open", ("file.txt", "r", 0)),
    ],
)
def test_loopback_and_unrelated_events_pass(event: str, args: tuple[object, ...]) -> None:
    guard = EgressGuard()
    guard._hook(event, args)
    assert guard.external_attempts == 0


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("socket.connect", (None, ("203.0.113.7", 443))),
        ("socket.getaddrinfo", ("smartstore.naver.com", 443, 0, 0, 0)),
        ("socket.getaddrinfo", (b"example.com", 80, 0, 0, 0)),
        ("socket.sendto", (None, ("8.8.8.8", 53))),
        ("socket.gethostbyname", ("example.com",)),
    ],
)
def test_external_destinations_are_blocked_and_counted(
    event: str, args: tuple[object, ...]
) -> None:
    guard = EgressGuard()
    with pytest.raises(EgressBlockedError):
        guard._hook(event, args)
    assert guard.external_attempts == 1
    assert guard.snapshot()["recent"][0]["event"] == event


def test_installed_guard_blocks_real_sockets_in_a_fresh_interpreter() -> None:
    """Runs in a subprocess because an audit hook can never be removed from a process."""
    program = """
import json, socket
from app.platform.core.egress import EGRESS, EgressBlockedError
EGRESS.install()
results = {}
for name, call in {
    "dns": lambda: socket.getaddrinfo("example.invalid", 80),
    "connect": lambda: socket.create_connection(("203.0.113.9", 80), timeout=1),
}.items():
    try:
        call()
        results[name] = "allowed"
    except EgressBlockedError:
        results[name] = "blocked"
listener = socket.create_server(("127.0.0.1", 0))
socket.create_connection(listener.getsockname(), timeout=1).close()
results["loopback"] = "allowed"
results["attempts"] = EGRESS.snapshot()["external_attempts"]
print(json.dumps(results))
"""
    completed = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=60, check=True
    )
    results = json.loads(completed.stdout.strip().splitlines()[-1])
    assert results == {"dns": "blocked", "connect": "blocked", "loopback": "allowed", "attempts": 2}


# ---------------------------------------------------------------- test isolation of the one guard


def _attempt() -> None:
    from app.platform.core.egress import EGRESS

    with pytest.raises(EgressBlockedError):
        EGRESS._hook("socket.connect", (None, ("203.0.113.7", 443)))


def test_a_deliberate_attempt_is_counted_inside_its_block_and_gone_after_it() -> None:
    from app.platform.core.egress import EGRESS
    from tests.conftest import deliberate_egress_attempts

    before = EGRESS.snapshot()
    with deliberate_egress_attempts():
        _attempt()
        # Inside the block the production guard counts it, as always.
        inside = EGRESS.snapshot()
        assert inside["external_attempts"] == before["external_attempts"] + 1
        assert inside["recent"][-1]["destination"] == "203.0.113.7"
    assert EGRESS.snapshot() == before


def test_the_restore_also_happens_when_the_test_body_fails() -> None:
    from app.platform.core.egress import EGRESS
    from tests.conftest import deliberate_egress_attempts

    before = EGRESS.snapshot()
    with pytest.raises(RuntimeError, match="the test failed"), deliberate_egress_attempts():
        _attempt()
        raise RuntimeError("the test failed")
    assert EGRESS.snapshot() == before


def test_an_attempt_outside_the_block_stays_counted() -> None:
    """The isolation is opt-in: an attempt no test declared is never rewound, so a later
    readiness check still exposes it. (This test declares its own to clean up after itself.)"""
    from app.platform.core.egress import EGRESS
    from tests.conftest import deliberate_egress_attempts

    with deliberate_egress_attempts():
        before = EGRESS.external_attempts
        _attempt()
        with deliberate_egress_attempts():
            _attempt()
        # The inner block rewound only its own attempt; the outer one is still counted.
        assert EGRESS.external_attempts == before + 1
