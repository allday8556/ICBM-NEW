"""A job that can no longer run tells its owner, exactly once (Issue #52 ruling 5720586391 G1).

The hook is generic job lifecycle: the runner calls it for every job type that declares one, when
that job reaches a state it will never leave — succeeded, dead-lettered, or dead after an
interrupted attempt — and never while a retry is still scheduled. It runs after the job's own state
is committed, so an owner that fails cannot undo the job's bookkeeping.
"""

from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from app.config import AppConfig
from app.container import Container, build_container
from app.core.errors import AppError, ErrorClass, InputValidationError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.jobs.models import JobState
from app.jobs.policy import RetryPolicy
from app.jobs.registry import JobContext, JobDefinition, JobHandler, TerminalJob
from tests.support import TEST_JOBS, FakeClock

pytestmark = pytest.mark.integration

ONCE = RetryPolicy(max_attempts=1, base_delay_s=0.01, max_delay_s=0.01)
TWICE = RetryPolicy(max_attempts=2, base_delay_s=0.01, max_delay_s=0.01)


class Owner:
    """An owner that records every job it is told about.

    ``waiting`` stands for the rows an owner of its own would have: a job it has durable work for
    that is not settled yet. The reconciliation sweep reads it, so the double has to keep it the
    way a real owner does — a settled job is no longer waiting.
    """

    def __init__(self) -> None:
        self.told: list[TerminalJob] = []
        self.raise_on_call = False
        self.waiting: list[str] = []
        self.asked_with: list[tuple[str, ...]] = []

    def __call__(self, terminal: TerminalJob) -> None:
        self.told.append(terminal)
        if self.raise_on_call:
            raise RuntimeError("the owner itself is broken")
        if terminal.job_id in self.waiting:
            self.waiting.remove(terminal.job_id)

    def unsettled(self, terminal_states: Sequence[str]) -> tuple[str, ...]:
        # The states that count as over are the job system's, handed in; this double keeps no
        # opinion of its own about them.
        self.asked_with.append(tuple(terminal_states))
        return tuple(self.waiting)


class Transient(AppError):
    error_class = ErrorClass.TRANSIENT


def jobs_for(owner: Owner) -> tuple[JobDefinition, ...]:
    def succeed(ctx: JobContext) -> None:
        return None

    def crash(ctx: JobContext) -> None:
        raise RuntimeError("unexpected bug")

    def reject(ctx: JobContext) -> None:
        raise InputValidationError("OWNED_BAD_INPUT", "never valid")

    def flaky(ctx: JobContext) -> None:
        raise Transient("OWNED_TRANSIENT", "try again")

    def owned(job_type: str, handler: JobHandler, described: str, **rest: Any) -> JobDefinition:
        # A terminal owner and where its unsettled work can be found are declared together.
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
        owned("owned.reject", reject, "invalid", retry_policy=ONCE),
        owned("owned.flaky", flaky, "retries once", retry_policy=TWICE),
        owned("owned.interrupted", succeed, "not idempotent", idempotent=False),
        owned(
            "owned.resumable",
            succeed,
            "idempotent, with an attempt left",
            idempotent=True,
            retry_policy=TWICE,
        ),
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


@pytest.mark.parametrize(
    ("job_type", "state", "error_class", "error_code"),
    [
        ("owned.succeed", JobState.SUCCEEDED, None, None),
        ("owned.crash", JobState.DEAD, "UNKNOWN", "UNHANDLED_EXCEPTION"),
        ("owned.reject", JobState.DEAD, "VALIDATION", "OWNED_BAD_INPUT"),
    ],
)
def test_a_terminal_job_tells_its_owner_once_with_the_job_systems_own_classification(
    owning: Container,
    owner: Owner,
    job_type: str,
    state: JobState,
    error_class: str | None,
    error_code: str | None,
) -> None:
    job = owning.jobs.enqueue(job_type)
    result = owning.runner.run_next()
    assert result is not None and result.state is state

    assert [told.job_id for told in owner.told] == [job.job_id]
    told = owner.told[0]
    assert (told.job_type, told.state) == (job_type, state.value)
    assert (told.error_class, told.error_code) == (error_class, error_code)
    assert told.attempt_no == 1 and told.correlation_id == job.correlation_id

    assert owning.runner.run_next() is None, "a terminal job never runs again"
    assert len(owner.told) == 1, "and its owner is told exactly once"


def test_an_owner_is_not_told_while_a_retry_is_still_coming(
    owning: Container, owner: Owner, clock: FakeClock
) -> None:
    owning.jobs.enqueue("owned.flaky")
    first = owning.runner.run_next()
    assert first is not None and first.state is JobState.RETRY_SCHEDULED
    assert owner.told == [], "the job can still run, so nothing of its owner's is over"

    clock.advance(1)
    second = owning.runner.run_next()
    assert second is not None and second.state is JobState.DEAD
    assert [told.state for told in owner.told] == ["DEAD"]
    assert owner.told[0].error_code == "OWNED_TRANSIENT", "the last attempt's own classification"


def test_an_interrupted_job_that_will_never_resume_tells_its_owner(
    owning: Container, owner: Owner
) -> None:
    job = owning.jobs.enqueue("owned.interrupted")
    claimed = owning.runner._claim()  # left RUNNING, as if the process died mid-attempt
    assert claimed is not None

    assert owning.runner.recover_interrupted() == 1
    assert owning.jobs.get(job.job_id).state == JobState.DEAD
    assert [(told.job_id, told.error_code) for told in owner.told] == [
        (job.job_id, "INTERRUPTED_OUTCOME_UNKNOWN")
    ]


def test_an_interrupted_job_that_will_resume_does_not_tell_its_owner(
    owning: Container, owner: Owner
) -> None:
    job = owning.jobs.enqueue("owned.resumable")
    assert owning.runner._claim() is not None
    assert owning.runner.recover_interrupted() == 1
    assert owning.jobs.get(job.job_id).state == JobState.RETRY_SCHEDULED
    assert owner.told == [], "it is rescheduled, so nothing is over yet"


def test_a_broken_owner_cannot_undo_the_jobs_own_bookkeeping(
    owning: Container, owner: Owner, caplog: pytest.LogCaptureFixture
) -> None:
    owner.raise_on_call = True
    job = owning.jobs.enqueue("owned.crash")
    result = owning.runner.run_next()

    assert result is not None and result.state is JobState.DEAD
    record = owning.jobs.get(job.job_id)
    assert record.state == JobState.DEAD and record.last_error_code == "UNHANDLED_EXCEPTION"
    assert [r.getMessage() for r in caplog.records if "terminal_owner_failed" in r.getMessage()]


def test_a_job_type_without_an_owner_is_unaffected(owning: Container, owner: Owner) -> None:
    owning.jobs.enqueue("test.crash")
    result = owning.runner.run_next()
    assert result is not None and result.state is JobState.DEAD
    assert owner.told == []
