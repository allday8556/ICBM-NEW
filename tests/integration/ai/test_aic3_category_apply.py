"""ADR-0029 C3: a category result applied to the Preparation as an unconfirmed selection.

The category result comes from the C2 world (the production provider over the AIS-1 fakes and
ICBM's durable catalog). The apply writes ``AI_SUGGESTION`` under the target's current mapping
revision; it never confirms (AIC-04), never overwrites an operator-confirmed selection (AIC-05),
and refuses a result recommended under another taxonomy than the target's.
"""

from typing import Any

import pytest

from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError
from app.stages.register.authoring import (
    AuthoredInputs,
    EnrichmentApply,
    RegistrationPreparationService,
    decode_inputs,
)
from app.stages.register.model import ListingShape
from app.stages.register.policy import StaticRegistrationPolicy
from app.stages.register.preflight import RegistrationPreflightService
from app.stages.register.preparation import (
    CategoryConfirmation,
    CategorySelection,
    ListingValues,
)
from tests.integration.ai.test_aic2_category_task import CATEGORY, _ask, _sync
from tests.integration.ai.test_aic2_category_task import world as world
from tests.support.product_support import Collections, context
from tests.support.register_support import (
    CID,
    OPERATOR,
    FakeCapability,
    ready_item,
    target,
)

pytestmark = pytest.mark.integration


def _unit(
    container: Container, config: AppConfig, world: dict[str, Any], taxonomy: str | None = None
) -> dict[str, Any]:
    _sync(world)
    item = ready_item(
        container,
        Collections.of(container, config),
        "9002",
        pricing=context(marketplace_key="smartstore"),
    )
    account = world["target"].marketplace_account_id
    with container.registrations.transaction() as tx:
        created = tx.create_draft(
            "smartstore",
            account,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR,
            correlation_id=CID,
        )
        tx.add_draft_item(
            created.draft_id,
            item.item_id,
            item.pricing_snapshot_id,
            added_by=OPERATOR,
            correlation_id=CID,
        )
    world["group"] = item.group
    [result] = _ask(container, world)
    assert result.status == "OK", result
    current = world["catalog"].current("smartstore").taxonomy_revision
    policies = StaticRegistrationPolicy(
        (
            target(
                account,
                marketplace_key="smartstore",
                taxonomy_revision=taxonomy or current,
                category_mapping_revision="mapping-smartstore-1",
            ),
        )
    )
    preflight = RegistrationPreflightService(
        registrations=container.registrations,
        readiness=container.product_readiness,
        pricing=container.pricing,
        images=container.images,
        capability=FakeCapability(),
        metadata=container.registration_preflight._metadata,
        policies=policies,
    )
    preparations = RegistrationPreparationService(
        registrations=container.registrations,
        preflight=preflight,
        builder=container.registration_builder,
        enrichment=world["enrichment"],
    )
    record = preparations.create(
        created.draft_id,
        item_ids=[item.item_id],
        inputs=AuthoredInputs(category=None, listing=ListingValues(), detail=None),
        actor="operator",
    )
    return {
        "preparations": preparations,
        "preparation_id": record.preparation_id,
        "group": item.group,
        "result": result,
        "item_id": item.item_id,
    }


def _apply(unit: dict[str, Any], revision: int) -> Any:
    return unit["preparations"].apply_enrichment(
        unit["preparation_id"],
        EnrichmentApply(
            "category",
            unit["group"],
            CATEGORY,
            "category",
            True,
            unit["result"].sequence,
            "category_id",
            revision,
        ),
        actor="operator",
    )


def test_an_apply_writes_an_unconfirmed_selection_under_the_targets_mapping(
    container: Container, config: AppConfig, world: dict[str, Any]
) -> None:
    unit = _unit(container, config, world)
    applied = _apply(unit, 1)
    selection = decode_inputs(applied.current).category
    assert selection == CategorySelection(
        category_id="50000803",
        mapping_revision="mapping-smartstore-1",
        taxonomy_revision=unit["result"].value["taxonomy_revision"],
        confirmation=CategoryConfirmation.AI_SUGGESTION,
    )


def test_an_operator_confirmed_category_is_locked(
    container: Container, config: AppConfig, world: dict[str, Any]
) -> None:
    unit = _unit(container, config, world)
    applied = _apply(unit, 1)
    confirmed = unit["preparations"].update(
        unit["preparation_id"],
        item_ids=[unit["item_id"]],
        inputs=AuthoredInputs(
            category=CategorySelection(
                "50000803",
                "mapping-smartstore-1",
                unit["result"].value["taxonomy_revision"],
                CategoryConfirmation.OPERATOR_CONFIRMED,
            ),
            listing=ListingValues(),
            detail=None,
        ),
        actor="operator",
    )
    assert confirmed.current.revision_no == applied.current.revision_no + 1
    with pytest.raises(AppError) as locked:
        _apply(unit, confirmed.current.revision_no)
    assert locked.value.code == "AI_APPLY_FIELD_LOCKED"


def test_a_result_under_another_taxonomy_than_the_targets_is_refused(
    container: Container, config: AppConfig, world: dict[str, Any]
) -> None:
    unit = _unit(container, config, world, taxonomy="smartstore-categories-older")
    with pytest.raises(AppError) as refused:
        _apply(unit, 1)
    assert refused.value.code == "AI_APPLY_RESULT_UNUSABLE"


@pytest.mark.parametrize("value_field", ["whole_category_name", "taxonomy_revision"])
def test_a_category_is_applied_only_from_its_category_id(
    container: Container, config: AppConfig, world: dict[str, Any], value_field: str
) -> None:
    unit = _unit(container, config, world)
    with pytest.raises(AppError) as refused:
        unit["preparations"].apply_enrichment(
            unit["preparation_id"],
            EnrichmentApply(
                "category",
                unit["group"],
                CATEGORY,
                "category",
                True,
                unit["result"].sequence,
                value_field,
                1,
            ),
            actor="operator",
        )
    assert refused.value.code == "AI_APPLY_VALUE_INVALID"
    assert refused.value.details == {"value_field": value_field, "expected": "category_id"}
    assert (
        decode_inputs(unit["preparations"].preparation(unit["preparation_id"]).current).category
        is None
    )
