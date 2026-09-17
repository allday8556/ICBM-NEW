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

# How often an idle worker re-checks that nothing durable is waiting on a job that is already
# over. The sweep is the authority for that (ruling 5721367502 S2), and the recovery boundary at
# startup is where it matters most; this only keeps a long-running process from carrying an
# inconsistency until its next restart.
RECONCILE_INTERVAL_S = 60.0


class JobWorker:
    def __init__(
        self,
        runner: JobRunner,
        *,
        poll_interval_s: float,
        clock: Clock,
        reconcile_interval_s: float = RECONCILE_INTERVAL_S,
    ) -> None:
        self._runner = runner
        self._poll_interval_s = poll_interval_s
        self._clock = clock
        self._reconcile_interval_s = reconcile_interval_s
        self._reconciled_at: datetime | None = None
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
        # The recovery boundary (ruling 5721367502 S3): before this process serves a read-back of
        # anything, every job the database says is over has told its owner — whether or not the
        # process that ended it managed to.
        unsettled = await self._reconcile()
        self.started_at = self._clock.now()
        self.last_heartbeat = self.started_at
        self._task = asyncio.create_task(self._run(), name="icbm-job-worker")
        logger.info(
            "job.worker.started",
            extra={
                "recovered_jobs": recovered,
                "unsettled_owners": unsettled,
                "poll_interval_s": self._poll_interval_s,
            },
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
                # Nothing is due, which is the moment to check that nothing durable is waiting on
                # a job that is already over. A process that runs for weeks reaches its own
                # recovery boundary here rather than at the next restart.
                await self._reconcile_if_due()
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

    async def _reconcile(self) -> int:
        """Run one sweep in a thread. The timestamp is taken first, so a sweep that fails is
        retried on the interval rather than on every pass of an idle loop."""
        self._reconciled_at = self._clock.now()
        return await asyncio.to_thread(self._runner.reconcile_terminal_owners)

    async def _reconcile_if_due(self) -> None:
        last = self._reconciled_at
        if last is not None and (self._clock.now() - last).total_seconds() < (
            self._reconcile_interval_s
        ):
            return
        await self._reconcile()

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
