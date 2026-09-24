"""When review reconciliation runs, and how missed work is recovered (Gate 2 G2-B, ADR-0016 §4).

**Fast path.** After the owner's own unit of work commits, the scope it changed is reconciled at
once. It is not the recovery guarantee: it depends on an event, and it never touches the owner's
write. A fast-path failure is recorded durably as a known indexing failure, and it is never raised
into the owner's work.

**Safety net.** Every wired producer runs a full reconciliation:
- at process startup, before the application serves;
- periodically while the process runs, at a finite, server-owned interval.

Both are independent of owner writes, re-evaluation and resolution.

A full pass visits every scope the owner holds and every scope the producer's items hold, in one
deterministic order. Each scope is its own short locked unit (derive and apply together, as the
owner requires), so a pass makes monotonic forward progress, and a crash loses at most the scope
in flight. The watermark is renewed **only when the pass reached its end with no failure**. A
failed scope is recorded as a known failure, and the watermark stays where it was.
"""

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.clock import Clock
from app.core.correlation import new_correlation_id
from app.core.errors import AppError
from app.review.coverage import CoverageView, ReviewCoverageStore
from app.review.model import canonical_scope
from app.review.owner import ReviewItemStore

logger = logging.getLogger("icbm.review.reconciler")

REVIEW_INDEX_FAILED = "REVIEW_INDEX_FAILED"


@dataclass(frozen=True)
class FullPass:
    producer: str
    scopes: int
    failed: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.failed


def _code(exc: BaseException) -> str:
    return exc.code if isinstance(exc, AppError) else REVIEW_INDEX_FAILED


class ReviewReconciler:
    def __init__(
        self,
        store: ReviewItemStore,
        coverage: ReviewCoverageStore,
        clock: Clock,
        *,
        process_run_id: str,
        interval_s: float,
        max_age_s: float,
    ) -> None:
        self._store = store
        self._coverage = coverage
        self._clock = clock
        self._process_run_id = process_run_id
        self._interval_s = interval_s
        self._max_age_s = max_age_s
        self._task: asyncio.Task[None] | None = None
        self._stopping: asyncio.Event | None = None

    @property
    def process_run_id(self) -> str:
        return self._process_run_id

    # -------------------------------------------------------------- the fast path

    def index_scope(self, producer: str, scope: Mapping[str, str]) -> bool:
        """Reconcile one scope right after its owner changed. Never raises into the caller."""
        correlation_id = new_correlation_id()
        try:
            self._store.reconcile(producer, scope=scope, correlation_id=correlation_id)
            return True
        except Exception as exc:
            logger.warning("review.index_failed", extra={"producer": producer, "code": _code(exc)})
            self._record_failure(producer, _code(exc), correlation_id)
            return False

    # -------------------------------------------------------------- the safety net

    def full_pass(self, producer: str) -> FullPass:
        """One complete reconciliation of a producer, scope by scope. Never raises."""
        correlation_id = new_correlation_id()
        started_at = self._clock.now()
        try:
            scopes = self._scopes(producer)
        except Exception as exc:
            self._record_failure(producer, _code(exc), correlation_id)
            return FullPass(producer, 0, (_code(exc),))
        failed: list[str] = []
        for scope in scopes:
            try:
                self._store.reconcile(producer, scope=scope, correlation_id=correlation_id)
            except Exception as exc:
                failed.append(_code(exc))
                logger.warning(
                    "review.full_pass_scope_failed",
                    extra={"producer": producer, "code": _code(exc)},
                )
        if failed:
            self._record_failure(producer, failed[0], correlation_id)
        else:
            try:
                self._coverage.record_pass(
                    producer,
                    process_run_id=self._process_run_id,
                    started_at=started_at,
                    correlation_id=correlation_id,
                )
            except Exception as exc:
                failed.append(_code(exc))
                logger.warning("review.watermark_failed", extra={"producer": producer})
        return FullPass(producer, len(scopes), tuple(failed))

    def full_passes(self) -> tuple[FullPass, ...]:
        return tuple(self.full_pass(producer) for producer in self._store.producers)

    def coverage(self) -> tuple[CoverageView, ...]:
        return tuple(
            self._coverage.coverage(
                producer, process_run_id=self._process_run_id, max_age_s=self._max_age_s
            )
            for producer in self._store.producers
        )

    def _scopes(self, producer: str) -> Sequence[Mapping[str, str]]:
        """What the owner holds and what the producer's items hold, once each, in order. A scope
        the owner cannot express canonically is still visited, and fails there."""
        merged: dict[str, Mapping[str, str]] = {}
        for scope in (*self._store.producer(producer).scopes(), *self._store.scopes(producer)):
            try:
                key = json.dumps(canonical_scope(scope), sort_keys=True)
            except AppError:
                key = json.dumps(dict(scope), sort_keys=True, ensure_ascii=True)
            merged.setdefault(key, scope)
        return [merged[key] for key in sorted(merged)]

    def _record_failure(self, producer: str, code: str, correlation_id: str) -> None:
        try:
            self._coverage.record_failure(producer, code=code, correlation_id=correlation_id)
        except Exception:
            # The watermark cannot be renewed either while writes fail, so the freshness bound
            # still makes coverage non-current; this only loses the explicit reason.
            logger.exception("review.failure_not_recorded", extra={"producer": producer})

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """The startup pass, awaited before the application serves, then the periodic loop."""
        await asyncio.to_thread(self.full_passes)
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="icbm-review-reconciler")

    async def _run(self) -> None:
        assert self._stopping is not None
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
            except TimeoutError:
                try:
                    await asyncio.to_thread(self.full_passes)
                except Exception:
                    logger.exception("review.periodic_pass_error")

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=30.0)
            except TimeoutError:
                self._task.cancel()
            self._task = None
