from datetime import UTC, datetime, timedelta

from app.core.errors import AppError, ErrorClass, InputValidationError
from app.jobs.registry import JobContext, JobDefinition


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 13, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def _succeed(ctx: JobContext) -> None:
    return None


def _reject(ctx: JobContext) -> None:
    raise InputValidationError("TEST_BAD_INPUT", "input can never be valid")


def _crash(ctx: JobContext) -> None:
    raise RuntimeError("unexpected bug")


SUCCEEDING_JOB = JobDefinition("test.succeed", _succeed, "always succeeds", idempotent=True)
VALIDATION_JOB = JobDefinition("test.reject", _reject, "raises VALIDATION")
CRASHING_JOB = JobDefinition("test.crash", _crash, "raises a non-contract exception")
NON_IDEMPOTENT_JOB = JobDefinition("test.write_once", _succeed, "external write", idempotent=False)


def _always_failing(error_class: ErrorClass) -> JobDefinition:
    """A job whose every attempt fails with ``error_class``. The ADR-0008 classes have no
    exception subclass in the application yet, so the test builds one."""
    failure = type(f"{error_class.title()}Error", (AppError,), {"error_class": error_class})

    def run(ctx: JobContext) -> None:
        raise failure(f"TEST_{error_class}", f"always fails with {error_class}")

    return JobDefinition(f"test.fail.{error_class.lower()}", run, f"raises {error_class}")


# ADR-0008 Stage 1: one job per class that the aligned taxonomy added.
ADR_0008_JOBS = tuple(
    _always_failing(c)
    for c in (
        ErrorClass.CONFLICT,
        ErrorClass.DUPLICATE,
        ErrorClass.REVIEW_REQUIRED,
        ErrorClass.FATAL,
    )
)

TEST_JOBS = (SUCCEEDING_JOB, VALIDATION_JOB, CRASHING_JOB, NON_IDEMPOTENT_JOB, *ADR_0008_JOBS)
