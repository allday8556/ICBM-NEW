"""The SmartStore session keeper: automatic token renewal (owner decision 2026-10-07, M6).

AUTH §15 renews inside the provider window with a configured margin, serialized per account, and
re-proves the account identity after every new session; §17 Case B recovers an expired token
without user approval. The keeper runs exactly the CONNECT owner's own pass (:meth:`connect`) when
:meth:`auto_renewal_due` says so — at startup and whenever the committed bearer is missing or about
to expire — and nothing else: no second token owner, no second store. A failed pass backs off; an
AUTHENTICATION review (a mismatch or a refused credential) stops it until the operator acts.
"""

import logging
import threading
from typing import Final, Protocol

logger = logging.getLogger("icbm.connect.smartstore.keeper")

# The first check soon after startup (AUTH §17 convergence), then one check a minute.
FIRST_CHECK_S: Final = 5.0
TICK_S: Final = 60.0
# A failed or unproven pass waits, doubling, between these bounds.
BACKOFF_MIN_S: Final = 300.0
BACKOFF_MAX_S: Final = 1800.0


class Renewable(Protocol):
    def auto_renewal_due(self) -> bool: ...

    def connect(self) -> object: ...


class SmartStoreSessionKeeper:
    def __init__(
        self,
        service: Renewable,
        *,
        enabled: bool,
        first_check_s: float = FIRST_CHECK_S,
        tick_s: float = TICK_S,
    ) -> None:
        self._service = service
        self._enabled = enabled
        self._first_check_s = first_check_s
        self._tick_s = tick_s
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._backoff = 0.0

    def start(self) -> None:
        if not self._enabled:
            return
        self._halt.clear()
        self._thread = threading.Thread(
            target=self._loop, name="icbm-smartstore-session-keeper", daemon=True
        )
        self._thread.start()

    def check(self) -> bool:
        """One check: run CONNECT when due. Whether a pass ran and left the session current."""
        if not self._service.auto_renewal_due():
            self._backoff = 0.0
            return False
        try:
            self._service.connect()
        except Exception:
            logger.warning("smartstore.session_keeper.connect_failed", exc_info=True)
            self._backoff = min(max(self._backoff * 2, BACKOFF_MIN_S), BACKOFF_MAX_S)
            return False
        if self._service.auto_renewal_due():
            # The pass ran but proved no usable session: wait before the next one.
            self._backoff = min(max(self._backoff * 2, BACKOFF_MIN_S), BACKOFF_MAX_S)
            return False
        self._backoff = 0.0
        logger.info("smartstore.session_keeper.renewed")
        return True

    def _loop(self) -> None:
        wait = self._first_check_s
        while not self._halt.wait(wait):
            try:
                self.check()
            except Exception:
                logger.exception("smartstore.session_keeper.error")
            wait = self._backoff or self._tick_s

    def stop(self) -> None:
        self._halt.set()
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None


__all__ = ["SmartStoreSessionKeeper"]
