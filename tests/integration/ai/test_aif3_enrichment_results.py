"""ADR-0026 AIF-3: PRODUCT DB's structured enrichment results, their fingerprint, reuse and
derived staleness, and the ``enrich.tasks`` job.

Production binds no AI provider and defines no task: every request is refused before anything is
written. The owner's behaviour with a provider is proven with the deterministic fake of the AIF-2
suite and a task definition that exists only in this test (the bundle task's four result keys).
"""

import json
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.ai.execution import AIExecution
from app.capabilities.ai.prompts import ReviseRequest
from app.capabilities.ai.provider import ProviderOutcome
from app.capabilities.audit.models import AuditEventType
from app.capabilities.jobs.models import Job
from app.capabilities.jobs.registry import JobContext
from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.errors import (
    AppError,
    InputValidationError,
    NotFoundError,
    RateLimitedError,
)
from app.stages.collect.facts import TextValue
from app.stages.products.enrichment import (
    ENRICH_JOB,
    EnrichmentRequest,
    EnrichmentService,
    ResultSchema,
    TargetView,
    TaskDefinition,
)
from app.stages.products.materialization import MaterializationStatus
from tests.integration.ai.test_aif2_provider_port import FakeProvider, _provenance
from tests.support.collect_support import confirmed
from tests.support.product_support import Collections, product

pytestmark = pytest.mark.integration

BUNDLE = "TASK_PRODUCT_RECOMMEND_BUNDLE_V1"
TASK = TaskDefinition(
    task_key=BUNDLE,
    results={
        "product_name": ResultSchema(required=("recommended",)),
        "tags": ResultSchema(required=("recommended",)),
        "category": ResultSchema(required=("category_id",)),
        "options": ResultSchema(required=("normalized",)),
    },
    fact_fields=("original_name", "brand"),
    schema_version="test-bundle-1",
)
ENVELOPE = {"evidence": ["original_name"], "requires_review": False}
ANSWER = {
    "product_name": {
        "current": "생들기름",
        "recommended": "국산 생들기름 350ml",
        "confidence": 0.82,
    }
    | ENVELOPE,
    "tags": {"recommended": ["생들기름", "들기름"], "confidence": 0.7} | ENVELOPE,
    "category": {
        "category_id": "50000803",
        "confidence": 0.55,
        "evidence": ["original_name"],
        "requires_review": True,
    },
    "options": {"normalized": [], "source_sku_count": 1, "result_sku_count": 1, "confidence": 1}
    | ENVELOPE,
}


def _ok(value: dict[str, Any] | None = None) -> FakeProvider:
    return FakeProvider(
        outcome=ProviderOutcome(ok=True, provenance=_provenance(), value=value or ANSWER)
    )


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def group(container: Container, sources: Collections) -> str:
    container.prompt_registry.seed_on_startup()
    run_id, _ = sources.collect(product())
    result = container.materializer.materialize_run(run_id)
    assert result.status is MaterializationStatus.MATERIALIZED, result
    return str(result.product_group_id)


def _service(container: Container, provider: FakeProvider) -> EnrichmentService:
    return EnrichmentService(
        db=container.db,
        clock=container.clock,
        audit=container.audit,
        jobs=container.jobs,
        products=container.product_store,
        accounts=container.accounts,
        composer=container.ai_composer,
        execution=AIExecution(provider),
        tasks=(TASK,),
    )


def _run(container: Container, service: EnrichmentService, job_id: str, attempt: int = 1) -> None:
    with container.db.read() as session:
        job = session.get(Job, job_id)
        assert job is not None and job.job_type == ENRICH_JOB
        payload, cid, max_attempts = (
            json.loads(job.payload_json),
            job.correlation_id,
            job.max_attempts,
        )
    service.job_definition().handler(
        JobContext(
            job_id=job_id,
            job_type=ENRICH_JOB,
            attempt_no=attempt,
            max_attempts=max_attempts,
            correlation_id=cid,
            target_ref=None,
            payload=payload,
        )
    )


def _request(service: EnrichmentService, group: str, **kwargs: Any) -> Any:
    return service.request(
        group, EnrichmentRequest(actor="operator", tasks=[BUNDLE], **kwargs), correlation_id="c-1"
    )


def _results(service: EnrichmentService, group: str) -> dict[str, Any]:
    return {r.result_key: r for r in service.results(group).results}


def _count(config: AppConfig, sql: str) -> int:
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        return int(raw.execute(sql).fetchone()[0])


def _restate_name(container: Container, sources: Collections, name: str, **fields: Any) -> None:
    """The supplier's page now says another name: a new revision, moved to current."""
    run_id, _ = sources.collect(
        product(original_name=confirmed(TextValue(text=name), ".name"), **fields)
    )
    assert (
        container.materializer.materialize_run(run_id).status is MaterializationStatus.MATERIALIZED
    )


def test_with_no_provider_a_request_is_refused_and_nothing_is_written(
    container: Container, group: str, config: AppConfig
) -> None:
    jobs = _count(config, "SELECT COUNT(*) FROM jobs")
    with pytest.raises(AppError) as refused:
        container.enrichment.request(
            group, EnrichmentRequest(actor="operator", tasks=[BUNDLE]), correlation_id="c-0"
        )
    assert refused.value.code == "AI_PROVIDER_NOT_CONFIGURED"
    assert _count(config, "SELECT COUNT(*) FROM jobs") == jobs
    view = container.enrichment.results(group)
    assert (view.ai_configured, view.results) == (False, [])


def test_a_run_records_each_result_key_as_its_own_state_and_an_unchanged_input_is_reused(
    container: Container, group: str, config: AppConfig
) -> None:
    fake = _ok()
    service = _service(container, fake)
    queued = _request(service, group)
    assert [t.decision for t in queued.tasks] == ["QUEUED"] and queued.job_id
    _run(container, service, queued.job_id)
    results = _results(service, group)
    assert sorted(results) == ["category", "options", "product_name", "tags"]
    # The value is the object without its envelope; the envelope is kept in its own columns.
    assert results["product_name"].value == {
        "current": "생들기름",
        "recommended": "국산 생들기름 350ml",
    }
    assert results["product_name"].evidence == ["original_name"]
    assert results["product_name"].confidence == 0.82
    assert results["category"].requires_review is True
    assert {r.status for r in results.values()} == {"OK"}
    assert {r.stale for r in results.values()} == {False}
    assert results["tags"].provenance["actual_model"] == "fake-model-1-0101"
    assert results["tags"].input_fingerprint == queued.tasks[0].input_fingerprint
    assert len(fake.requests) == 1
    # The relevant facts went to the provider as runtime data; the prompt is the stores' own.
    assert "생들기름 350ml" in fake.requests[0].text and "TASK_MODE" in fake.requests[0].text
    # The same inputs reuse the results: no call, no job.
    again = _request(service, group)
    assert ([t.decision for t in again.tasks], again.job_id) == (["REUSED"], None)
    assert len(fake.requests) == 1
    # One audit record names the run by statuses and fingerprint, never by a value or a fact.
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        details = [
            json.loads(row[0])
            for row in raw.execute(
                "SELECT details_json FROM audit_events WHERE event_type = ?",
                (AuditEventType.AI_ENRICHMENT_RESULT_RECORDED,),
            )
        ]
    assert len(details) == 1 and details[0]["results"] == dict.fromkeys(results, "OK")
    assert "국산" not in json.dumps(details, ensure_ascii=False)
    # A value never reaches a source fact or a product field: the facts are unchanged.
    assert _count(config, "SELECT COUNT(*) FROM product_facts_revisions") == 1


def test_only_a_dependency_change_makes_a_result_stale_and_says_what_changed(
    container: Container, group: str, sources: Collections
) -> None:
    service = _service(container, _ok())
    _run(container, service, _request(service, group).job_id)
    # A field the task does not depend on changes: the results stay fresh.
    _restate_name(container, sources, "생들기름 350ml", prices=product(price=12000)["prices"])
    assert {r.stale for r in _results(service, group).values()} == {False}
    # Its own fact changes: every result of it is stale, because of the facts.
    _restate_name(container, sources, "참들기름 500ml")
    stale = _results(service, group)
    assert {tuple(r.stale_reasons) for r in stale.values()} == {("facts",)}
    # So does its task prompt, and the reason says so.
    entry = next(e for e in container.prompt_registry.registry().entries if e.key == BUNDLE)
    container.prompt_registry.revise(
        BUNDLE,
        "template",
        ReviseRequest(
            actor="operator",
            expected_current_revision=entry.current.revision_id,
            field="prompt",
            text="GOAL\n수정된 작업",
        ),
        cid="c-2",
    )
    assert {tuple(r.stale_reasons) for r in _results(service, group).values()} == {
        ("facts", "prompt")
    }
    # A stale task is queued again and its new results are fresh.
    queued = _request(service, group)
    assert queued.tasks[0].decision == "QUEUED"
    _run(container, service, queued.job_id)
    fresh = _results(service, group)
    assert {(r.sequence, r.stale) for r in fresh.values()} == {(2, False)}


def test_a_missing_result_key_fails_alone_and_a_retry_re_runs_only_it(
    container: Container, group: str
) -> None:
    partial = {k: v for k, v in ANSWER.items() if k != "options"}
    service = _service(container, _ok(partial))
    _run(container, service, _request(service, group).job_id)
    results = _results(service, group)
    assert results["options"].status == "FAILED"
    assert (results["options"].error_class, results["options"].error_code) == (
        "VALIDATION",
        "AI_OUTPUT_FIELD_MISSING",
    )
    assert results["options"].value is None
    assert {results[k].status for k in ("product_name", "tags", "category")} == {"OK"}
    # The task is not fresh while one key failed, so it is queued; the run records only that key.
    whole = _service(container, _ok())
    queued = _request(whole, group)
    assert queued.tasks[0].decision == "QUEUED"
    _run(container, whole, queued.job_id)
    after = _results(whole, group)
    assert (after["options"].status, after["options"].sequence) == ("OK", 2)
    assert {after[k].sequence for k in ("product_name", "tags", "category")} == {1}


def test_a_rate_limit_is_retried_without_a_record_and_fails_only_on_the_last_attempt(
    container: Container, group: str, config: AppConfig
) -> None:
    limited = _service(container, FakeProvider(raises=RateLimitedError("AI_RATE_LIMITED", "slow")))
    job_id = _request(limited, group).job_id
    with pytest.raises(RateLimitedError):
        _run(container, limited, job_id, attempt=1)
    assert _count(config, "SELECT COUNT(*) FROM product_enrichment_results") == 0
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        last = raw.execute("SELECT max_attempts FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[
            0
        ]
    _run(container, limited, job_id, attempt=last)
    failed = _results(limited, group)
    assert {(r.status, r.error_class) for r in failed.values()} == {("FAILED", "RATE_LIMITED")}
    # A failed run's provenance holds only the request: nothing is invented.
    assert {r.provenance["actual_model"] for r in failed.values()} == {None}


def test_a_request_names_a_known_product_runnable_tasks_and_a_canonical_target(
    container: Container, group: str
) -> None:
    service = _service(container, _ok())
    with pytest.raises(NotFoundError) as unknown:
        _request(service, "00000000-0000-0000-0000-000000000000")
    assert unknown.value.code == "PRODUCTS_PRODUCT_UNKNOWN"
    with pytest.raises(InputValidationError) as task:
        service.request(
            group,
            EnrichmentRequest(actor="operator", tasks=["TASK_INQUIRY_REPLY_V1"]),
            correlation_id="c-3",
        )
    assert task.value.code == "AI_TASK_NOT_RUNNABLE"
    with pytest.raises(InputValidationError) as policy:
        _request(
            service,
            group,
            target=TargetView(marketplace_key="gmarket", marketplace_account_id="mka_x"),
        )
    assert policy.value.code == "AI_TARGET_HAS_NO_POLICY"
    with pytest.raises(NotFoundError) as account:
        _request(
            service,
            group,
            target=TargetView(marketplace_key="smartstore", marketplace_account_id="mka_none"),
        )
    assert account.value.code == "AI_TARGET_UNKNOWN"


def test_the_database_refuses_to_rewrite_a_result(
    container: Container, group: str, config: AppConfig
) -> None:
    service = _service(container, _ok())
    _run(container, service, _request(service, group).job_id)
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        for statement in (
            "UPDATE product_enrichment_results SET value_json = '{}'",
            "DELETE FROM product_enrichment_results",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                raw.execute(statement)
    assert _count(config, "SELECT COUNT(*) FROM product_enrichment_results") == 4


def test_the_routes_read_results_and_refuse_a_request_with_no_provider(config: AppConfig) -> None:
    from app.main import create_app
    from tests.conftest import LOCAL

    headers = {"X-ICBM-Client": "pytest"}
    with TestClient(create_app(config), base_url=LOCAL) as served:
        app_container: Container = served.app.state.container  # type: ignore[attr-defined]
        run_id, _ = Collections.of(app_container, config).collect(product())
        result = app_container.materializer.materialize_run(run_id)
        group = str(result.product_group_id)
        read = served.get(f"/api/v1/products/{group}/enrichment", headers=headers)
        assert read.status_code == 200
        assert read.json() == {"product_group_id": group, "ai_configured": False, "results": []}
        refused = served.post(
            f"/api/v1/products/{group}/enrichment",
            headers=headers,
            json={"actor": "operator", "tasks": [BUNDLE]},
        )
        assert (refused.status_code, refused.json()["error"]["code"]) == (
            403,
            "AI_PROVIDER_NOT_CONFIGURED",
        )
        unknown = served.get(
            "/api/v1/products/00000000-0000-0000-0000-000000000000/enrichment", headers=headers
        )
        assert unknown.status_code == 404


def test_an_object_without_the_structured_envelope_or_a_required_field_is_never_ok(
    container: Container, group: str
) -> None:
    broken = {
        **ANSWER,
        # No confidence: the envelope is incomplete.
        "product_name": {"recommended": "이름", "evidence": [], "requires_review": False},
        # A confidence outside 0..1.
        "tags": {"recommended": ["a"], "confidence": 1.5, "evidence": [], "requires_review": False},
        # The required value field is missing.
        "category": {"confidence": 0.5, "evidence": [], "requires_review": True},
    }
    service = _service(container, _ok(broken))
    _run(container, service, _request(service, group).job_id)
    results = _results(service, group)
    for key in ("product_name", "tags", "category"):
        assert (results[key].status, results[key].error_code) == (
            "FAILED",
            "AI_OUTPUT_SCHEMA_INVALID",
        ), key
        assert results[key].value is None
    assert results["options"].status == "OK"
