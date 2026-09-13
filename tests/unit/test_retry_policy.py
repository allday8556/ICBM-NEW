import pytest

from app.core.errors import ErrorClass
from app.jobs.policy import RetryPolicy


def test_exponential_schedule_with_cap() -> None:
    policy = RetryPolicy(max_attempts=6, base_delay_s=1, factor=2, max_delay_s=5)
    assert policy.schedule_s() == [1, 2, 4, 5, 5]


def test_retry_stops_at_the_attempt_cap() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_s=1)
    assert policy.allows_retry(ErrorClass.TRANSIENT, 1)
    assert policy.allows_retry(ErrorClass.TRANSIENT, 2)
    assert not policy.allows_retry(ErrorClass.TRANSIENT, 3)


@pytest.mark.parametrize(
    "error_class",
    [
        ErrorClass.UNKNOWN,
        ErrorClass.VALIDATION,
        ErrorClass.POLICY_BLOCKED,
        ErrorClass.AUTH,
        ErrorClass.NOT_FOUND,
    ],
)
def test_non_retryable_classes_never_retry(error_class: ErrorClass) -> None:
    assert not RetryPolicy(max_attempts=10, base_delay_s=1).allows_retry(error_class, 1)


def test_rate_limited_is_retried() -> None:
    assert RetryPolicy(max_attempts=2, base_delay_s=1).allows_retry(ErrorClass.RATE_LIMITED, 1)


def test_invalid_parameters_are_rejected() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0, base_delay_s=1)
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=2, base_delay_s=2, max_delay_s=1)
