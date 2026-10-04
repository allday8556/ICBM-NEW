"""Provider-neutral Common Sales Option structure compatibility and read-only preview.

C-P1 consumes the immutable Product DB Common Sales Option revision and its source-proven
AtomicSKU set.  It never authors either owner, creates combinations, evaluates price/readiness,
builds a provider payload or reaches a marketplace.  ``COMPATIBLE`` means only that reviewed
metadata proves the existing structure fits the documented count ranges.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.stages.products.atomic_sku_store import AtomicSKUSetRecord
from app.stages.products.common_option_store import CommonSalesOptionRevisionRecord

OPTION_STRUCTURE_PREVIEW_VERSION: Final = "marketplace-option-structure-preview/v1"
STRUCTURE_RANGE_ONLY: Final = "REVIEWED_METADATA_STRUCTURE_RANGE_ONLY"
NOT_EVALUATED: Final = "NOT_EVALUATED"
NOT_VERIFIED: Final = "NOT_VERIFIED"

METADATA_MISSING: Final = "OPTION_STRUCTURE_METADATA_MISSING"
METADATA_UNREVIEWED: Final = "OPTION_STRUCTURE_METADATA_UNREVIEWED"
OPTIONS_UNSUPPORTED: Final = "OPTION_STRUCTURE_OPTIONS_UNSUPPORTED"
AXIS_LIMIT_EXCEEDED: Final = "OPTION_STRUCTURE_AXIS_LIMIT_EXCEEDED"
VALUE_LIMIT_EXCEEDED: Final = "OPTION_STRUCTURE_VALUE_LIMIT_EXCEEDED"
ATOMIC_SKU_LIMIT_EXCEEDED: Final = "OPTION_STRUCTURE_ATOMIC_SKU_LIMIT_EXCEEDED"
PRODUCT_MISMATCH: Final = "OPTION_STRUCTURE_PRODUCT_MISMATCH"
REVISION_MISMATCH: Final = "OPTION_STRUCTURE_REVISION_MISMATCH"
ATOMIC_SKU_SELECTION_INVALID: Final = "OPTION_STRUCTURE_ATOMIC_SKU_SELECTION_INVALID"


class OptionStructureCompatibility(StrEnum):
    """A marketplace-local structural answer, never registration readiness."""

    COMPATIBLE = "COMPATIBLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class ReviewedOptionStructureMetadata:
    """The adopted/reviewed structural range for one marketplace category.

    Requirement semantics and provider wire rendering are deliberately absent.  Later slices own
    requirement mapping and provider projection; C-P1 may only compare documented count ranges.
    """

    marketplace_key: str
    category_id: str
    metadata_revision_id: str
    reviewed: bool
    options_supported: bool
    max_axes: int
    max_values_per_axis: int
    max_atomic_skus: int

    def __post_init__(self) -> None:
        for name in ("marketplace_key", "category_id", "metadata_revision_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be present")
        for name in ("max_axes", "max_values_per_axis", "max_atomic_skus"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True)
class OptionCompatibilityReason:
    code: str
    subject: str


@dataclass(frozen=True)
class OptionValuePreview:
    value_id: str
    canonical_value: str
    display_value: str
    unit_code: str | None
    ordinal: int


@dataclass(frozen=True)
class OptionAxisPreview:
    axis_id: str
    semantic_key: str
    display_name: str
    ordinal: int
    values: tuple[OptionValuePreview, ...]


@dataclass(frozen=True)
class AtomicSKUSelectionPreview:
    axis_id: str
    semantic_key: str
    axis_display_name: str
    value_id: str
    canonical_value: str
    display_value: str
    unit_code: str | None
    ordinal: int


@dataclass(frozen=True)
class AtomicSKUPreview:
    atomic_sku_id: str
    ordinal: int
    selections: tuple[AtomicSKUSelectionPreview, ...]


@dataclass(frozen=True)
class MarketplaceOptionStructurePreview:
    preview_version: str
    marketplace_key: str
    category_id: str
    metadata_revision_id: str | None
    product_group_id: str
    common_option_revision_id: str
    atomic_sku_set_revision_id: str
    compatibility: OptionStructureCompatibility
    compatibility_scope: str
    readiness: str
    sendability: str
    provider_verification: str
    reasons: tuple[OptionCompatibilityReason, ...]
    axes: tuple[OptionAxisPreview, ...]
    atomic_skus: tuple[AtomicSKUPreview, ...]


def preview_marketplace_option_structure(
    common_options: CommonSalesOptionRevisionRecord,
    atomic_skus: AtomicSKUSetRecord,
    metadata: ReviewedOptionStructureMetadata | None,
    *,
    marketplace_key: str,
    category_id: str,
) -> MarketplaceOptionStructurePreview:
    """Compare one canonical structure with reviewed limits and render its existing rows.

    The authored order is preserved.  The implementation iterates only the persisted AtomicSKUs;
    it never derives the Cartesian product of axis values.
    """

    requested_marketplace = marketplace_key.strip()
    requested_category = category_id.strip()
    if not requested_marketplace or not requested_category:
        raise ValueError("marketplace_key and category_id must be present")

    axes = tuple(
        OptionAxisPreview(
            axis_id=axis.axis_id,
            semantic_key=axis.semantic_key,
            display_name=axis.display_name,
            ordinal=axis.ordinal,
            values=tuple(
                OptionValuePreview(
                    value_id=value.value_id,
                    canonical_value=value.canonical_value,
                    display_value=value.display_value,
                    unit_code=value.unit_code,
                    ordinal=value.ordinal,
                )
                for value in axis.values
            ),
        )
        for axis in common_options.axes
    )
    axis_records = {axis.axis_id: axis for axis in common_options.axes}
    values = {
        (axis.axis_id, value.value_id): value
        for axis in common_options.axes
        for value in axis.values
    }
    reasons: list[OptionCompatibilityReason] = []

    if common_options.product_group_id != atomic_skus.product_group_id:
        reasons.append(OptionCompatibilityReason(PRODUCT_MISMATCH, "product_group_id"))
    if common_options.revision_id != atomic_skus.common_option_revision_id:
        reasons.append(OptionCompatibilityReason(REVISION_MISMATCH, "common_option_revision_id"))

    rendered_skus: list[AtomicSKUPreview] = []
    expected_axes = frozenset(axis_records)
    for sku in atomic_skus.atomic_skus:
        selected_axes = [selection.axis_id for selection in sku.selections]
        if (
            len(selected_axes) != len(set(selected_axes))
            or frozenset(selected_axes) != expected_axes
        ):
            reasons.append(
                OptionCompatibilityReason(ATOMIC_SKU_SELECTION_INVALID, sku.atomic_sku_id)
            )
        rendered_selections: list[AtomicSKUSelectionPreview] = []
        for selection in sku.selections:
            axis = axis_records.get(selection.axis_id)
            value = values.get((selection.axis_id, selection.value_id))
            if axis is None or value is None:
                reasons.append(
                    OptionCompatibilityReason(ATOMIC_SKU_SELECTION_INVALID, sku.atomic_sku_id)
                )
                continue
            rendered_selections.append(
                AtomicSKUSelectionPreview(
                    axis_id=axis.axis_id,
                    semantic_key=axis.semantic_key,
                    axis_display_name=axis.display_name,
                    value_id=value.value_id,
                    canonical_value=value.canonical_value,
                    display_value=value.display_value,
                    unit_code=value.unit_code,
                    ordinal=selection.ordinal,
                )
            )
        rendered_skus.append(
            AtomicSKUPreview(sku.atomic_sku_id, sku.ordinal, tuple(rendered_selections))
        )

    if metadata is None:
        reasons.append(OptionCompatibilityReason(METADATA_MISSING, "metadata"))
        metadata_revision_id = None
    else:
        metadata_revision_id = metadata.metadata_revision_id
        if (
            metadata.marketplace_key != requested_marketplace
            or metadata.category_id != requested_category
        ):
            reasons.append(OptionCompatibilityReason(METADATA_MISSING, "metadata_scope"))
        elif not metadata.reviewed:
            reasons.append(OptionCompatibilityReason(METADATA_UNREVIEWED, "metadata"))
        else:
            if not metadata.options_supported:
                reasons.append(OptionCompatibilityReason(OPTIONS_UNSUPPORTED, "options"))
            if len(axes) > metadata.max_axes:
                reasons.append(OptionCompatibilityReason(AXIS_LIMIT_EXCEEDED, "axes"))
            for preview_axis in axes:
                if len(preview_axis.values) > metadata.max_values_per_axis:
                    reasons.append(
                        OptionCompatibilityReason(VALUE_LIMIT_EXCEEDED, preview_axis.semantic_key)
                    )
            if len(rendered_skus) > metadata.max_atomic_skus:
                reasons.append(OptionCompatibilityReason(ATOMIC_SKU_LIMIT_EXCEEDED, "atomic_skus"))

    compatibility = (
        OptionStructureCompatibility.COMPATIBLE
        if not reasons
        else OptionStructureCompatibility.REVIEW_REQUIRED
    )
    return MarketplaceOptionStructurePreview(
        preview_version=OPTION_STRUCTURE_PREVIEW_VERSION,
        marketplace_key=requested_marketplace,
        category_id=requested_category,
        metadata_revision_id=metadata_revision_id,
        product_group_id=common_options.product_group_id,
        common_option_revision_id=common_options.revision_id,
        atomic_sku_set_revision_id=atomic_skus.sku_set_revision_id,
        compatibility=compatibility,
        compatibility_scope=STRUCTURE_RANGE_ONLY,
        readiness=NOT_EVALUATED,
        sendability=NOT_EVALUATED,
        provider_verification=NOT_VERIFIED,
        reasons=tuple(dict.fromkeys(reasons)),
        axes=axes,
        atomic_skus=tuple(rendered_skus),
    )


__all__ = [
    "ATOMIC_SKU_LIMIT_EXCEEDED",
    "AXIS_LIMIT_EXCEEDED",
    "METADATA_MISSING",
    "METADATA_UNREVIEWED",
    "OPTIONS_UNSUPPORTED",
    "OPTION_STRUCTURE_PREVIEW_VERSION",
    "VALUE_LIMIT_EXCEEDED",
    "MarketplaceOptionStructurePreview",
    "OptionStructureCompatibility",
    "ReviewedOptionStructureMetadata",
    "preview_marketplace_option_structure",
]
