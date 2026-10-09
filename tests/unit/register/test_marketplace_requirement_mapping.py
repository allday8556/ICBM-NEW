"""C-P3 marketplace requirement adoption and semantic mapping."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.stages.products.common_option_store import (
    CommonSalesOptionAxisRecord,
    CommonSalesOptionRevisionRecord,
    CommonSalesOptionValueRecord,
)
from app.stages.register.category_metadata import MarketplaceOptionRequirementView
from app.stages.register.marketplace_requirement_mapping import (
    COMMON_AXIS_MISSING,
    INTERPRETATION_REVIEW_REQUIRED,
    LISTING_COMPOSITION_QUANTITY,
    METADATA_MISSING,
    METADATA_SCOPE_MISMATCH,
    METADATA_UNADOPTED,
    SEPARATE_ROLE,
    UNIT_SEMANTICS_MISMATCH,
    MarketplaceRequirementMappingResult,
    RequirementMappingState,
    RequirementTargetKind,
    ReviewedMarketplaceRequirementMetadata,
    map_marketplace_requirements,
)

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _options() -> CommonSalesOptionRevisionRecord:
    return CommonSalesOptionRevisionRecord(
        revision_id="common-1",
        product_group_id="product-1",
        revision_no=1,
        structure_signature="a" * 64,
        signature_version="common-sales-option-signature/v1",
        reason="reviewed",
        decided_by="owner",
        correlation_id="corr",
        created_at=NOW,
        axes=(
            CommonSalesOptionAxisRecord(
                "axis-weight",
                "individual_weight",
                "개별 중량/용량",
                0,
                (CommonSalesOptionValueRecord("weight-300", "300", "300mg", "mg", 0),),
            ),
            CommonSalesOptionAxisRecord(
                "axis-count",
                "unit_count",
                "수량",
                1,
                (CommonSalesOptionValueRecord("count-60", "60", "60정", "tablet", 0),),
            ),
        ),
    )


def _requirement(**changes: object) -> MarketplaceOptionRequirementView:
    values: dict[str, object] = {
        "provider_rule_key": "CNT_PER_UNIT",
        "provider_label": "수량",
        "role": "PURCHASE_OPTION",
        "provider_requiredness": "MANDATORY",
        "requiredness": "REQUIRED",
        "provider_semantics": "count of tablets contained by one sellable unit",
        "semantic_key": "unit_count",
        "value_semantics": "COUNT_PER_UNIT",
        "basic_unit": "tablet",
        "usable_units": ["tablet"],
        "allowed_values": [],
        "interpretation_state": "CONFIRMED",
    }
    values.update(changes)
    return MarketplaceOptionRequirementView.model_validate(values)


def _metadata(
    *requirements: MarketplaceOptionRequirementView, **changes: object
) -> ReviewedMarketplaceRequirementMetadata:
    values: dict[str, object] = {
        "marketplace_key": "coupang",
        "taxonomy_revision": "taxonomy-2026-10",
        "category_id": "health",
        "metadata_revision_id": "metadata-1",
        "adopted": True,
        "reviewed": True,
        "requirements": tuple(requirements or (_requirement(),)),
    }
    values.update(changes)
    return ReviewedMarketplaceRequirementMetadata(**values)  # type: ignore[arg-type]


def _map(
    metadata: ReviewedMarketplaceRequirementMetadata | None = None,
) -> MarketplaceRequirementMappingResult:
    return map_marketplace_requirements(
        _options(),
        _metadata() if metadata is None else metadata,
        marketplace_key="coupang",
        taxonomy_revision="taxonomy-2026-10",
        category_id="health",
    )


def test_explicit_semantic_key_and_units_map_a_purchase_option_axis() -> None:
    result = _map()

    assert result.state is RequirementMappingState.MAPPED
    mapping = result.mappings[0]
    assert mapping.target_kind is RequirementTargetKind.COMMON_SALES_OPTION_AXIS
    assert mapping.target_reference == "axis-count"
    assert mapping.provider_label == "수량"


def test_same_provider_label_never_decides_between_unit_and_selling_quantity() -> None:
    unit_count = _requirement()
    bundle_count = _requirement(
        provider_rule_key="SALE_BUNDLE_COUNT",
        provider_semantics="number of sellable units in the listing composition",
        semantic_key="selling_bundle_quantity",
        value_semantics="SELLING_BUNDLE_QUANTITY",
        basic_unit="count",
        usable_units=["count"],
    )

    result = _map(_metadata(unit_count, bundle_count))

    assert tuple(mapping.provider_label for mapping in result.mappings) == ("수량", "수량")
    assert result.mappings[0].target_reference == "axis-count"
    assert result.mappings[1].target_kind is RequirementTargetKind.LISTING_COMPOSITION_QUANTITY
    assert result.mappings[1].target_reference == LISTING_COMPOSITION_QUANTITY


def test_label_equality_cannot_rescue_wrong_semantics_or_units() -> None:
    wrong_semantic = _requirement(semantic_key="selling_count")
    wrong_unit = _requirement(basic_unit="capsule", usable_units=["capsule"])

    missing = _map(_metadata(wrong_semantic)).mappings[0]
    mismatch = _map(_metadata(wrong_unit)).mappings[0]

    assert missing.state is RequirementMappingState.REVIEW_REQUIRED
    assert missing.reasons[0].code == COMMON_AXIS_MISSING
    assert mismatch.state is RequirementMappingState.REVIEW_REQUIRED
    assert mismatch.reasons[0].code == UNIT_SEMANTICS_MISMATCH


def test_unknown_or_contradictory_requiredness_remains_review_required() -> None:
    requirement = _requirement(
        provider_requiredness="MANDATORY_OR_OPTIONAL_DEPENDING_ON_GROUP",
        requiredness=None,
        semantic_key=None,
        interpretation_state="REVIEW_REQUIRED",
    )

    mapping = _map(_metadata(requirement)).mappings[0]

    assert mapping.state is RequirementMappingState.REVIEW_REQUIRED
    assert mapping.reasons[0].code == INTERPRETATION_REVIEW_REQUIRED


@pytest.mark.parametrize(
    ("provider_requiredness", "requiredness"),
    (
        ("MANDATORY_OR_OPTIONAL_DEPENDING_ON_GROUP", "REQUIRED"),
        ("MANDATORY", "OPTIONAL"),
        ("OPTIONAL", "REQUIRED"),
    ),
)
def test_falsely_confirmed_unknown_or_contradictory_requiredness_fails_closed(
    provider_requiredness: str, requiredness: str
) -> None:
    requirement = _requirement(
        provider_requiredness=provider_requiredness,
        requiredness=requiredness,
        interpretation_state="CONFIRMED",
    )

    mapping = _map(_metadata(requirement)).mappings[0]

    assert mapping.state is RequirementMappingState.REVIEW_REQUIRED
    assert mapping.reasons[0].code == INTERPRETATION_REVIEW_REQUIRED


def test_search_and_notice_roles_remain_separate_from_common_options() -> None:
    search = _requirement(provider_rule_key="SEARCH_COUNT", role="SEARCH_ATTRIBUTE")
    notice = _requirement(provider_rule_key="NOTICE_COUNT", role="PRODUCT_INFORMATION_NOTICE")
    before = _options()

    result = map_marketplace_requirements(
        before,
        _metadata(search, notice),
        marketplace_key="coupang",
        taxonomy_revision="taxonomy-2026-10",
        category_id="health",
    )

    assert result.state is RequirementMappingState.NOT_APPLICABLE
    assert all(mapping.target_kind is RequirementTargetKind.NONE for mapping in result.mappings)
    assert all(mapping.reasons[0].code == SEPARATE_ROLE for mapping in result.mappings)
    assert before == _options()


def test_missing_or_unreviewed_metadata_never_infers_a_mapping() -> None:
    missing = map_marketplace_requirements(
        _options(),
        None,
        marketplace_key="coupang",
        taxonomy_revision="taxonomy-2026-10",
        category_id="health",
    )
    unreviewed = _map(replace(_metadata(), reviewed=False))

    assert missing.state is RequirementMappingState.REVIEW_REQUIRED
    assert missing.reasons[0].code == METADATA_MISSING
    assert unreviewed.state is RequirementMappingState.REVIEW_REQUIRED
    assert unreviewed.mappings == ()


def test_unadopted_or_different_scope_metadata_never_maps() -> None:
    unadopted = _map(replace(_metadata(), adopted=False))
    different_marketplace = _map(replace(_metadata(), marketplace_key="smartstore"))

    assert unadopted.state is RequirementMappingState.REVIEW_REQUIRED
    assert unadopted.reasons[0].code == METADATA_UNADOPTED
    assert unadopted.mappings == ()
    assert different_marketplace.state is RequirementMappingState.REVIEW_REQUIRED
    assert different_marketplace.reasons[0].code == METADATA_SCOPE_MISMATCH
    assert different_marketplace.mappings == ()


def test_empty_reviewed_requirements_leave_legacy_no_option_path_not_applicable() -> None:
    result = _map(_metadata(requirements=()))

    assert result.state is RequirementMappingState.NOT_APPLICABLE
    assert result.mappings == ()
