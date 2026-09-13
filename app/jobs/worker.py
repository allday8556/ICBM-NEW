"""The single in-process background worker (ADR-0002 option A).

Runs as one asyncio task inside the FastAPI process. Every blocking runner call happens in a
thread, so the event loop — and therefore the API — stays responsive while jobs execute.
"""

import asyncio
import contextlib
import logging
from datetime import datetime

from app.core.clock import Clock
from app.jobs.runner import JobRunner

logger = logging.getLogger("icbm.jobs")


class JobWorker:
    def __init__(self, runner: JobRunner, *, poll_interval_s: float, clock: Clock) -> None:
        self._runner = runner
        self._poll_interval_s = poll_interval_s
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False
        self.started_at: datetime | None = None
        self.last_heartbeat: datetime | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            raise RuntimeError("the job worker is already running")
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._stopping = False
        recovered = await asyncio.to_thread(self._runner.recover_interrupted)
        self.started_at = self._clock.now()
        self.last_heartbeat = self.started_at
        self._task = asyncio.create_task(self._run(), name="icbm-job-worker")
        logger.info(
            "job.worker.started",
            extra={"recovered_jobs": recovered, "poll_interval_s": self._poll_interval_s},
        )

    def notify(self) -> None:
        """Wake the worker early. Safe to call from any thread."""
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(wake.set)

    async def _run(self) -> None:
        wake = self._wake
        assert wake is not None
        while not self._stopping:
            self.last_heartbeat = self._clock.now()
            wait_s: float | None = None
            try:
                if await asyncio.to_thread(self._runner.run_next) is not None:
                    continue
                wait_s = await asyncio.to_thread(self._runner.seconds_until_next_due)
            except Exception:
                # Infrastructure failure outside the job boundary (e.g. database locked).
                # Log and keep the loop alive; it must never take the web application down.
                logger.exception("job.worker.loop_error")
            timeout = (
                self._poll_interval_s if wait_s is None else min(self._poll_interval_s, wait_s)
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=timeout)
            wake.clear()

    async def stop(self, timeout_s: float = 30.0) -> None:
        task = self._task
        if task is None:
            return
        self._stopping = True
        if self._wake is not None:
            self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout_s)
        except TimeoutError:
            task.cancel()
            logger.error("job.worker.stop_timeout", extra={"timeout_s": timeout_s})
        self._task = None
        logger.info("job.worker.stopped")
