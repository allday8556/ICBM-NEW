"""ADR-0028 T3: the SmartStore tag task through the production enrichment owner and provider.

The provider is the production ``ProfiledProvider`` over the AIS-1 fakes; the platform's two tag
reads are a fake reader with the ``SmartStoreTagSource`` interface. Nothing leaves the test, and
no tag is ever sent (AIT-01).
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.ai import platform_tags
from app.capabilities.ai.execution import AIExecution
from app.capabilities.ai.platform_tags import SmartStoreTagContext, keywords
from app.capabilities.ai.profiles import ProfiledProvider, ProfileStore, ProviderProfileService
from app.capabilities.ai.search_signal import NoSearchSignal
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError, InputValidationError, PolicyBlockedError
from app.platform.core.secrets import MemorySecretStore
from app.stages.products.enrichment import EnrichmentRequest, EnrichmentService, TargetView
from app.stages.products.materialization import MaterializationStatus
from app.stages.products.tasks import PRODUCT_NAME_TASK, platform_tag_task
from tests.integration.ai.test_aif3_enrichment_results import _run
from tests.integration.ai.test_ais1_provider_profile import (
    FakeComplete,
    FakeProbe,
    _approve_all,
    _serving,
)
from tests.support.product_support import Collections, product
from tests.support.register_support import bind, establish

pytestmark = pytest.mark.integration

TAGS = platform_tags.TAG_TASK_KEY
ENVELOPE = {"evidence": ["original_name"], "requires_review": False}
ANSWER = {
    "product_name": {"recommended": "국산 생들기름 350ml", "confidence": 0.8} | ENVELOPE,
    "tags": {
        "recommended": ["들기름", "생들기름 350ml", " 국산들기름 ", "금지어", "들기름"],
        "confidence": 0.7,
    }
    | ENVELOPE,
}


class FakeReader:
    def __init__(self) -> None:
        self.pool: dict[str, list[tuple[str, int | None]]] = {
            "생들기름": [("들기름", 101), ("참기름", 102)],
        }
        self.restricted = {"금지어"}
        self.unanswered: set[str] = set()
        self.session = True
        self.recommended: list[str] = []
        self.checked: list[tuple[str, ...]] = []

    def recommend(self, keyword: str) -> tuple[Any, ...]:
        if not self.session:
            raise PolicyBlockedError("SMARTSTORE_SESSION_UNAVAILABLE", "no session")
        self.recommended.append(keyword)
        return tuple(SimpleNamespace(text=t, code=c) for t, c in self.pool.get(keyword, []))

    def check(self, tags: tuple[str, ...]) -> dict[str, bool | None]:
        self.checked.append(tags)
        return {tag: None if tag in self.unanswered else tag in self.restricted for tag in tags}


@pytest.fixture
def world(container: Container, config: AppConfig, tmp_path: Path) -> dict[str, Any]:
    container.prompt_registry.seed_on_startup()
    complete = FakeComplete(value=json.loads(json.dumps(ANSWER)))
    store = ProfileStore(container.db, container.clock, container.audit)
    provider = ProfiledProvider(
        store, FakeProbe(_serving(tmp_path)), container.clock, MemorySecretStore(), complete
    )
    _approve_all(ProviderProfileService(store, provider, container.clock))
    reader = FakeReader()
    task = platform_tag_task(
        task_key=TAGS,
        prompt_key=platform_tags.BUNDLE_PROMPT_KEY,
        marketplace="smartstore",
        result_key=platform_tags.TAG_RESULT_KEY,
        fact_fields=platform_tags.TAG_FACT_FIELDS,
        schema_version=platform_tags.TAG_SCHEMA_VERSION,
        context=SmartStoreTagContext(reader, NoSearchSignal(), container.clock.now),
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
    account = establish(container, config, "smartstore", "uid-tags-1")
    return {
        "complete": complete,
        "reader": reader,
        "enrichment": enrichment,
        "group": str(materialized.product_group_id),
        "target": TargetView(marketplace_key="smartstore", marketplace_account_id=account),
    }


def _ask(container: Container, world: dict[str, Any]) -> Any:
    enrichment: EnrichmentService = world["enrichment"]
    sent = enrichment.request(
        world["group"],
        EnrichmentRequest(actor="operator", tasks=[TAGS], target=world["target"]),
        correlation_id="c-t",
    )
    assert [d.decision for d in sent.tasks] == ["QUEUED"]
    _run(container, enrichment, sent.job_id)
    return [r for r in enrichment.results(world["group"]).results if r.task_key == TAGS]


def test_keywords_come_from_the_confirmed_facts_only() -> None:
    def facts(name: str | None, brand: str | None) -> list[dict[str, Any]]:
        fields: dict[str, Any] = {}
        if name is not None:
            fields["original_name"] = {"status": "CONFIRMED", "value": {"text": name}}
        if brand is not None:
            fields["brand"] = {"status": "ABSENT", "value": {"text": brand}}
        return [{"member": "m", "fields": fields}]

    # The cleaned name, then its own words longest first (ADR-0028 §4 note, R0 6079478595).
    assert keywords(facts("[특가] 국산 참기름 (300ml) 2개 세트 선물", None)) == [
        "국산 참기름 세트 선물",
        "참기름",
        "국산",
        "세트",
        "선물",
    ]
    assert keywords(facts("[어바틀] 멀티비타민 앤 미네랄 맥스 90캡슐", None)) == [
        "멀티비타민 앤 미네랄 맥스",
        "멀티비타민",
        "미네랄",
        "맥스",
    ]
    assert keywords(facts(None, "브랜드")) == []  # an unconfirmed brand is no fact
    assert keywords(facts("x" * 80, None)) == ["x" * 50]


def test_a_tag_request_needs_a_smartstore_target(
    container: Container, world: dict[str, Any]
) -> None:
    enrichment: EnrichmentService = world["enrichment"]
    with pytest.raises(InputValidationError) as untargeted:
        enrichment.request(
            world["group"],
            EnrichmentRequest(actor="operator", tasks=[TAGS]),
            correlation_id="c",
        )
    assert untargeted.value.code == "AI_TASK_NEEDS_TARGET"
    # Another marketplace's account is not this task's target.
    other = TargetView(
        marketplace_key="coupang",
        marketplace_account_id=world["target"].marketplace_account_id,
    )
    with pytest.raises(AppError) as foreign:
        enrichment.request(
            world["group"],
            EnrichmentRequest(actor="operator", tasks=[TAGS], target=other),
            correlation_id="c",
        )
    assert foreign.value.code in ("AI_TARGET_UNKNOWN", "AI_TASK_NEEDS_TARGET")
    assert world["complete"].calls == [] and world["reader"].recommended == []


def test_the_pipeline_keeps_platform_codes_and_drops_what_cannot_be_used(
    container: Container, world: dict[str, Any]
) -> None:
    [result] = _ask(container, world)
    assert (result.status, result.enrichment_schema_version) == ("OK", "tag-stage-1")
    assert result.target is not None and result.target.marketplace_key == "smartstore"
    value = result.value
    assert value["recommended"] == [
        {"text": "들기름", "code": 101, "source": "PLATFORM"},
        {"text": "국산들기름", "code": None, "source": "AI_DIRECT"},
    ]
    assert {r["reason"] for r in value["removed"]} == {"name_or_brand", "restricted"}
    assert value["restricted_checked_at"]
    # A direct-input tag and a removed tag both ask for review.
    assert result.requires_review is True
    assert world["reader"].recommended == ["생들기름", "예시 브랜드"]
    assert world["reader"].checked == [("들기름", "국산들기름", "금지어")]
    assert len(world["complete"].calls) == 1
    assert result.stale is False


def test_an_unchanged_pool_reuses_and_a_changed_pool_asks_again(
    container: Container, world: dict[str, Any]
) -> None:
    first = _ask(container, world)[0]
    again = _ask(container, world)[0]
    assert again.sequence == first.sequence and len(world["complete"].calls) == 1
    world["reader"].pool["생들기름"].append(("들기름오일", 103))
    moved = _ask(container, world)[0]
    assert moved.sequence == first.sequence + 1 and len(world["complete"].calls) == 2


def test_nothing_usable_is_a_failed_result_never_an_empty_one(
    container: Container, world: dict[str, Any]
) -> None:
    world["reader"].restricted = {"들기름", "국산들기름", "금지어"}
    [result] = _ask(container, world)
    assert (result.status, result.error_code) == ("FAILED", "AI_TAGS_NONE_USABLE")


def test_an_unanswered_tag_is_never_taken_as_unrestricted(
    container: Container, world: dict[str, Any]
) -> None:
    world["reader"].unanswered = {"국산들기름"}
    [result] = _ask(container, world)
    assert [t["text"] for t in result.value["recommended"]] == ["들기름"]
    assert {"text": "국산들기름", "reason": "not_checked"} in result.value["removed"]


def test_no_session_fails_the_task_before_any_ai_call(
    container: Container, world: dict[str, Any]
) -> None:
    world["reader"].session = False
    [result] = _ask(container, world)
    assert (result.status, result.error_code) == ("FAILED", "SMARTSTORE_SESSION_UNAVAILABLE")
    assert world["complete"].calls == []


def test_readiness_reports_the_search_signal_port_as_not_configured(config: AppConfig) -> None:
    from app.main import create_app
    from tests.conftest import LOCAL

    with TestClient(create_app(config), base_url=LOCAL) as client:
        ready = client.get("/api/ready", headers={"X-ICBM-Client": "pytest"}).json()
    signal = [c for c in ready["capabilities"] if c["key"] == "search_signal"]
    assert signal == [
        {
            "key": "search_signal",
            "status": "NOT_CONFIGURED",
            "detail": "no search-signal source is configured (ADR-0028 §3)",
        }
    ]


def test_only_the_account_bound_to_the_connection_is_a_tag_target(
    container: Container, config: AppConfig, world: dict[str, Any]
) -> None:
    """GPT audit of #277: the reads go through the one committed SmartStore connection, so a
    target that is not the account bound to it is refused at the request, and again in the job
    when the binding moved in between, before any read or call."""
    enrichment: EnrichmentService = world["enrichment"]
    sent = enrichment.request(
        world["group"],
        EnrichmentRequest(actor="operator", tasks=[TAGS], target=world["target"]),
        correlation_id="c-b",
    )
    # The connection is rebound to another provider account before the job runs.
    bind(config, "smartstore", "uid-someone-else")
    _run(container, enrichment, sent.job_id)
    [result] = [r for r in enrichment.results(world["group"]).results if r.task_key == TAGS]
    assert (result.status, result.error_code) == ("FAILED", "AI_TARGET_NOT_BOUND")
    assert world["reader"].recommended == [] and world["complete"].calls == []
    with pytest.raises(PolicyBlockedError) as refused:
        enrichment.request(
            world["group"],
            EnrichmentRequest(actor="operator", tasks=[TAGS], target=world["target"]),
            correlation_id="c-b2",
        )
    assert refused.value.code == "AI_TARGET_NOT_BOUND"
