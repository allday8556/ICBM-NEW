"""Single-flight authentication (Issue #7 comment 5653608622 §4).

Concurrent callers that need a login for the same supplier collapse into one real login: the
first caller authenticates while the others wait, and a waiter that finds an authentication was
completed while it waited reuses that result instead of submitting credentials again.
"""

import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class Flight:
    # True when another caller completed an authentication while this one waited.
    superseded: bool
    _owner: "SingleFlightAuth"
    _key: str

    def completed(self) -> None:
        """Record that this flight produced a new authenticated session."""
        self._owner._complete(self._key)


class SingleFlightAuth:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._generation: Counter[str] = Counter()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _complete(self, key: str) -> None:
        with self._guard:
            self._generation[key] += 1

    @contextmanager
    def flight(self, key: str) -> Iterator[Flight]:
        with self._guard:
            seen = self._generation[key]
        lock = self._lock_for(key)
        with lock:
            with self._guard:
                superseded = self._generation[key] != seen
            yield Flight(superseded=superseded, _owner=self, _key=key)
