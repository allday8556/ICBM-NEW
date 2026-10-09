"""What a task gathers in the job before its call, and the filter it applies after (ADR-0028 §4).

The enrichment owner (``app/stages/products/enrichment.py``) runs these; a task that has them
gathers in the ``enrich.tasks`` job only, never on a request or a read, because gathering may read
a provider.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class TaskContext:
    """The runtime data the composition carries, the part of the inputs it adds to the
    fingerprint, and the facts it was gathered from."""

    runtime: Mapping[str, Any]
    inputs: Mapping[str, Any]
    facts: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


@dataclass(frozen=True)
class FinishResult:
    """One result object after the task's deterministic filter, or why none is usable."""

    part: Mapping[str, Any] | None
    error_code: str | None


class TaskContextSource(Protocol):
    def gather(self, facts: Sequence[Mapping[str, Any]], target: Any) -> TaskContext: ...

    def finish(self, part: Mapping[str, Any], context: TaskContext) -> FinishResult: ...
