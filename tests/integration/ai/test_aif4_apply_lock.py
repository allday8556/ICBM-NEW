"""ADR-0026 AIF-4: an enrichment result applied to the register Preparation, behind the lock and
the revision check.

The register Preparation stays the only owner of a listing's outbound values. An apply appends
one revision whose field holds the result's value as ``AI_SUGGESTION``, which never satisfies a
required field. A confirmed value (``OPERATOR_CONFIRMED`` or ``SOURCE_FACT``) is never overwritten,
and a Preparation that moved past the revision the operator read is skipped, never overwritten.
Results come from the AIF-3 owner with the test-only fake provider; production has none.
"""

import json
import sqlite3
from typing import Any

import pytest

from app.capabilities.ai.execution import AIExecution
from app.capabilities.ai.prompts import ReviseRequest
from app.capabilities.audit.models import AuditEventType
from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.errors import AppError
from app.stages.register.authoring import (
    AuthoredInputs,
    EnrichmentApply,
    RegistrationPreparationService,
)
from app.stages.register.policy import Provenance
from app.stages.register.preparation import FieldValue, ListingValues
from tests.integration.ai.test_aif3_enrichment_results import (
    ANSWER,
    BUNDLE,
    TASK,
    _ok,
    _request,
    _run,
)
from tests.support.product_support import Collections
from tests.support.register_support import MARKET, draft, establish, ready_item

pytestmark = pytest.mark.integration


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def world(container: Container, config: AppConfig, sources: Collections) -> dict[str, Any]:
    """One READY Item in a Draft, its Preparation, and a fresh bundle result of its product."""
    container.prompt_registry.seed_on_startup()
    account = establish(container, config, MARKET, "uid-ai-apply-1")
    item = ready_item(container, sources, "1234")
    draft_id = draft(container.registrations, account, [item])
    enrichment = _fake_enrichment(container)
    _run(container, enrichment, _request(enrichment, item.group).job_id)
    preparations = RegistrationPreparationService(
        registrations=container.registrations,
        preflight=container.registration_preflight,
        builder=container.registration_builder,
        enrichment=enrichment,
    )
    record = preparations.create(
        draft_id,
        item_ids=[item.item_id],
        inputs=AuthoredInputs(category=None, listing=ListingValues(), detail=None),
        actor="operator",
    )
    return {
        "preparations": preparations,
        "enrichment": enrichment,
        "preparation_id": record.preparation_id,
        "group": item.group,
    }


def _fake_enrichment(container: Container) -> Any:
    from app.stages.products.enrichment import EnrichmentService

    return EnrichmentService(
        db=container.db,
        clock=container.clock,
        audit=container.audit,
        jobs=container.jobs,
        products=container.product_store,
        accounts=container.accounts,
        composer=container.ai_composer,
        execution=AIExecution(_ok()),
        tasks=(TASK,),
    )


def _apply(
    world: dict[str, Any], *, field: str = "name", revision: int = 1, **overrides: Any
) -> Any:
    values: dict[str, Any] = {
        "field": field,
        "product_group_id": world["group"],
        "task_key": BUNDLE,
        "result_key": "product_name",
        "result_sequence": 1,
        "value_field": "recommended",
        "expected_revision_no": revision,
    }
    values.update(overrides)
    return world["preparations"].apply_enrichment(
        world["preparation_id"], EnrichmentApply(**values), actor="operator"
    )


def _listing(record: Any) -> dict[str, Any]:
    return dict(record.current.listing)


def test_an_apply_appends_one_revision_as_an_ai_suggestion_that_never_satisfies(
    world: dict[str, Any], config: AppConfig
) -> None:
    applied = _apply(world)
    assert applied.current.revision_no == 2
    assert _listing(applied)["name"] == {
        "value": ANSWER["product_name"]["recommended"],
        "provenance": "AI_SUGGESTION",
        "detail_page_reference": False,
    }
    # The value is a suggestion: the preflight's rule for it is unchanged (ADR-0014 §18).
    from app.stages.register.policy import SATISFYING

    assert Provenance.AI_SUGGESTION not in SATISFYING
    # An unconfirmed AI suggestion may be replaced by a newer result.
    # An unconfirmed AI suggestion does not lock: a field it holds may take a newer result, and
    # another field may take another result key's scalar.
    again = _apply(
        world,
        field="attribute.category_hint",
        revision=2,
        result_key="category",
        value_field="category_id",
    )
    assert again.current.revision_no == 3
    assert _listing(again)["attributes"]["category_hint"]["value"] == "50000803"
    replaced = _apply(world, revision=3)
    assert replaced.current.revision_no == 4
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        applied_events = [
            json.loads(row[0])
            for row in raw.execute(
                "SELECT details_json FROM audit_events WHERE event_type = ? ORDER BY seq",
                (AuditEventType.AI_ENRICHMENT_APPLIED,),
            )
        ]
    assert [(e["revision_no"], e["result_key"]) for e in applied_events] == [
        (2, "product_name"),
        (3, "category"),
        (4, "product_name"),
    ]
    # By ids and fingerprint only: never the value.
    assert "국산" not in json.dumps(applied_events, ensure_ascii=False)


def test_a_confirmed_value_is_locked_and_a_moved_revision_is_skipped(
    world: dict[str, Any],
) -> None:
    preparations: RegistrationPreparationService = world["preparations"]
    # The operator confirms a name of their own: revision 2.
    confirmed = preparations.update(
        world["preparation_id"],
        item_ids=list(preparations.preparation(world["preparation_id"]).current.item_ids),
        inputs=AuthoredInputs(
            category=None,
            listing=ListingValues(
                name=FieldValue(value="운영자 상품명", provenance=Provenance.OPERATOR_CONFIRMED)
            ),
            detail=None,
        ),
        actor="operator",
    )
    assert confirmed.current.revision_no == 2
    with pytest.raises(AppError) as locked:
        _apply(world, revision=2)
    assert locked.value.code == "AI_APPLY_FIELD_LOCKED"
    # Read at revision 1, but the preparation is at 2: skipped, never overwritten.
    with pytest.raises(AppError) as moved:
        _apply(world, field="notice.manufacturer", revision=1)
    assert moved.value.code == "AI_APPLY_REVISION_CHANGED"
    assert moved.value.details["current_revision_no"] == 2
    after = preparations.preparation(world["preparation_id"])
    assert after.current.revision_no == 2
    assert _listing(after)["name"]["value"] == "운영자 상품명"


def test_only_a_current_fresh_ok_result_of_this_unit_and_a_known_field_is_applied(
    world: dict[str, Any], container: Container, sources: Collections
) -> None:
    for overrides, code in (
        ({"field": "price"}, "AI_APPLY_FIELD_INVALID"),
        ({"field": "attribute."}, "AI_APPLY_FIELD_INVALID"),
        ({"product_group_id": "00000000-0000-0000-0000-000000000000"}, "AI_APPLY_RESULT_FOREIGN"),
        ({"result_key": "nothing"}, "AI_APPLY_RESULT_UNUSABLE"),
        ({"value_field": "missing"}, "AI_APPLY_VALUE_INVALID"),
        # A list is not a field value.
        ({"result_key": "tags", "value_field": "recommended_list"}, "AI_APPLY_VALUE_INVALID"),
    ):
        with pytest.raises(AppError) as refused:
            _apply(world, **overrides)
        assert refused.value.code == code, overrides
    # A stale result is not applied: the product's name fact changed after the run.
    from tests.integration.ai.test_aif3_enrichment_results import _restate_name

    _restate_name(container, sources, "참들기름 500ml")
    with pytest.raises(AppError) as stale:
        _apply(world)
    assert stale.value.code == "AI_APPLY_RESULT_UNUSABLE"
    assert stale.value.details["stale_reasons"] == ["facts"]
    assert world["preparations"].preparation(world["preparation_id"]).current.revision_no == 1


def test_only_the_named_result_revision_is_applied(
    world: dict[str, Any], container: Container
) -> None:
    # The operator saw sequence 1. A prompt change makes it stale and a new run records
    # sequence 2: the older revision is never applied unseen, and the newer one only when named.
    newer = _fake_enrichment(container)
    entry = next(e for e in container.prompt_registry.registry().entries if e.key == BUNDLE)
    container.prompt_registry.revise(
        BUNDLE,
        "template",
        ReviseRequest(
            actor="operator",
            expected_current_revision=entry.current.revision_id,
            field="prompt",
            text="GOAL\n새 작업 지시",
        ),
        cid="c-new",
    )
    _run(container, newer, _request(newer, world["group"]).job_id)
    with pytest.raises(AppError) as changed:
        _apply(world)
    assert changed.value.code == "AI_APPLY_RESULT_CHANGED"
    assert changed.value.details["result_sequence"] == 2
    # Naming the current one applies it.
    assert _apply(world, result_sequence=2).current.revision_no == 2


def test_production_has_no_result_to_apply(container: Container) -> None:
    # The production preparation owner reads the production enrichment owner: no provider, so no
    # current result exists and an apply ends there.
    with pytest.raises(AppError) as missing:
        container.registration_preparations.apply_enrichment(
            "no-such-preparation",
            EnrichmentApply("name", "g", BUNDLE, "product_name", 1, "recommended", 1),
            actor="operator",
        )
    assert missing.value.code == "REGISTER_PREPARATION_NOT_FOUND"
