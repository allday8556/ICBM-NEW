"""Single-flight authentication (Issue #7 comment 5653608622 §4, PR #9 review 5191372031).

Callers that need a connection for the same supplier while one is already being established do
not start their own: they wait for the flight in progress and share its outcome — its proof *or
its failure*. A rejected login is therefore submitted once per burst, never once per waiter.
Callers arriving after a flight finished start a new one, subject to the ordinary state rules
and the PAUSED loop guard.

The flight runs under a per-supplier lifecycle lock that credential replacement also takes, so
new credentials can never be saved underneath an authentication still using the old ones.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar, cast

T = TypeVar("T")


@dataclass
class _Flight:
    done: threading.Event = field(default_factory=threading.Event)
    waiters: int = 0
    result: object = None
    error: BaseException | None = None


class SingleFlightAuth:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._flights: dict[str, _Flight] = {}
        self._lifecycle: dict[str, threading.Lock] = {}

    def lifecycle(self, key: str) -> threading.Lock:
        """The per-supplier operation lock: held by a flight, and by credential replacement."""
        with self._guard:
            return self._lifecycle.setdefault(key, threading.Lock())

    def waiters(self, key: str) -> int:
        """Callers currently waiting on the flight for ``key`` (diagnostics and tests)."""
        with self._guard:
            flight = self._flights.get(key)
            return flight.waiters if flight else 0

    def run(self, key: str, operation: Callable[[], T]) -> T:
        with self._guard:
            flight = self._flights.get(key)
            owner = flight is None
            if flight is None:
                flight = self._flights[key] = _Flight()
            else:
                flight.waiters += 1
        if not owner:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return cast(T, flight.result)
        try:
            with self.lifecycle(key):
                result = operation()
        except BaseException as exc:
            flight.error = exc
            raise
        else:
            flight.result = result
            return result
        finally:
            with self._guard:
                del self._flights[key]
            flight.done.set()
