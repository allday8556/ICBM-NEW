"""ADR-0028 T4: the Preparation's tags gain a provenance, and a tag result is applied to them.

The tag result comes from the T3 world (the production provider over the AIS-1 fakes and a fake
platform reader). The register owner keeps texts only; the apply writes one AI_SUGGESTION set,
which never satisfies until the operator's own save confirms it; a confirmed set is locked. No tag
is ever sent (AIT-01).
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
    encode_inputs,
)
from app.stages.register.model import ListingShape
from app.stages.register.policy import Provenance
from app.stages.register.preparation import ListingValues
from tests.integration.ai.test_ait3_tag_task import TAGS, _ask
from tests.integration.ai.test_ait3_tag_task import world as world
from tests.support.product_support import Collections, context
from tests.support.register_support import CID, OPERATOR, ready_item

pytestmark = pytest.mark.integration


@pytest.fixture
def unit(container: Container, config: AppConfig, world: dict[str, Any]) -> dict[str, Any]:
    """A SmartStore Draft of the world's product with an empty Preparation, and its tag result."""
    item = ready_item(
        container,
        Collections.of(container, config),
        "9001",
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
    assert result.status == "OK"
    preparations = RegistrationPreparationService(
        registrations=container.registrations,
        preflight=container.registration_preflight,
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
    }


def _reasons(
    preparations: RegistrationPreparationService, unit: dict[str, Any]
) -> set[tuple[str, str]]:
    """The listing reasons of what is authored now, as the preflight owner derives them."""
    from app.stages.register.authoring import preflight_request
    from app.stages.register.preparation import _listing_reasons

    preparation = preparations.preparation(unit["preparation_id"])
    request = preflight_request(preparation, preparation.current)
    return {(r.code, r.subject) for r in _listing_reasons(request, None)}


def _apply(unit: dict[str, Any], revision: int, sequence: int | None = None) -> Any:
    return unit["preparations"].apply_enrichment(
        unit["preparation_id"],
        EnrichmentApply(
            "tags",
            unit["group"],
            TAGS,
            "tags",
            True,
            sequence or unit["result"].sequence,
            "recommended",
            revision,
        ),
        actor="operator",
    )


def test_an_apply_writes_the_texts_as_one_ai_suggestion_set(unit: dict[str, Any]) -> None:
    applied = _apply(unit, 1)
    listing = dict(applied.current.listing)
    assert sorted(listing["tags"]) == ["국산들기름", "들기름"]
    assert listing["tags_provenance"] == "AI_SUGGESTION"
    # The platform code stays with the result: the Preparation holds texts only.
    assert "101" not in str(listing)


def test_an_unconfirmed_ai_set_blocks_until_the_operators_save_confirms_it(
    unit: dict[str, Any],
) -> None:
    from app.stages.register.authoring import decode_inputs

    preparations: RegistrationPreparationService = unit["preparations"]
    applied = _apply(unit, 1)
    assert decode_inputs(applied.current).listing.tags_provenance is Provenance.AI_SUGGESTION
    assert ("FIELD_AI_SUGGESTION_UNCONFIRMED", "tags") in _reasons(preparations, unit)
    saved = preparations.update(
        unit["preparation_id"],
        item_ids=list(applied.current.item_ids),
        inputs=AuthoredInputs(
            category=None,
            listing=ListingValues(tags=frozenset(dict(applied.current.listing)["tags"])),
            detail=None,
        ),
        actor="operator",
    )
    assert decode_inputs(saved.current).listing.tags_provenance is None
    assert ("FIELD_AI_SUGGESTION_UNCONFIRMED", "tags") not in _reasons(preparations, unit)


def test_operator_revisions_encode_exactly_as_before() -> None:
    plain = encode_inputs(
        AuthoredInputs(
            category=None, listing=ListingValues(tags=frozenset({"a", "b"})), detail=None
        )
    )
    assert "tags_provenance" not in plain.listing
    suggested = encode_inputs(
        AuthoredInputs(
            category=None,
            listing=ListingValues(tags=frozenset({"a"}), tags_provenance=Provenance.AI_SUGGESTION),
            detail=None,
        )
    )
    assert suggested.listing["tags_provenance"] == "AI_SUGGESTION"
    assert suggested.fingerprint != plain.fingerprint


def test_a_confirmed_set_is_locked_and_an_ai_set_may_be_replaced(unit: dict[str, Any]) -> None:
    applied = _apply(unit, 1)
    again = _apply(unit, applied.current.revision_no)
    assert again.current.revision_no == applied.current.revision_no + 1
    # The operator saves the tags: the set is now theirs, and locked.
    preparations: RegistrationPreparationService = unit["preparations"]
    saved = preparations.update(
        unit["preparation_id"],
        item_ids=list(again.current.item_ids),
        inputs=AuthoredInputs(
            category=None, listing=ListingValues(tags=frozenset({"내태그"})), detail=None
        ),
        actor="operator",
    )
    assert "tags_provenance" not in dict(saved.current.listing)
    with pytest.raises(AppError) as locked:
        _apply(unit, saved.current.revision_no)
    assert locked.value.code == "AI_APPLY_FIELD_LOCKED"
