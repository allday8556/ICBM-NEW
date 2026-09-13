from dataclasses import dataclass
from datetime import timedelta

from app.core.errors import AUTO_RETRYABLE, ErrorClass


@dataclass(frozen=True)
class RetryPolicy:
    """Deterministic exponential backoff with a hard attempt cap.

    Only TRANSIENT and RATE_LIMITED failures are retried (ADR-0004); every other class goes to
    dead-letter on its first failure. No jitter in M0, so schedules are exactly verifiable.
    """

    max_attempts: int
    base_delay_s: float
    factor: float = 2.0
    max_delay_s: float = 300.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_s <= 0 or self.factor < 1 or self.max_delay_s < self.base_delay_s:
            raise ValueError("invalid backoff parameters")

    def delay_after(self, attempt_no: int) -> timedelta:
        """Delay between failed attempt ``attempt_no`` (1-based) and the next attempt."""
        seconds = min(self.base_delay_s * self.factor ** (attempt_no - 1), self.max_delay_s)
        return timedelta(seconds=seconds)

    def allows_retry(self, error_class: ErrorClass, attempt_no: int) -> bool:
        return error_class in AUTO_RETRYABLE and attempt_no < self.max_attempts

    def schedule_s(self) -> list[float]:
        """Planned delays between consecutive attempts when every attempt fails retryably."""
        return [self.delay_after(n).total_seconds() for n in range(1, self.max_attempts)]
