"""The runnable enrichment tasks, each landing with its own stage (ADR-0026 §5).

**The product-name stage (ADR-0027 §6; AIS-2).** The v29 bundle task with its single result key
``product_name``. Its other keys (tags, category, options) belong to later stages and are never
recorded. The task reads exactly the facts a name depends on, so a change of any other fact leaves
a recommendation fresh (dependency-scoped staleness).
"""

from typing import Final

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
