import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.core.errors import NotFoundError
from app.jobs.policy import RetryPolicy

_JOB_TYPE = re.compile(r"^[a-z0-9]+(\.[a-z0-9_]+)+$")


@dataclass(frozen=True)
class JobContext:
    job_id: str
    job_type: str
    attempt_no: int
    max_attempts: int
    correlation_id: str
    target_ref: str | None
    payload: Mapping[str, Any]


JobHandler = Callable[[JobContext], None]


@dataclass(frozen=True)
class JobDefinition:
    job_type: str
    handler: JobHandler
    description: str
    # True only when re-running after an interrupted attempt cannot duplicate an external
    # effect. Interrupted non-idempotent jobs go to dead-letter as UNKNOWN (ARCHITECTURE §8).
    idempotent: bool = False
    retry_policy: RetryPolicy | None = None


class JobRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, JobDefinition] = {}

    def register(self, definition: JobDefinition) -> None:
        if not _JOB_TYPE.fullmatch(definition.job_type):
            raise ValueError(f"invalid job type name: {definition.job_type!r}")
        if definition.job_type in self._definitions:
            raise ValueError(f"job type already registered: {definition.job_type}")
        self._definitions[definition.job_type] = definition

    def get(self, job_type: str) -> JobDefinition:
        try:
            return self._definitions[job_type]
        except KeyError:
            raise NotFoundError(
                "JOB_TYPE_UNKNOWN", f"no handler registered for {job_type}"
            ) from None

    def find(self, job_type: str) -> JobDefinition | None:
        return self._definitions.get(job_type)

    def job_types(self) -> list[str]:
        return sorted(self._definitions)
