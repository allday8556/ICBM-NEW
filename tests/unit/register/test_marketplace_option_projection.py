"""C-P1 provider-zero option compatibility and canonical read-only rendering preview."""

from dataclasses import fields, replace
from datetime import UTC, datetime

import pytest

from app.stages.products.atomic_sku_store import (
    AtomicSKURecord,
    AtomicSKUSelectionRecord,
    AtomicSKUSetRecord,
)
from app.stages.products.common_option_store import (
    CommonSalesOptionAxisRecord,
    CommonSalesOptionRevisionRecord,
    CommonSalesOptionValueRecord,
)
from app.stages.register.marketplace_option_projection import (
    ATOMIC_SKU_LIMIT_EXCEEDED,
    AXIS_LIMIT_EXCEEDED,
    METADATA_MISSING,
    METADATA_UNADOPTED,
    METADATA_UNREVIEWED,
    OPTIONS_UNSUPPORTED,
    VALUE_LIMIT_EXCEEDED,
    OptionStructureCompatibility,
    ReviewedOptionStructureMetadata,
    preview_marketplace_option_structure,
)

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def common_options() -> CommonSalesOptionRevisionRecord:
    return CommonSalesOptionRevisionRecord(
        revision_id="common-1",
        product_group_id="product-1",
        revision_no=1,
        structure_signature="a" * 64,
        signature_version="common-sales-option-signature/v1",
        reason="reviewed",
        decided_by="owner",
        correlation_id="corr-1",
        created_at=NOW,
        axes=(
            CommonSalesOptionAxisRecord(
                "axis-weight",
                "individual_weight",
                "개별 중량/용량",
                0,
                (
                    CommonSalesOptionValueRecord("weight-300", "300", "300mg", "mg", 0),
                    CommonSalesOptionValueRecord("weight-500", "500", "500mg", "mg", 1),
                ),
            ),
            CommonSalesOptionAxisRecord(
                "axis-count",
                "unit_count",
                "수량",
                1,
                (
                    CommonSalesOptionValueRecord("count-30", "30", "30정", "tablet", 0),
                    CommonSalesOptionValueRecord("count-60", "60", "60정", "tablet", 1),
                ),
            ),
        ),
    )


def selection(axis: str, value: str, semantic: str, canonical: str, ordinal: int):
    return AtomicSKUSelectionRecord(
        selection_id=f"selection-{axis}-{value}",
        axis_id=axis,
        value_id=value,
        semantic_key=semantic,
        canonical_value=canonical,
        unit_code="mg" if axis == "axis-weight" else "tablet",
        source_json_path=f"$.options[{ordinal}]",
        source_value_json=f'"{canonical}"',
        ordinal=ordinal,
    )


def sku(sku_id: str, ordinal: int, weight: str, count: str) -> AtomicSKURecord:
    return AtomicSKURecord(
        atomic_sku_id=sku_id,
        revision_member_id=f"member-{sku_id}",
        selection_signature=str(ordinal) * 64,
        source_revision_id="facts-1",
        source_field_key="options",
        source_configuration_path=f"$.options[{ordinal}]",
        source_configuration_json="{}",
        source_field_fingerprint="f" * 64,
        supplier_sku_id=f"supplier-{sku_id}",
        ordinal=ordinal,
        selections=(
            selection("axis-weight", f"weight-{weight}", "individual_weight", weight, 0),
            selection("axis-count", f"count-{count}", "unit_count", count, 1),
        ),
    )


def atomic_skus() -> AtomicSKUSetRecord:
    return AtomicSKUSetRecord(
        sku_set_revision_id="sku-set-1",
        product_group_id="product-1",
        common_option_revision_id="common-1",
        fact_mapping_revision_id="mapping-1",
        revision_no=1,
        set_signature="s" * 64,
        signature_version="atomic-sku-set-signature/v1",
        reason="source proven",
        decided_by="owner",
        correlation_id="corr-1",
        created_at=NOW,
        atomic_skus=(
            sku("sku-a", 0, "300", "30"),
            sku("sku-b", 1, "500", "60"),
        ),
    )


def metadata(**changes: object) -> ReviewedOptionStructureMetadata:
    base = ReviewedOptionStructureMetadata(
        marketplace_key="coupang",
        category_id="health",
        metadata_revision_id="metadata-1",
        adopted=True,
        reviewed=True,
        options_supported=True,
        max_axes=2,
        max_values_per_axis=2,
        max_atomic_skus=2,
    )
    return replace(base, **changes)


def preview(meta: ReviewedOptionStructureMetadata | None = None):
    return preview_marketplace_option_structure(
        common_options(),
        atomic_skus(),
        metadata() if meta is None else meta,
        marketplace_key="coupang",
        category_id="health",
    )


def test_compatible_means_only_reviewed_structure_range_compatibility() -> None:
    result = preview()

    assert result.compatibility is OptionStructureCompatibility.COMPATIBLE
    assert result.compatibility_scope == "REVIEWED_METADATA_STRUCTURE_RANGE_ONLY"
    assert result.readiness == "NOT_EVALUATED"
    assert result.sendability == "NOT_EVALUATED"
    assert result.provider_verification == "NOT_VERIFIED"
    assert result.reasons == ()
    assert {field.name for field in fields(result)}.isdisjoint(
        {"price", "sale_price", "payload", "provider_rule_key", "provider_option_id"}
    )


def test_preview_renders_only_the_two_source_proven_sparse_atomic_skus() -> None:
    result = preview()

    assert tuple(item.atomic_sku_id for item in result.atomic_skus) == ("sku-a", "sku-b")
    assert tuple(
        tuple(selection.display_value for selection in item.selections)
        for item in result.atomic_skus
    ) == (("300mg", "30정"), ("500mg", "60정"))
    # Axis inventories have four mathematical pairs, but no Cartesian rows are created.
    assert len(result.atomic_skus) == 2
    assert common_options().axes[0].semantic_key == "individual_weight"


def test_absent_unadopted_or_unreviewed_metadata_never_infers_compatibility() -> None:
    absent = preview_marketplace_option_structure(
        common_options(),
        atomic_skus(),
        None,
        marketplace_key="coupang",
        category_id="health",
    )
    unadopted = preview(metadata(adopted=False))
    unreviewed = preview(metadata(reviewed=False))

    assert absent.compatibility is OptionStructureCompatibility.REVIEW_REQUIRED
    assert absent.reasons[0].code == METADATA_MISSING
    assert unadopted.compatibility is OptionStructureCompatibility.REVIEW_REQUIRED
    assert unadopted.reasons[0].code == METADATA_UNADOPTED
    assert unreviewed.compatibility is OptionStructureCompatibility.REVIEW_REQUIRED
    assert unreviewed.reasons[0].code == METADATA_UNREVIEWED
    assert len(absent.atomic_skus) == len(unadopted.atomic_skus) == len(unreviewed.atomic_skus) == 2


def test_metadata_from_another_marketplace_or_category_is_not_reused() -> None:
    wrong_marketplace = preview(metadata(marketplace_key="naver"))
    wrong_category = preview(metadata(category_id="beauty"))

    for result in (wrong_marketplace, wrong_category):
        assert result.compatibility is OptionStructureCompatibility.REVIEW_REQUIRED
        assert tuple((reason.code, reason.subject) for reason in result.reasons) == (
            (METADATA_MISSING, "metadata_scope"),
        )


@pytest.mark.parametrize(
    ("meta", "code"),
    [
        (metadata(options_supported=False), OPTIONS_UNSUPPORTED),
        (metadata(max_axes=1), AXIS_LIMIT_EXCEEDED),
        (metadata(max_values_per_axis=1), VALUE_LIMIT_EXCEEDED),
        (metadata(max_atomic_skus=1), ATOMIC_SKU_LIMIT_EXCEEDED),
    ],
)
def test_documented_structure_limit_conflicts_are_marketplace_local_review(meta, code) -> None:
    result = preview(meta)

    assert result.compatibility is OptionStructureCompatibility.REVIEW_REQUIRED
    assert code in {reason.code for reason in result.reasons}
    assert len(result.axes) == 2 and len(result.atomic_skus) == 2


def test_one_marketplace_conflict_never_mutates_the_shared_canonical_structure() -> None:
    common = common_options()
    skus = atomic_skus()
    before = (common, skus)

    coupang = preview_marketplace_option_structure(
        common,
        skus,
        metadata(max_atomic_skus=1),
        marketplace_key="coupang",
        category_id="health",
    )
    naver = preview_marketplace_option_structure(
        common,
        skus,
        replace(
            metadata(),
            marketplace_key="naver",
            metadata_revision_id="naver-metadata-1",
        ),
        marketplace_key="naver",
        category_id="health",
    )

    assert coupang.compatibility is OptionStructureCompatibility.REVIEW_REQUIRED
    assert naver.compatibility is OptionStructureCompatibility.COMPATIBLE
    assert (common, skus) == before
    assert coupang.atomic_skus == naver.atomic_skus


def test_metadata_ranges_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="max_atomic_skus"):
        metadata(max_atomic_skus=-1)
