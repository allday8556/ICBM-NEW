"""Per-supplier request pacing: a concurrency cap and a minimum spacing between request starts."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from integrations.suppliers.base import SupplierProfile


@dataclass
class _Lane:
    slots: threading.BoundedSemaphore
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_start: float | None = None


class RequestPacer:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._guard = threading.Lock()
        self._lanes: dict[str, _Lane] = {}

    def _lane(self, profile: SupplierProfile) -> _Lane:
        with self._guard:
            lane = self._lanes.get(profile.supplier_key)
            if lane is None:
                slots = threading.BoundedSemaphore(profile.request_policy.max_concurrency)
                lane = self._lanes[profile.supplier_key] = _Lane(slots=slots)
            return lane

    @contextmanager
    def slot(self, profile: SupplierProfile) -> Iterator[None]:
        lane = self._lane(profile)
        interval = profile.request_policy.minimum_request_interval_s
        with lane.slots:
            with lane.lock:
                if lane.last_start is not None:
                    wait = lane.last_start + interval - self._monotonic()
                    if wait > 0:
                        self._sleep(wait)
                lane.last_start = self._monotonic()
            yield
