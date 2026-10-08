"""One task's request, composed from the current prompt registry (ADR-0026 §3, §3.1, §4; AIF-2).

The layers are read from their stores at their current revisions, in the prototype's order: the
global rules, the task's role, the platform policy, the task, its input variables and output
schema, then the runtime data. A platform policy carries the limits a canonical owner holds for
that marketplace, read from that owner at composition time and never copied into the policy
(ADR-0026 §3: referenced, never copied). The composition records the revision of every layer it
read (``prompt_version``) and of the policy and every limit owner it referenced
(``policy_version``): together they are what a result's fingerprint names (ADR-0012 §6).
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from app.capabilities.ai.prompts import PromptRegistryStore, compose
from app.capabilities.ai.registry import BY_KEY, COMMON_POLICY_KEY, GLOBAL_KEY, Layer
from app.platform.core.errors import InputValidationError

AI_TASK_UNKNOWN: Final = "AI_TASK_UNKNOWN"
AI_TASK_HAS_NO_ROLE: Final = "AI_TASK_HAS_NO_ROLE"
AI_POLICY_UNKNOWN: Final = "AI_POLICY_UNKNOWN"


@dataclass(frozen=True)
class PlatformLimits:
    """The limits one canonical owner holds for one marketplace context, with the revision they
    were read at."""

    owner: str
    owner_revision: str
    values: Mapping[str, Any]


class PlatformLimitSource(Protocol):
    def limits(self, context: Mapping[str, Any]) -> PlatformLimits | None:
        """The owner's limits for this context, or ``None`` when it holds none for it."""
        ...


@dataclass(frozen=True)
class ComposedRequest:
    task_key: str
    role_key: str
    policy_key: str
    mode: str
    text: str
    # Layer key → the revision it was read at (global rules, role, task).
    prompt_version: Mapping[str, str]
    # The policy's revision, and ``limits:<owner>`` → each referenced owner's revision.
    policy_version: Mapping[str, str]
    limits: tuple[PlatformLimits, ...] = field(default=())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


class PromptComposer:
    """Composes a task request from the stores. It never calls a provider."""

    def __init__(
        self,
        store: PromptRegistryStore,
        limit_sources: Mapping[str, tuple[PlatformLimitSource, ...]] | None = None,
    ) -> None:
        self._store = store
        # Policy key → the owners whose limits that policy references. A marketplace's limits are
        # wired with the stage that first needs them; none is wired before.
        self._limit_sources = dict(limit_sources or {})

    def compose(
        self,
        task_key: str,
        runtime_data: Mapping[str, Any],
        *,
        policy_key: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> ComposedRequest:
        task_entry = BY_KEY.get(task_key)
        if task_entry is None or task_entry.layer is not Layer.TASK:
            raise InputValidationError(AI_TASK_UNKNOWN, f"{task_key} is not a registry task")
        if task_entry.role_key is None:
            raise InputValidationError(
                AI_TASK_HAS_NO_ROLE,
                f"{task_key} has no role in the registry, so it cannot be composed",
            )
        policy = policy_key or COMMON_POLICY_KEY
        policy_entry = BY_KEY.get(policy)
        if policy_entry is None or policy_entry.layer is not Layer.POLICY:
            raise InputValidationError(AI_POLICY_UNKNOWN, f"{policy} is not a platform policy")
        role_key = task_entry.role_key
        records = {key: self._store.entry(key) for key in (GLOBAL_KEY, role_key, policy, task_key)}
        limits = tuple(
            found
            for source in self._limit_sources.get(policy, ())
            if (found := source.limits(context or {})) is not None
        )
        policy_text = records[policy].current.content["prompt"]
        if limits:
            policy_text += "\n\nPLATFORM_LIMITS\n" + _json(
                {limit.owner: dict(limit.values) for limit in limits}
            )
        task = records[task_key].current.content
        return ComposedRequest(
            task_key=task_key,
            role_key=role_key,
            policy_key=policy,
            mode=task["mode"],
            text=compose(
                records[GLOBAL_KEY].current.content["prompt"],
                records[role_key].current.content["prompt"],
                policy_text,
                task,
                runtime_data=_json(dict(runtime_data)),
            ),
            prompt_version={
                key: records[key].current.revision_id for key in (GLOBAL_KEY, role_key, task_key)
            },
            policy_version={
                policy: records[policy].current.revision_id,
                **{f"limits:{limit.owner}": limit.owner_revision for limit in limits},
            },
            limits=limits,
        )
