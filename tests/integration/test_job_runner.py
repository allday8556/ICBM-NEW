"""JobRunner semantics under a fake clock: exact, deterministic schedules."""

import logging
from datetime import timedelta

import pytest

from app.audit.models import AuditEventType
from app.container import Container
from app.core.correlation import correlation_scope
from app.jobs.diagnostic import FAILING_JOB_TYPE
from app.jobs.models import JobState
from tests.support import FakeClock

pytestmark = pytest.mark.integration


def test_failing_job_retries_on_schedule_then_dead_letters(
    container: Container, clock: FakeClock
) -> None:
    with correlation_scope("trace-runner-0001"):
        job = container.jobs.enqueue(FAILING_JOB_TYPE)
    assert job.state == JobState.QUEUED
    assert job.correlation_id == "trace-runner-0001"
    assert job.max_attempts == 3

    first = container.runner.run_next()
    assert first is not None
    assert first.state is JobState.RETRY_SCHEDULED
    assert first.next_attempt_at == clock.now() + timedelta(seconds=0.05)

    assert container.runner.run_next() is None, "retry must not run before it is due"
    clock.advance(0.05)
    second = container.runner.run_next()
    assert second is not None and second.state is JobState.RETRY_SCHEDULED
    assert second.next_attempt_at == clock.now() + timedelta(seconds=0.1)

    clock.advance(0.1)
    third = container.runner.run_next()
    assert third is not None and third.state is JobState.DEAD
    assert container.runner.run_next() is None

    record = container.jobs.get(job.job_id)
    assert record.state == JobState.DEAD
    assert record.attempt_count == 3
    assert record.last_error_class == "TRANSIENT"
    assert record.last_error_code == "M0_DIAGNOSTIC_FORCED_FAILURE"
    assert [a.outcome for a in record.attempts] == ["FAILED", "FAILED", "FAILED"]
    gaps = [
        (later.scheduled_for - earlier.finished_at).total_seconds()
        for earlier, later in zip(record.attempts, record.attempts[1:], strict=False)
        if earlier.finished_at is not None
    ]
    assert gaps == [0.05, 0.1]
    assert all(a.started_at == a.scheduled_for for a in record.attempts)

    events = container.audit.list_events(correlation_id="trace-runner-0001")
    assert [e.event_type for e in events] == [AuditEventType.JOB_DEAD_LETTERED]
    assert events[0].target_ref == f"job:{job.job_id}"
    assert events[0].details["dead_letter_reason"] == "RETRY_CAP_REACHED"


def test_successful_job(container: Container) -> None:
    job = container.jobs.enqueue("test.succeed")
    result = container.runner.run_next()
    assert result is not None and result.state is JobState.SUCCEEDED
    record = container.jobs.get(job.job_id)
    assert record.state == JobState.SUCCEEDED
    assert record.finished_at is not None
    assert container.audit.list_events() == []


@pytest.mark.parametrize(
    ("job_type", "error_class"),
    [("test.reject", "VALIDATION"), ("test.crash", "UNKNOWN")],
)
def test_non_retryable_failures_dead_letter_on_first_attempt(
    container: Container, job_type: str, error_class: str
) -> None:
    job = container.jobs.enqueue(job_type)
    result = container.runner.run_next()
    assert result is not None and result.state is JobState.DEAD
    record = container.jobs.get(job.job_id)
    assert record.attempt_count == 1
    assert record.last_error_class == error_class
    event = container.audit.list_events(correlation_id=record.correlation_id)[0]
    assert event.details["dead_letter_reason"] == "NON_RETRYABLE_ERROR_CLASS"


def test_attempt_logs_carry_the_job_correlation_id(
    container: Container, caplog: pytest.LogCaptureFixture
) -> None:
    with correlation_scope("trace-runner-logs"):
        container.jobs.enqueue(FAILING_JOB_TYPE)
    caplog.set_level(logging.INFO)
    container.runner.run_next()
    attempt_logs = [r for r in caplog.records if r.getMessage() == "job.attempt.started"]
    assert attempt_logs
    assert attempt_logs[0].correlation_id == "trace-runner-logs"  # type: ignore[attr-defined]


def _interrupt(container: Container, job_type: str) -> str:
    job = container.jobs.enqueue(job_type)
    claimed = container.runner._claim()
    assert claimed is not None and claimed.job_id == job.job_id
    return job.job_id  # left RUNNING, as if the process died mid-attempt


def test_interrupted_idempotent_job_is_rescheduled(container: Container) -> None:
    job_id = _interrupt(container, "test.succeed")
    assert container.runner.recover_interrupted() == 1
    record = container.jobs.get(job_id)
    assert record.state == JobState.RETRY_SCHEDULED
    assert record.attempts[0].outcome == "INTERRUPTED"
    result = container.runner.run_next()
    assert result is not None and result.state is JobState.SUCCEEDED
    assert container.jobs.get(job_id).attempt_count == 2


def test_interrupted_non_idempotent_job_is_dead_lettered_as_unknown(container: Container) -> None:
    job_id = _interrupt(container, "test.write_once")
    container.runner.recover_interrupted()
    record = container.jobs.get(job_id)
    assert record.state == JobState.DEAD
    assert record.last_error_class == "UNKNOWN"
    assert record.last_error_code == "INTERRUPTED_OUTCOME_UNKNOWN"
