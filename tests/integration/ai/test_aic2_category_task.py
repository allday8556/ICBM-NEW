"""ADR-0029 C2: the SmartStore category task, chosen only from the official leaf catalog.

The provider is the production ``ProfiledProvider`` over the AIS-1 fakes; the catalog is ICBM's own
durable snapshot. The task reads no provider, and nothing leaves the test.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.capabilities.ai import platform_category, platform_tags
from app.capabilities.ai.execution import AIExecution
from app.capabilities.ai.platform_category import SmartStoreCategoryContext, candidates, terms
from app.capabilities.ai.profiles import ProfiledProvider, ProfileStore, ProviderProfileService
from app.config import AppConfig
from app.container import Container
from app.platform.core.secrets import MemorySecretStore
from app.stages.products.enrichment import EnrichmentRequest, EnrichmentService, TargetView
from app.stages.products.materialization import MaterializationStatus
from app.stages.products.tasks import PRODUCT_NAME_TASK, platform_tag_task
from app.stages.register.category_catalog import CategoryCatalogStore, ProviderCategory
from tests.integration.ai.test_aif3_enrichment_results import _run
from tests.integration.ai.test_ais1_provider_profile import (
    FakeComplete,
    FakeProbe,
    _approve_all,
    _serving,
)
from tests.support.product_support import Collections, product
from tests.support.register_support import establish

pytestmark = pytest.mark.integration

CATEGORY = platform_category.CATEGORY_TASK_KEY
ENVELOPE = {"evidence": ["original_name"], "requires_review": False}
LEAVES = (
    ProviderCategory("50000803", "들기름", "식품>식용유/오일>들기름", True),
    ProviderCategory("50000804", "참기름", "식품>식용유/오일>참기름", True),
    ProviderCategory("50001234", "운동화", "패션잡화>신발>운동화", True),
)


def _answer(category_id: str = "50000803", confidence: float = 0.9) -> dict[str, Any]:
    return {
        "product_name": {"recommended": "국산 생들기름", "confidence": 0.8} | ENVELOPE,
        "category": {"category_id": category_id, "confidence": confidence} | ENVELOPE,
    }


@pytest.fixture
def world(container: Container, config: AppConfig, tmp_path: Path) -> dict[str, Any]:
    container.prompt_registry.seed_on_startup()
    complete = FakeComplete(value=_answer())
    store = ProfileStore(container.db, container.clock, container.audit)
    provider = ProfiledProvider(
        store, FakeProbe(_serving(tmp_path)), container.clock, MemorySecretStore(), complete
    )
    _approve_all(ProviderProfileService(store, provider, container.clock))
    catalog = CategoryCatalogStore(container.db, container.clock, container.audit)
    task = platform_tag_task(
        task_key=CATEGORY,
        prompt_key=platform_tags.BUNDLE_PROMPT_KEY,
        marketplace="smartstore",
        result_key=platform_category.CATEGORY_RESULT_KEY,
        fact_fields=platform_category.CATEGORY_FACT_FIELDS,
        schema_version=platform_category.CATEGORY_SCHEMA_VERSION,
        context=SmartStoreCategoryContext(catalog, "smartstore"),
        required=("category_id",),
    )
    enrichment = EnrichmentService(
        db=container.db,
        clock=container.clock,
        audit=container.audit,
        jobs=container.jobs,
        products=container.product_store,
        accounts=container.accounts,
        composer=container.ai_composer,
        execution=AIExecution(provider),
        tasks=(PRODUCT_NAME_TASK, task),
    )
    run_id, _ = Collections.of(container, config).collect(product())
    materialized = container.materializer.materialize_run(run_id)
    assert materialized.status is MaterializationStatus.MATERIALIZED
    account = establish(container, config, "smartstore", "uid-category-1")
    return {
        "complete": complete,
        "catalog": catalog,
        "enrichment": enrichment,
        "group": str(materialized.product_group_id),
        "target": TargetView(marketplace_key="smartstore", marketplace_account_id=account),
    }


def _sync(world: dict[str, Any], entries: tuple[ProviderCategory, ...] = LEAVES) -> None:
    world["catalog"].record(
        "smartstore",
        entries,
        endpoint_mapping_revision="test-mapping",
        actor="operator",
        correlation_id="c-cat",
    )


def _ask(container: Container, world: dict[str, Any]) -> Any:
    enrichment: EnrichmentService = world["enrichment"]
    sent = enrichment.request(
        world["group"],
        EnrichmentRequest(actor="operator", tasks=[CATEGORY], target=world["target"]),
        correlation_id="c-c",
    )
    _run(container, enrichment, sent.job_id)
    return [r for r in enrichment.results(world["group"]).results if r.task_key == CATEGORY]


def test_terms_and_candidates_come_from_the_confirmed_name_and_the_leaves_only() -> None:
    facts = [
        {
            "member": "m",
            "fields": {
                "original_name": {
                    "status": "CONFIRMED",
                    "value": {"text": "[특가] 예시 국산 들기름 350ml"},
                },
                "brand": {"status": "CONFIRMED", "value": {"text": "예시"}},
            },
        }
    ]
    assert terms(facts) == ["국산", "들기름"]
    non_leaf = ProviderCategory("50000800", "식용유/오일", "식품>식용유/오일", False)
    found = candidates((*LEAVES, non_leaf), ["국산", "들기름"])
    # The non-leaf "식용유/오일" never qualifies; an unrelated leaf scores nothing.
    assert [c["category_id"] for c in found] == ["50000803"]


def test_the_chosen_leaf_is_recorded_with_its_whole_name_and_revision(
    container: Container, world: dict[str, Any]
) -> None:
    _sync(world)
    [result] = _ask(container, world)
    assert (result.status, result.enrichment_schema_version) == ("OK", "category-stage-1")
    assert result.value["category_id"] == "50000803"
    assert result.value["whole_category_name"] == "식품>식용유/오일>들기름"
    assert result.value["taxonomy_revision"]
    assert result.requires_review is False and len(world["complete"].calls) == 1


def test_an_invented_or_unoffered_id_is_never_an_ok_result(
    container: Container, world: dict[str, Any]
) -> None:
    _sync(world)
    world["complete"].value = _answer("50001234")  # a real leaf, but not a candidate
    [result] = _ask(container, world)
    assert (result.status, result.error_code) == ("FAILED", "AI_CATEGORY_NOT_A_CANDIDATE")


def test_a_low_confidence_pick_asks_for_review(container: Container, world: dict[str, Any]) -> None:
    _sync(world)
    world["complete"].value = _answer(confidence=0.4)
    [result] = _ask(container, world)
    assert result.status == "OK" and result.requires_review is True


def test_no_catalog_or_no_candidate_fails_with_no_ai_call(
    container: Container, world: dict[str, Any]
) -> None:
    [missing] = _ask(container, world)
    assert (missing.status, missing.error_code) == ("FAILED", "AI_CATEGORY_CATALOG_MISSING")
    _sync(world, (ProviderCategory("50001234", "운동화", "패션잡화>신발>운동화", True),))
    [unmatched] = _ask(container, world)
    assert (unmatched.status, unmatched.error_code) == ("FAILED", "AI_CATEGORY_NO_CANDIDATES")
    assert world["complete"].calls == []


def test_a_new_catalog_that_changes_the_candidates_asks_again(
    container: Container, world: dict[str, Any]
) -> None:
    _sync(world)
    first = _ask(container, world)[0]
    assert _ask(container, world)[0].sequence == first.sequence
    _sync(
        world,
        (*LEAVES, ProviderCategory("50000805", "들기름세트", "식품>선물세트>들기름세트", True)),
    )
    world["complete"].value = json.loads(json.dumps(_answer()))
    again = _ask(container, world)[0]
    assert again.sequence == first.sequence + 1 and len(world["complete"].calls) == 2
