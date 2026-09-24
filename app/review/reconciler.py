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
in flight. The watermark is renewed **only when the pass reached its end with no failure and the
owner did not move during it**: the owner's truth token is taken as the pass begins and compared
again inside the watermark's own write unit. A failed scope, or an owner that moved, is recorded as
a known failure, and the watermark stays where it was until a later, stable pass.
"""

import asyncio
import json
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.clock import Clock
from app.core.correlation import new_correlation_id
from app.core.errors import AppError
from app.review.coverage import (
    REVIEW_OWNER_MOVED_DURING_PASS,
    REVIEW_OWNER_MOVED_SINCE_PASS,
    CoverageView,
    ReviewCoverageStore,
)
from app.review.model import canonical_scope
from app.review.owner import ReviewItemStore

logger = logging.getLogger("icbm.review.reconciler")

REVIEW_INDEX_FAILED = "REVIEW_INDEX_FAILED"
# How many times a full pass the owner moved under is retried at once (G2-C).
CHURN_RETRIES = 2


@dataclass(frozen=True)
class FullPass:
    producer: str
    scopes: int
    failed: tuple[str, ...]
    # Stopped by shutdown before its end: incomplete, so it renews no watermark, and not a failure.
    stopped: bool = False

    @property
    def complete(self) -> bool:
        return not self.failed and not self.stopped


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
        # Shutdown (review 5807477351 B1): the periodic loop runs on a thread this reconciler owns
        # and joins. A pass checks the halt between scopes, and stop() returns only once that
        # thread has ended, so no pass can still write after the owner lease is released.
        self._halt = threading.Event()
        # A request for a prompt full pass (G2-C): the fast path, and a reader that finds the owner
        # moved since the watermark, ask for one instead of waiting out the periodic interval.
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

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
        finally:
            # The owner moved, so its watermark no longer covers it (coverage condition 5): a full
            # pass is what makes it current again, and it is asked for now.
            self.request_pass()

    def request_pass(self) -> None:
        """Ask the periodic loop for a full pass now. It never runs one in the caller's thread."""
        self._wake.set()

    # -------------------------------------------------------------- the safety net

    def full_pass(self, producer: str) -> FullPass:
        """One complete reconciliation of a producer, scope by scope. Never raises."""
        correlation_id = new_correlation_id()
        started_at = self._clock.now()
        try:
            # Which failures already existed when this pass began (review 5807477351 B2): only
            # those can be recovered by it, whatever the clock says.
            failures_before = self._coverage.failures_recorded(producer)
            # The owner fence (review 5807902325 B3): the owner's truth, including each scope's
            # current source identity, as the pass begins. It is compared again inside the
            # watermark's own write unit.
            source = self._store.producer(producer)
            token_before = source.truth_token()
            scopes = self._scopes(producer)
        except Exception as exc:
            self._record_failure(producer, _code(exc), correlation_id)
            return FullPass(producer, 0, (_code(exc),))
        failed: list[str] = []
        for scope in scopes:
            if self._halt.is_set():
                return FullPass(producer, len(scopes), tuple(failed), stopped=True)
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
                published = self._coverage.record_pass(
                    producer,
                    process_run_id=self._process_run_id,
                    started_at=started_at,
                    failures_before=failures_before,
                    token_before=token_before,
                    owner_token=source.truth_token,
                    correlation_id=correlation_id,
                )
                if not published:
                    failed.append(REVIEW_OWNER_MOVED_DURING_PASS)
            except Exception as exc:
                failed.append(_code(exc))
                logger.warning("review.watermark_failed", extra={"producer": producer})
        return FullPass(producer, len(scopes), tuple(failed))

    def full_passes(self) -> tuple[FullPass, ...]:
        """One full pass of every wired producer. A pass the owner moved under is retried at
        once, at most ``CHURN_RETRIES`` times: under whole-owner churn every attempt still indexes
        its scopes in their own units (progress), and none publishes a watermark it cannot fence
        (safety). What remains is left to the next requested or periodic pass."""
        passes: list[FullPass] = []
        for producer in self._store.producers:
            for _ in range(1 + CHURN_RETRIES):
                if self._halt.is_set():
                    return tuple(passes)
                result = self.full_pass(producer)
                passes.append(result)
                if REVIEW_OWNER_MOVED_DURING_PASS not in result.failed:
                    break
        return tuple(passes)

    def coverage(self) -> tuple[CoverageView, ...]:
        """Every wired producer's coverage now, each checked against its owner's truth now. An
        owner found moved since its watermark asks for a full pass; it is still not current."""
        views = tuple(
            self._coverage.coverage(
                producer,
                process_run_id=self._process_run_id,
                max_age_s=self._max_age_s,
                owner_token=self._store.producer(producer).truth_token,
            )
            for producer in self._store.producers
        )
        if any(view.reason == REVIEW_OWNER_MOVED_SINCE_PASS for view in views):
            self.request_pass()
        return views

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
        """The startup pass, awaited before the application serves, then the periodic loop on a
        thread this reconciler owns."""
        self._halt.clear()
        self._wake.clear()
        await asyncio.to_thread(self.full_passes)
        self._thread = threading.Thread(
            target=self._periodic, name="icbm-review-reconciler", daemon=True
        )
        self._thread.start()

    def _periodic(self) -> None:
        """A full pass every ``interval_s``, or sooner when one is requested."""
        while True:
            self._wake.wait(self._interval_s)
            if self._halt.is_set():
                return
            self._wake.clear()
            try:
                self.full_passes()
            except Exception:
                logger.exception("review.periodic_pass_error")

    async def stop(self) -> None:
        """Halt the loop and wait until its thread has **actually** ended.

        A pass in flight stops at its next scope boundary; the scope it is in finishes as the one
        short locked unit it is. There is no timeout after which this returns while that thread
        could still write: the caller disposes the database and releases the owner lease only
        after this returns (ADR-0006).
        """
        self._halt.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join)
            self._thread = None
