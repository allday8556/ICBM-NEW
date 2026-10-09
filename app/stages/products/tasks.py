"""The runnable enrichment tasks, each landing with its own stage (ADR-0026 §5).

**The product-name stage (ADR-0027 §6; AIS-2).** The v29 bundle task with its single result key
``product_name``. Its other keys (tags, category, options) belong to later stages and are never
recorded. The task reads exactly the facts a name depends on, so a change of any other fact leaves
a recommendation fresh (dependency-scoped staleness).
"""

from typing import Final

from app.capabilities.ai.task_context import TaskContextSource
from app.stages.products.enrichment import ResultSchema, TaskDefinition

PRODUCT_NAME_TASK: Final = TaskDefinition(
    task_key="TASK_PRODUCT_RECOMMEND_BUNDLE_V1",
    results={"product_name": ResultSchema(("recommended",))},
    fact_fields=(
        "original_name",
        "brand",
        "manufacturer",
        "origin",
        "options",
        "detail_description",
    ),
    schema_version="name-stage-1",
)

PRODUCTION_TASKS: Final = (PRODUCT_NAME_TASK,)


def platform_tag_task(
    *,
    task_key: str,
    prompt_key: str,
    marketplace: str,
    result_key: str,
    fact_fields: tuple[str, ...],
    schema_version: str,
    context: TaskContextSource,
) -> TaskDefinition:
    """ADR-0028 §4: a marketplace's tag task. It composes the bundle under its own enrichment id,
    needs a target of that marketplace, and gathers the platform's candidates in the job; the
    marketplace and its reads are the composition root's to name, never this owner's."""
    return TaskDefinition(
        task_key=task_key,
        results={result_key: ResultSchema(("recommended",))},
        fact_fields=fact_fields,
        schema_version=schema_version,
        prompt_key=prompt_key,
        marketplace=marketplace,
        context=context,
    )
