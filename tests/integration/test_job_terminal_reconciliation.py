"""A missed settlement is found again from the database (Issue #52 ruling 5721367502).

The terminal hook of PR #76 runs after the job's own state is committed, so it is prompt and
nothing more: a hook that raises, and a process that stops between the commit and the call, both
leave a job that is over beside an owner that is still waiting. Correctness therefore belongs to a
sweep over durable state, not to that call having worked.

What these tests hold to:

* S1 — the inconsistency is discoverable from committed rows alone: a job the job table says is
  terminal, named by an owner that says it is still waiting on it;
* S2 — the sweep is repeat-safe: an owner that has settled is not asked again, and one told twice
  settles nothing new;
* S3 — the worker runs it at its recovery boundary, and a successful sweep leaves none behind;
* S4 — a crash before the hook, a hook that raised, and a job settled promptly all converge;
* G5 — what the sweep hands the owner is rebuilt from the job's own committed rows;
* G7 — a settlement that fails again is logged and clears nothing.

The owner here is a double whose ``waiting`` list stands for its own durable rows; COLLECT's real
rows are exercised in ``test_collect_run_lifecycle.py``. Nothing in this module reaches a provider.
"""

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, cast

import pytest

from app.config import AppConfig
from app.container import Container, build_container
from app.core.errors import AppError, ErrorClass
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.jobs.models import TERMINAL_STATE_NAMES, JobState
from app.jobs.policy import RetryPolicy
from app.jobs.registry import JobContext, JobDefinition, JobHandler, JobRegistry, TerminalJob
from app.jobs.runner import JobRunner
from app.jobs.worker import JobWorker
from tests.support import TEST_JOBS, FakeClock

pytestmark = pytest.mark.integration

ONCE = RetryPolicy(max_attempts=1, base_delay_s=0.01, max_delay_s=0.01)
TWICE = RetryPolicy(max_attempts=2, base_delay_s=0.01, max_delay_s=0.01)
TARGET = "target:of-the-owned-job"


class Transient(AppError):
    error_class = ErrorClass.TRANSIENT


class Owner:
    """An owner of durable work, whose ``waiting`` list stands for its own unsettled rows."""

    def __init__(self) -> None:
        self.told: list[TerminalJob] = []
        self.waiting: list[str] = []
        self.asked_with: list[tuple[str, ...]] = []
        self.broken = False

    def __call__(self, terminal: TerminalJob) -> None:
        self.told.append(terminal)
        if self.broken:
            raise RuntimeError("settling the owner's own state failed")
        if terminal.job_id in self.waiting:
            self.waiting.remove(terminal.job_id)

    def unsettled(self, terminal_states: Sequence[str]) -> tuple[str, ...]:
        # Which states count as over is handed in by the job layer; the owner keeps no such
        # definition of its own, and records what it was asked with.
        self.asked_with.append(tuple(terminal_states))
        return tuple(self.waiting)


def jobs_for(owner: Owner) -> tuple[JobDefinition, ...]:
    def succeed(ctx: JobContext) -> None:
        return None

    def crash(ctx: JobContext) -> None:
        raise RuntimeError("unexpected bug")

    def flaky_then_ok(ctx: JobContext) -> None:
        if ctx.attempt_no == 1:
            raise Transient("OWNED_TRANSIENT", "try again")

    def owned(job_type: str, handler: JobHandler, described: str, **rest: Any) -> JobDefinition:
        return JobDefinition(
            job_type,
            handler,
            described,
            on_terminal=owner,
            unsettled_owned_jobs=owner.unsettled,
            **rest,
        )

    return (
        owned("owned.succeed", succeed, "succeeds", retry_policy=ONCE),
        owned("owned.crash", crash, "crashes", retry_policy=ONCE),
        owned("owned.recovers", flaky_then_ok, "fails once, then succeeds", retry_policy=TWICE),
    )


@pytest.fixture
def owner() -> Owner:
    return Owner()


@pytest.fixture
def owning(config: AppConfig, clock: FakeClock, owner: Owner) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=MemorySecretStore(),
            extra_jobs=(*TEST_JOBS, *jobs_for(owner)),
        )
        try:
            yield built
        finally:
            built.db.dispose()


@contextmanager
def nothing_told(container: Container, job_type: str) -> Iterator[None]:
    """A process that ends a job and stops before its owner hears anything about it."""
    registry = container.job_registry
    original = registry.get(job_type)
    registry._definitions[job_type] = replace(original, on_terminal=None)
    try:
        yield
    finally:
        registry._definitions[job_type] = original


def submit(container: Container, owner: Owner, job_type: str) -> str:
    """Enqueue an owned job and record that the owner now has work waiting on it."""
    job = container.jobs.enqueue(job_type, target_ref=TARGET)
    owner.waiting.append(job.job_id)
    return job.job_id


# ---------------------------------------------------------------- discovery and convergence


def test_a_job_that_ended_without_telling_its_owner_is_settled_by_the_next_sweep(
    owning: Container, owner: Owner
) -> None:
    job_id = submit(owning, owner, "owned.crash")
    with nothing_told(owning, "owned.crash"):
        assert owning.runner.run_next() is not None

    # S1: neither side of this is in memory — the job table says the job is over, and the owner's
    # own rows say it is still waiting on it.
    assert owning.jobs.get(job_id).state == JobState.DEAD
    assert owner.told == [] and owner.waiting == [job_id]

    assert owning.runner.reconcile_terminal_owners() == 1
    told = owner.told[0]
    assert (told.job_id, told.state, told.error_class, told.error_code) == (
        job_id,
        "DEAD",
        "UNKNOWN",
        "UNHANDLED_EXCEPTION",
    )
    assert owner.waiting == []
    # S3: after a successful sweep there is no terminal job whose owner is still waiting.
    assert owning.runner.reconcile_terminal_owners() == 0
    assert len(owner.told) == 1, "and nothing is told twice for the sake of it"


def test_the_sweep_hands_the_owner_what_the_prompt_call_would_have(
    owning: Container, owner: Owner
) -> None:
    # G5: the same job type failing the same way, once settled promptly and once by the sweep.
    submit(owning, owner, "owned.crash")
    assert owning.runner.run_next() is not None
    swept_id = submit(owning, owner, "owned.crash")
    with nothing_told(owning, "owned.crash"):
        assert owning.runner.run_next() is not None
    assert owning.runner.reconcile_terminal_owners() == 1

    prompt, swept = owner.told
    assert (swept.job_type, swept.state, swept.attempt_no, swept.target_ref) == (
        prompt.job_type,
        prompt.state,
        prompt.attempt_no,
        prompt.target_ref,
    )
    assert (swept.error_class, swept.error_code) == (prompt.error_class, prompt.error_code)
    assert swept.job_id == swept_id and swept.target_ref == TARGET
    assert swept.correlation_id == owning.jobs.get(swept_id).correlation_id
    assert swept.correlation_id != prompt.correlation_id, "each job keeps its own"


def test_a_job_that_failed_before_it_succeeded_is_not_swept_as_a_failure(
    owning: Container, owner: Owner, clock: FakeClock
) -> None:
    job_id = submit(owning, owner, "owned.recovers")
    with nothing_told(owning, "owned.recovers"):
        assert owning.runner.run_next() is not None  # the transient failure
        clock.advance(1)
        assert owning.runner.run_next() is not None  # and then the attempt that succeeded

    record = owning.jobs.get(job_id)
    assert record.state == JobState.SUCCEEDED
    assert record.last_error_code == "OWNED_TRANSIENT", "the job keeps its most recent failure"

    assert owning.runner.reconcile_terminal_owners() == 1
    told = owner.told[0]
    # G5: the attempt that ended the job is what the sweep reports, not a failure it survived.
    assert (told.state, told.error_class, told.error_code) == ("SUCCEEDED", None, None)


def test_a_job_that_can_still_run_is_never_reconciled(
    owning: Container, owner: Owner, clock: FakeClock
) -> None:
    job_id = submit(owning, owner, "owned.recovers")
    with nothing_told(owning, "owned.recovers"):
        first = owning.runner.run_next()
        assert first is not None and first.state is JobState.RETRY_SCHEDULED

        assert owning.runner.reconcile_terminal_owners() == 0, "it is not over, so nothing is"
        assert owner.told == [] and owner.waiting == [job_id]

    # And the retry then settles it the ordinary way, so the wait above was legitimate.
    clock.advance(1)
    assert owning.runner.run_next() is not None
    assert [told.job_id for told in owner.told] == [job_id]
    assert owner.waiting == []


def test_an_owner_that_settled_its_work_is_not_asked_about_it_again(
    owning: Container, owner: Owner
) -> None:
    submit(owning, owner, "owned.succeed")
    assert owning.runner.run_next() is not None
    assert len(owner.told) == 1 and owner.waiting == []

    assert owning.runner.reconcile_terminal_owners() == 0
    assert len(owner.told) == 1


def test_a_settlement_that_fails_leaves_the_same_inconsistency_for_the_next_sweep(
    owning: Container, owner: Owner, caplog: pytest.LogCaptureFixture
) -> None:
    owner.broken = True
    job_id = submit(owning, owner, "owned.crash")
    assert owning.runner.run_next() is not None  # the prompt call raises
    assert owner.waiting == [job_id]

    # G7: a sweep that fails again reports what it found, logs it, and marks nothing reconciled.
    assert owning.runner.reconcile_terminal_owners() == 1
    assert owner.waiting == [job_id]
    assert [r for r in caplog.records if "terminal_owner_failed" in r.getMessage()]
    assert [r for r in caplog.records if "owner_left_unsettled" in r.getMessage()]

    owner.broken = False
    assert owning.runner.reconcile_terminal_owners() == 1
    assert owner.waiting == []
    assert owning.runner.reconcile_terminal_owners() == 0


def test_the_job_layer_tells_the_owner_which_states_count_as_over(
    owning: Container, owner: Owner
) -> None:
    # An owner is handed the predicate; it never decides what "over" means (ruling 5721796080 §3).
    submit(owning, owner, "owned.succeed")
    assert owning.runner.run_next() is not None
    assert owning.runner.reconcile_terminal_owners() == 0

    assert owner.asked_with and set(owner.asked_with) == {TERMINAL_STATE_NAMES}
    assert TERMINAL_STATE_NAMES == ("SUCCEEDED", "DEAD")


def test_an_owner_is_only_offered_the_jobs_it_owns(owning: Container, owner: Owner) -> None:
    unowned = owning.jobs.enqueue("test.crash")
    assert owning.runner.run_next() is not None
    owner.waiting.append(unowned.job_id)  # a job id of a type this owner does not own

    assert owning.runner.reconcile_terminal_owners() == 0
    assert owner.told == []


def test_a_job_type_without_an_owner_is_never_swept(owning: Container, owner: Owner) -> None:
    owning.jobs.enqueue("test.crash")
    assert owning.runner.run_next() is not None
    assert owning.runner.reconcile_terminal_owners() == 0


def test_an_owner_and_where_to_find_its_unsettled_work_are_declared_together(
    owner: Owner,
) -> None:
    def handler(ctx: JobContext) -> None:
        return None

    registry = JobRegistry()
    with pytest.raises(ValueError, match="together"):
        registry.register(JobDefinition("half.told", handler, "hook only", on_terminal=owner))
    with pytest.raises(ValueError, match="together"):
        registry.register(
            JobDefinition("half.found", handler, "rows only", unsettled_owned_jobs=owner.unsettled)
        )
    registry.register(
        JobDefinition(
            "whole.owned",
            handler,
            "both",
            on_terminal=owner,
            unsettled_owned_jobs=owner.unsettled,
        )
    )
    assert registry.job_types() == ["whole.owned"]


# ---------------------------------------------------------------- when the sweep runs


def test_the_worker_settles_what_a_previous_process_left_at_its_recovery_boundary(
    owning: Container, owner: Owner
) -> None:
    job_id = submit(owning, owner, "owned.crash")
    with nothing_told(owning, "owned.crash"):
        assert owning.runner.run_next() is not None
    assert owner.told == [], "the process ended the job and told no one"

    async def start_then_stop() -> None:
        await owning.worker.start()  # S3: the sweep is part of starting, before anything is served
        await owning.worker.stop()

    asyncio.run(start_then_stop())

    assert [told.job_id for told in owner.told] == [job_id]
    assert owner.waiting == []


def test_an_idle_worker_reaches_another_sweep_on_the_interval(clock: FakeClock) -> None:
    class Idle:
        """A runner with nothing to run, counting the sweeps the worker asks for."""

        def __init__(self) -> None:
            self.sweeps = 0

        def recover_interrupted(self) -> int:
            return 0

        def run_next(self) -> None:
            return None

        def seconds_until_next_due(self) -> float | None:
            return None

        def reconcile_terminal_owners(self) -> int:
            self.sweeps += 1
            return 0

    idle = Idle()
    worker = JobWorker(
        cast(JobRunner, idle), poll_interval_s=0.01, clock=clock, reconcile_interval_s=30.0
    )

    async def drive() -> tuple[int, int, int]:
        await worker.start()
        at_start = idle.sweeps
        await asyncio.sleep(0.1)  # many idle passes of a 0.01 s loop
        idling = idle.sweeps
        clock.advance(31)
        for _ in range(200):
            if idle.sweeps > idling:
                break
            await asyncio.sleep(0.01)
        after = idle.sweeps
        await worker.stop()
        return at_start, idling, after

    at_start, idling, after = asyncio.run(drive())
    assert at_start == 1, "starting is a recovery boundary, so it sweeps once"
    assert idling == at_start, "and an idle loop does not sweep on every pass"
    assert after > idling, "but it reaches another when the interval is up"
