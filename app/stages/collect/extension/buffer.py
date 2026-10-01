"""The in-process handoff of an accepted capture to its job (ruling ``5906712259`` N-1).

An accepted capture's sanitized HTML is never written to the database, a job payload, the
filesystem, a log or any other durable store. It waits here, in this process's memory, keyed by the
run that was opened for it, until that run's job takes it. The durable job payload holds
identifiers and provenance only.

This is valid only while the job worker runs inside the application process (ADR-0002 Option A):
the request thread puts, the worker thread of the same process takes. A process that restarts
before the job ran finds nothing here; the job then ends with a fixed code and is never retried,
because there is nothing to retry with.

The buffer is bounded. A capture that does not fit is refused before its run exists.
"""

import threading
from dataclasses import dataclass, field

from app.platform.core.errors import RateLimitedError
from app.stages.collect.extension.capture import TransportEvidence

# One capture is accepted per pairing at a time (ruling 5906290729 B-4), so one entry is the normal
# case. The small margin covers a run that settled through the terminal hook before its job ran.
BUFFER_CAPACITY = 4


class CaptureBufferFull(RateLimitedError):
    """The buffer holds its bound of captures waiting for their jobs."""


@dataclass(frozen=True)
class BufferedCapture:
    """One accepted capture, in memory only."""

    html: str = field(repr=False)
    evidence: TransportEvidence


class CaptureBuffer:
    def __init__(self, *, capacity: int = BUFFER_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("the capture buffer needs a positive capacity")
        self._capacity = capacity
        self._held: dict[str, BufferedCapture] = {}
        self._lock = threading.Lock()

    def put(self, collection_run_id: str, capture: BufferedCapture) -> None:
        with self._lock:
            if len(self._held) >= self._capacity:
                raise CaptureBufferFull(
                    "EXTENSION_CAPTURE_BUFFER_FULL", "too many captures are waiting to be processed"
                )
            self._held[collection_run_id] = capture

    def take(self, collection_run_id: str) -> BufferedCapture | None:
        """Hand over a run's capture exactly once. ``None`` means this process never held it."""
        with self._lock:
            return self._held.pop(collection_run_id, None)

    def discard(self, collection_run_id: str) -> None:
        with self._lock:
            self._held.pop(collection_run_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._held)
