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

_SEEN: dict[str, int] = {}


def test_a_blocked_attempt_is_counted_inside_the_test_that_made_it() -> None:
    from app.platform.core.egress import EGRESS

    before = EGRESS.external_attempts
    with pytest.raises(EgressBlockedError):
        EGRESS._hook("socket.connect", (None, ("203.0.113.7", 443)))
    # The production guard counts it, as always: nothing about the guard itself is relaxed.
    assert EGRESS.external_attempts == before + 1
    assert EGRESS.snapshot()["recent"][-1]["destination"] == "203.0.113.7"
    _SEEN["before"] = before


def test_the_next_test_starts_from_the_count_the_previous_one_found() -> None:
    """The process-global guard never leaks one test's deliberate attempt into another test
    (``tests/conftest.py::egress_count_is_per_test``). This runs right after the test above."""
    from app.platform.core.egress import EGRESS

    assert EGRESS.external_attempts == _SEEN["before"]
    assert all(entry["destination"] != "203.0.113.7" for entry in EGRESS.snapshot()["recent"])
