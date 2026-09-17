import re
from collections.abc import Callable, Mapping, Sequence
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
class TerminalJob:
    """A job that has reached a state it will never leave, and why.

    ``error_class`` and ``error_code`` are the job system's own classification of the last
    attempt, not a domain's: an unexpected exception is ``UNKNOWN`` / ``UNHANDLED_EXCEPTION``
    here, and an owner must not translate that into a meaning of its own.
    """

    job_id: str
    job_type: str
    state: str  # SUCCEEDED or DEAD
    attempt_no: int
    correlation_id: str
    target_ref: str | None
    error_class: str | None
    error_code: str | None


# What an owner does when its job can no longer change: settle whatever durable state that job
# was the only thing working on. It is called once the job's terminal state is committed, so an
# unexpected failure anywhere inside the handler cannot leave that state waiting forever. It runs
# outside the job's own transaction and may never raise into the worker.
#
# Being called is prompt, not authoritative. The call happens after the commit, so a hook that
# raises — or a process that stops between the two — leaves the job over and its owner waiting.
# A hook must therefore be safe to call again, and must settle nothing that is already settled.
TerminalHook = Callable[[TerminalJob], None]

# Which jobs the owner's own durable rows are still waiting on, read from the database and from
# nothing else. This is what makes a settlement that never happened discoverable afterwards: a job
# the job table says is over, named by an owner that says it is still waiting, is an inconsistency
# — whether a hook raised, or no hook was ever called. An owner with nothing open says so with an
# empty sequence, which is the normal answer.
UnsettledOwnedJobs = Callable[[], Sequence[str]]


@dataclass(frozen=True)
class JobDefinition:
    job_type: str
    handler: JobHandler
    description: str
    # True only when re-running after an interrupted attempt cannot duplicate an external
    # effect. Interrupted non-idempotent jobs go to dead-letter as UNKNOWN (ARCHITECTURE §8).
    idempotent: bool = False
    retry_policy: RetryPolicy | None = None
    on_terminal: TerminalHook | None = None
    unsettled_owned_jobs: UnsettledOwnedJobs | None = None


class JobRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, JobDefinition] = {}

    def register(self, definition: JobDefinition) -> None:
        if not _JOB_TYPE.fullmatch(definition.job_type):
            raise ValueError(f"invalid job type name: {definition.job_type!r}")
        if definition.job_type in self._definitions:
            raise ValueError(f"job type already registered: {definition.job_type}")
        if (definition.on_terminal is None) != (definition.unsettled_owned_jobs is None):
            # An owner whose settlement can only be attempted once is the hole this contract
            # exists to close: declaring the hook means also saying where the work it did not do
            # can be found again.
            raise ValueError(
                "a job type declares a terminal owner and what that owner is still waiting on "
                f"together, or neither: {definition.job_type}"
            )
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
