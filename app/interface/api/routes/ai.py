"""The AI Prompt Registry routes (ADR-0026 §3, §8; AIF-1).

Settings reads the v29 registry with each entry's current text and revision history, saves one
field of one entry against the revision it was read from, resets one field to the v29 seed, and
previews a composed request. Nothing here calls an AI provider: no provider exists (ADR-0026 §1).
"""

from fastapi import APIRouter, Query

from app.capabilities.ai.prompts import (
    EntryView,
    PreviewView,
    RegistryView,
    ResetRequest,
    ReviseRequest,
)
from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import get_correlation_id, new_correlation_id

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


def _cid() -> str:
    return get_correlation_id() or new_correlation_id()


@router.get("/registry")
def registry(container: ContainerDep) -> RegistryView:
    return container.prompt_registry.registry()


@router.post("/prompts/{key}/revisions")
def revise_prompt(container: ContainerDep, key: str, request: ReviseRequest) -> EntryView:
    """Append one revision changing one field of a GLOBAL, ROLE or TASK entry, or change nothing."""
    return container.prompt_registry.revise(key, "template", request, cid=_cid())


@router.post("/prompts/{key}/reset")
def reset_prompt(container: ContainerDep, key: str, request: ResetRequest) -> EntryView:
    """Append one revision restoring one field to its v29 seed text, or change nothing."""
    return container.prompt_registry.reset(key, "template", request, cid=_cid())


@router.post("/policies/{key}/revisions")
def revise_policy(container: ContainerDep, key: str, request: ReviseRequest) -> EntryView:
    return container.prompt_registry.revise(key, "policy", request, cid=_cid())


@router.post("/policies/{key}/reset")
def reset_policy(container: ContainerDep, key: str, request: ResetRequest) -> EntryView:
    return container.prompt_registry.reset(key, "policy", request, cid=_cid())


@router.get("/preview")
def preview(
    container: ContainerDep,
    role: str = Query(max_length=64),
    policy: str = Query(max_length=64),
    task: str = Query(max_length=64),
) -> PreviewView:
    """The request a task would compose from the current revisions, with no runtime data."""
    return container.prompt_registry.preview(role, policy, task)
