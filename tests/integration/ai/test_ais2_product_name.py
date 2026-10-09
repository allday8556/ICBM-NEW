"""ADR-0027 AIS-2: the product-name task through the profiled provider, and the seed upgrade.

Offline: the provider is the production ``ProfiledProvider`` over a fake serving process and a
fake completion (the AIS-1 fakes), so the whole production path runs — profile, identity check,
composition, the production task, the result owner and the register apply — with nothing sent.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.ai import prompts
from app.capabilities.ai.execution import AIExecution
from app.capabilities.ai.profiles import ProfiledProvider, ProfileStore, ProviderProfileService
from app.capabilities.ai.prompts import ResetRequest, ReviseRequest
from app.capabilities.ai.registry import CATALOG
from app.capabilities.audit.models import AuditEventType
from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.secrets import MemorySecretStore
from app.stages.products.enrichment import EnrichmentRequest, EnrichmentService
from app.stages.products.tasks import PRODUCT_NAME_TASK, PRODUCTION_TASKS
from app.stages.register.authoring import (
    AuthoredInputs,
    EnrichmentApply,
    RegistrationPreparationService,
)
from app.stages.register.preparation import ListingValues
from tests.integration.ai.test_aif3_enrichment_results import _run
from tests.integration.ai.test_ais1_provider_profile import (
    FakeComplete,
    FakeProbe,
    _approve_all,
    _serving,
)
from tests.support.product_support import Collections
from tests.support.register_support import MARKET, draft, establish, ready_item

pytestmark = pytest.mark.integration

_REAL_SEED = prompts.load_seed

BUNDLE = PRODUCT_NAME_TASK.task_key
ENVELOPE = {"evidence": ["original_name"], "requires_review": False}
ANSWER = {
    "product_id": "",
    "product_name": {
        "current": "생들기름",
        "recommended": "국산 생들기름 350ml",
        "used_keywords": ["생들기름"],
        "confidence": 0.82,
    }
    | ENVELOPE,
    # The bundle's other keys belong to later stages: answered, never recorded.
    "tags": {"recommended": ["들기름"], "confidence": 0.7} | ENVELOPE,
    "warnings": [],
}


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def world(
    container: Container, config: AppConfig, sources: Collections, tmp_path: Path
) -> dict[str, Any]:
    container.prompt_registry.seed_on_startup()
    complete = FakeComplete(value=ANSWER)
    store = ProfileStore(container.db, container.clock, container.audit)
    provider = ProfiledProvider(
        store, FakeProbe(_serving(tmp_path)), container.clock, MemorySecretStore(), complete
    )
    _approve_all(ProviderProfileService(store, provider, container.clock))
    enrichment = EnrichmentService(
        db=container.db,
        clock=container.clock,
        audit=container.audit,
        jobs=container.jobs,
        products=container.product_store,
        accounts=container.accounts,
        composer=container.ai_composer,
        execution=AIExecution(provider),
        tasks=PRODUCTION_TASKS,
    )
    account = establish(container, config, MARKET, "uid-ai-name-1")
    item = ready_item(container, sources, "4321")
    preparations = RegistrationPreparationService(
        registrations=container.registrations,
        preflight=container.registration_preflight,
        builder=container.registration_builder,
        enrichment=enrichment,
    )
    record = preparations.create(
        draft(container.registrations, account, [item]),
        item_ids=[item.item_id],
        inputs=AuthoredInputs(category=None, listing=ListingValues(), detail=None),
        actor="operator",
    )
    return {
        "complete": complete,
        "enrichment": enrichment,
        "preparations": preparations,
        "preparation_id": record.preparation_id,
        "group": item.group,
    }


def _ask(container: Container, world: dict[str, Any]) -> Any:
    enrichment: EnrichmentService = world["enrichment"]
    sent = enrichment.request(
        world["group"], EnrichmentRequest(actor="operator", tasks=[BUNDLE]), correlation_id="c-n"
    )
    if sent.job_id is not None:
        _run(container, enrichment, sent.job_id)
    return enrichment.results(world["group"])


def test_the_product_name_task_runs_through_the_profile_and_flows_to_the_apply(
    container: Container, world: dict[str, Any], config: AppConfig
) -> None:
    view = _ask(container, world)
    assert view.ai_capability.status.value == "READY"
    # Only the stage's result key is recorded, though the bundle answered more.
    assert [(r.result_key, r.status) for r in view.results] == [("product_name", "OK")]
    result = view.results[0]
    assert result.value == {
        "current": "생들기름",
        "recommended": "국산 생들기름 350ml",
        "used_keywords": ["생들기름"],
    }
    assert (result.confidence, result.requires_review, result.stale) == (0.82, False, False)
    assert result.enrichment_schema_version == "name-stage-1"
    assert result.provenance["requested_model"] == "gpt-5.6-sol"
    assert result.provenance["actual_model"] == "gpt-5.6-sol-2026"
    assert result.provenance["vendor_cost"] is None
    assert len(world["complete"].calls) == 1
    # Unchanged inputs are reused: no second call.
    assert _ask(container, world).results[0].sequence == 1
    assert len(world["complete"].calls) == 1
    applied = world["preparations"].apply_enrichment(
        world["preparation_id"],
        EnrichmentApply("name", world["group"], BUNDLE, "product_name", False, 1, "recommended", 1),
        actor="operator",
    )
    assert dict(applied.current.listing)["name"]["provenance"] == "AI_SUGGESTION"
    assert dict(applied.current.listing)["name"]["value"] == "국산 생들기름 350ml"
    # The prompt text never reaches an audit record.
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        audit = "\n".join(str(r) for r in raw.execute("SELECT * FROM audit_events"))
    assert "GOAL" not in audit and "국산 생들기름" not in audit


def test_an_answer_without_the_envelope_is_a_failed_result(
    container: Container, world: dict[str, Any]
) -> None:
    bare = json.loads(json.dumps(ANSWER))
    del bare["product_name"]["evidence"]
    world["complete"].value = bare
    result = _ask(container, world).results[0]
    assert (result.status, result.error_code) == ("FAILED", "AI_OUTPUT_SCHEMA_INVALID")


def _old_seed() -> dict[str, Any]:
    """The seed as it was before ``v29-ai1``: the first upgrade's replaced text, version ``v29``."""
    seed = _REAL_SEED()
    first = seed["upgrades"][0]
    family = "policies" if first["key"] in seed["policies"] else "templates"
    seed[family][first["key"]]["content"][first["field"]] = first["previous"]
    seed["seed_version"] = "v29"
    return seed


def test_a_fresh_store_is_seeded_upgraded_and_writes_no_audit(
    container: Container, config: AppConfig
) -> None:
    assert container.prompt_registry.seed_on_startup() == len(CATALOG)
    output = container.prompt_registry._store.entry(BUNDLE).current.content["output"]
    assert '"evidence":[],"requires_review":false' in output
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        assert raw.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
        versions = {
            row[0]
            for row in raw.execute("SELECT DISTINCT seed_version FROM ai_prompt_template_revisions")
        }
    assert versions == {"v29-ai3"}


def test_an_unmodified_store_is_upgraded_once_and_an_edit_is_never_overwritten(
    container: Container, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    newest = prompts.seed_upgrades()[0].text
    previous = prompts.seed_upgrades()[0].previous
    with monkeypatch.context() as patch:
        patch.setattr(prompts, "load_seed", _old_seed)
        container.prompt_registry._store.seed_missing(correlation_id="old")
    store = container.prompt_registry._store
    assert store.entry(BUNDLE).current.content["output"] == previous
    # The 기본값 is already the newest seed: an unmodified entry reads as unmodified.
    assert store.entry(BUNDLE).seed.content["output"] == newest
    assert store.upgrade_seeds(correlation_id="up") == 1
    upgraded = store.entry(BUNDLE)
    assert upgraded.current.content["output"] == newest
    assert (upgraded.current.origin.value, upgraded.current.authored_by) == ("RESET", "system:seed")
    assert store.upgrade_seeds(correlation_id="again") == 0
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        events = [
            json.loads(row[0])
            for row in raw.execute(
                "SELECT details_json FROM audit_events WHERE event_type = ?",
                (AuditEventType.AI_PROMPT_TEMPLATE_REVISED,),
            )
        ]
    assert events == [
        {"layer": "TASK", "field": "output", "origin": "RESET", "seed_version": "v29-ai3"}
    ]
    view = container.prompt_registry.registry()
    entry = next(e for e in view.entries if e.key == BUNDLE)
    assert entry.modified_fields == []


def test_an_operator_edit_survives_the_upgrade_and_reset_restores_the_newest_seed(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    newest = prompts.seed_upgrades()[0].text
    with monkeypatch.context() as patch:
        patch.setattr(prompts, "load_seed", _old_seed)
        container.prompt_registry._store.seed_missing(correlation_id="old")
    registry = container.prompt_registry
    current = container.prompt_registry._store.entry(BUNDLE).current
    edited = registry.revise(
        BUNDLE,
        "template",
        ReviseRequest(
            actor="operator",
            expected_current_revision=current.revision_id,
            field="output",
            text='{"product_name":{"recommended":""}}',
        ),
        cid="c-e",
    )
    assert edited.modified_fields == ["output"]
    assert container.prompt_registry._store.upgrade_seeds(correlation_id="up") == 0
    assert container.prompt_registry._store.entry(BUNDLE).current.content["output"] != newest
    reset = registry.reset(
        BUNDLE,
        "template",
        ResetRequest(
            actor="operator", expected_current_revision=edited.current.revision_id, field="output"
        ),
        cid="c-r",
    )
    assert reset.content["output"] == newest
    assert reset.modified_fields == []


def test_production_binds_the_name_task_and_reads_the_capability(config: AppConfig) -> None:
    from app.main import create_app
    from tests.conftest import LOCAL

    with TestClient(create_app(config), base_url=LOCAL) as client:
        container: Container = client.app.state.container  # type: ignore[attr-defined]
        tasks = container.enrichment._tasks
        assert tasks[BUNDLE] == PRODUCT_NAME_TASK
        # ADR-0028 T3: the SmartStore tag task composes the same bundle under its own id.
        assert set(tasks) == {BUNDLE, "SMARTSTORE_TAGS_V1", "SMARTSTORE_CATEGORY_V1"}
        assert tasks["SMARTSTORE_TAGS_V1"].prompt_key == BUNDLE
        view = client.get(
            "/api/v1/products/00000000-0000-0000-0000-000000000000/enrichment",
            headers={"X-ICBM-Client": "pytest"},
        )
    assert view.status_code in (200, 404)
