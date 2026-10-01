"""Replay protection for the extension pairing (E1 specification ``5907009512`` §2.2).

A bounded, in-memory cache of the nonces each pairing has presented, each kept for a fixed time to
live. The time to live is longer than the accepted timestamp window, so a nonce can never be
forgotten while a request carrying it would still be accepted.

- A nonce seen inside its time to live is refused.
- The cache is bounded per pairing. When it is full the request is refused, fail closed; a live
  entry is never evicted to make room.
- It lives in this process only: no database table and no file. A restart empties it, which is
  accepted — the timestamp window still bounds what a replay could be.
- It is safe to call from several threads. The policy route runs in the server's thread pool, so
  two requests carrying one nonce can arrive together; the check and the insert are one step
  under a lock, and exactly one of them is accepted.
"""

import threading
from datetime import datetime, timedelta

from app.platform.core.clock import Clock
from app.platform.core.errors import AuthError, RateLimitedError

# Twice the accepted timestamp window (``pairing.TIMESTAMP_WINDOW_S``) and a margin: a request is
# accepted at most one window after its own timestamp, so its nonce outlives its validity.
NONCE_TTL_S = 300
NONCE_CAPACITY = 1024


class NonceReplayed(AuthError):
    """The request presents a nonce this pairing already used."""


class NonceCacheFull(RateLimitedError):
    """The cache holds its bound of live nonces; nothing is accepted until one expires."""


class NonceCache:
    def __init__(
        self, clock: Clock, *, ttl_s: int = NONCE_TTL_S, capacity: int = NONCE_CAPACITY
    ) -> None:
        if ttl_s < 1 or capacity < 1:
            raise ValueError("the nonce cache needs a positive time to live and capacity")
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_s)
        self._capacity = capacity
        self._seen: dict[str, dict[str, datetime]] = {}
        self._lock = threading.Lock()

    def consume(self, pairing_id: str, nonce: str) -> None:
        """Record one nonce as used, or refuse it as a replay or because the cache is full."""
        with self._lock:
            now = self._clock.now()
            live = self._seen.setdefault(pairing_id, {})
            for known in [key for key, expires in live.items() if expires <= now]:
                del live[known]
            if nonce in live:
                raise NonceReplayed(
                    "EXTENSION_NONCE_REPLAYED", "the request nonce was already used"
                )
            if len(live) >= self._capacity:
                raise NonceCacheFull(
                    "EXTENSION_NONCE_CACHE_FULL", "too many requests are inside the replay window"
                )
            live[nonce] = now + self._ttl

    def size(self, pairing_id: str) -> int:
        with self._lock:
            return len(self._seen.get(pairing_id, {}))

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()
