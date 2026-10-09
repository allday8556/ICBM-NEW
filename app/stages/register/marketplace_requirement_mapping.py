"""Provider-zero marketplace requirement metadata mapping (Track C, C-P3).

This module reads an adopted/reviewed marketplace category revision and compares its explicit
provider-neutral semantics with an existing Common Sales Option revision.  Provider labels are
display evidence only and never participate in matching.  The function is pure and read-only: it
does not author Product Facts, Common Sales Options, AtomicSKUs or provider payloads.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.stages.products.common_option_store import (
    CommonSalesOptionAxisRecord,
    CommonSalesOptionRevisionRecord,
)
from app.stages.register.category_metadata import (
    CONFIRMED_PROVIDER_REQUIREDNESS,
    MarketplaceOptionRequirementView,
    MetadataRecord,
    content_of,
)

REQUIREMENT_MAPPING_VERSION: Final = "marketplace-requirement-mapping/v1"
LISTING_COMPOSITION_QUANTITY: Final = "listing_composition.quantity"

METADATA_MISSING: Final = "MARKETPLACE_REQUIREMENT_METADATA_MISSING"
METADATA_UNADOPTED: Final = "MARKETPLACE_REQUIREMENT_METADATA_UNADOPTED"
METADATA_UNREVIEWED: Final = "MARKETPLACE_REQUIREMENT_METADATA_UNREVIEWED"
METADATA_SCOPE_MISMATCH: Final = "MARKETPLACE_REQUIREMENT_METADATA_SCOPE_MISMATCH"
INTERPRETATION_REVIEW_REQUIRED: Final = "REQUIREMENT_INTERPRETATION_REVIEW_REQUIRED"
COMMON_AXIS_MISSING: Final = "REQUIREMENT_COMMON_AXIS_MISSING"
UNIT_SEMANTICS_MISMATCH: Final = "REQUIREMENT_UNIT_SEMANTICS_MISMATCH"
VALUE_SEMANTICS_MISMATCH: Final = "REQUIREMENT_VALUE_SEMANTICS_MISMATCH"
SELLING_QUANTITY_CONTRACT_MISMATCH: Final = "SELLING_QUANTITY_CONTRACT_MISMATCH"
SEPARATE_ROLE: Final = "REQUIREMENT_ROLE_SEPARATE_FROM_PURCHASE_OPTION"


class RequirementMappingState(StrEnum):
    MAPPED = "MAPPED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RequirementTargetKind(StrEnum):
    COMMON_SALES_OPTION_AXIS = "COMMON_SALES_OPTION_AXIS"
    LISTING_COMPOSITION_QUANTITY = "LISTING_COMPOSITION_QUANTITY"
    NONE = "NONE"


@dataclass(frozen=True)
class ReviewedMarketplaceRequirementMetadata:
    marketplace_key: str
    taxonomy_revision: str
    category_id: str
    metadata_revision_id: str
    adopted: bool
    reviewed: bool
    requirements: tuple[MarketplaceOptionRequirementView, ...]


@dataclass(frozen=True)
class RequirementMappingReason:
    code: str
    subject: str


@dataclass(frozen=True)
class MarketplaceRequirementMapping:
    provider_rule_key: str
    provider_label: str
    role: str
    requiredness: str | None
    provider_semantics: str
    semantic_key: str | None
    value_semantics: str
    basic_unit: str | None
    usable_units: tuple[str, ...]
    state: RequirementMappingState
    target_kind: RequirementTargetKind
    target_reference: str | None
    reasons: tuple[RequirementMappingReason, ...]


@dataclass(frozen=True)
class MarketplaceRequirementMappingResult:
    mapping_version: str
    marketplace_key: str
    taxonomy_revision: str
    category_id: str
    metadata_revision_id: str | None
    product_group_id: str
    common_option_revision_id: str
    state: RequirementMappingState
    reasons: tuple[RequirementMappingReason, ...]
    mappings: tuple[MarketplaceRequirementMapping, ...]


def adopted_requirement_metadata(
    record: MetadataRecord,
) -> ReviewedMarketplaceRequirementMetadata | None:
    """Read the adopted current revision without allowing caller-supplied scope rebinding."""

    current = record.current
    if current is None:
        return None

    return ReviewedMarketplaceRequirementMetadata(
        marketplace_key=record.marketplace_key,
        taxonomy_revision=record.taxonomy_revision,
        category_id=record.category_id,
        metadata_revision_id=current.metadata_revision,
        adopted=True,
        reviewed=current.reviewed,
        requirements=tuple(content_of(current.content).option_requirements),
    )


def map_marketplace_requirements(
    common_options: CommonSalesOptionRevisionRecord,
    metadata: ReviewedMarketplaceRequirementMetadata | None,
    *,
    marketplace_key: str,
    taxonomy_revision: str,
    category_id: str,
) -> MarketplaceRequirementMappingResult:
    """Map only explicit semantics; never labels, defaults or inferred option combinations."""

    requested = (marketplace_key.strip(), taxonomy_revision.strip(), category_id.strip())
    if not all(requested):
        raise ValueError("marketplace_key, taxonomy_revision and category_id must be present")

    top_reasons: list[RequirementMappingReason] = []
    mappings: list[MarketplaceRequirementMapping] = []
    metadata_revision_id: str | None = None
    if metadata is None:
        top_reasons.append(RequirementMappingReason(METADATA_MISSING, "metadata"))
    else:
        metadata_revision_id = metadata.metadata_revision_id
        if (
            metadata.marketplace_key,
            metadata.taxonomy_revision,
            metadata.category_id,
        ) != requested:
            top_reasons.append(RequirementMappingReason(METADATA_SCOPE_MISMATCH, "metadata"))
        elif not metadata.adopted:
            top_reasons.append(RequirementMappingReason(METADATA_UNADOPTED, "metadata"))
        elif not metadata.reviewed:
            top_reasons.append(RequirementMappingReason(METADATA_UNREVIEWED, "metadata"))
        else:
            axes = {axis.semantic_key: axis for axis in common_options.axes}
            mappings.extend(
                _map_requirement(requirement, axes) for requirement in metadata.requirements
            )

    if top_reasons or any(
        mapping.state is RequirementMappingState.REVIEW_REQUIRED for mapping in mappings
    ):
        state = RequirementMappingState.REVIEW_REQUIRED
    elif any(mapping.state is RequirementMappingState.MAPPED for mapping in mappings):
        state = RequirementMappingState.MAPPED
    else:
        state = RequirementMappingState.NOT_APPLICABLE

    return MarketplaceRequirementMappingResult(
        mapping_version=REQUIREMENT_MAPPING_VERSION,
        marketplace_key=requested[0],
        taxonomy_revision=requested[1],
        category_id=requested[2],
        metadata_revision_id=metadata_revision_id,
        product_group_id=common_options.product_group_id,
        common_option_revision_id=common_options.revision_id,
        state=state,
        reasons=tuple(top_reasons),
        mappings=tuple(mappings),
    )


def _map_requirement(
    requirement: MarketplaceOptionRequirementView,
    axes: dict[str, CommonSalesOptionAxisRecord],
) -> MarketplaceRequirementMapping:
    reasons: list[RequirementMappingReason] = []
    target_kind = RequirementTargetKind.NONE
    target_reference: str | None = None

    if requirement.role != "PURCHASE_OPTION":
        return _mapping(
            requirement,
            RequirementMappingState.NOT_APPLICABLE,
            target_kind,
            target_reference,
            (RequirementMappingReason(SEPARATE_ROLE, requirement.role),),
        )

    if (
        requirement.interpretation_state != "CONFIRMED"
        or requirement.requiredness is None
        or requirement.semantic_key is None
        or requirement.requiredness
        != CONFIRMED_PROVIDER_REQUIREDNESS.get(requirement.provider_requiredness)
    ):
        reasons.append(
            RequirementMappingReason(INTERPRETATION_REVIEW_REQUIRED, requirement.provider_rule_key)
        )
    elif requirement.value_semantics == "SELLING_BUNDLE_QUANTITY":
        if (
            requirement.semantic_key != "selling_bundle_quantity"
            or requirement.basic_unit != "count"
            or "count" not in requirement.usable_units
        ):
            reasons.append(
                RequirementMappingReason(
                    SELLING_QUANTITY_CONTRACT_MISMATCH, requirement.provider_rule_key
                )
            )
        else:
            target_kind = RequirementTargetKind.LISTING_COMPOSITION_QUANTITY
            target_reference = LISTING_COMPOSITION_QUANTITY
    else:
        axis = axes.get(requirement.semantic_key)
        if axis is None:
            reasons.append(RequirementMappingReason(COMMON_AXIS_MISSING, requirement.semantic_key))
        else:
            target_kind = RequirementTargetKind.COMMON_SALES_OPTION_AXIS
            target_reference = axis.axis_id
            _validate_axis_semantics(requirement, axis, reasons)

    return _mapping(
        requirement,
        RequirementMappingState.REVIEW_REQUIRED if reasons else RequirementMappingState.MAPPED,
        target_kind if not reasons else RequirementTargetKind.NONE,
        target_reference if not reasons else None,
        tuple(reasons),
    )


def _validate_axis_semantics(
    requirement: MarketplaceOptionRequirementView,
    axis: CommonSalesOptionAxisRecord,
    reasons: list[RequirementMappingReason],
) -> None:
    units = {value.unit_code for value in axis.values}
    if requirement.value_semantics in {"MEASURE", "COUNT_PER_UNIT"}:
        if (
            requirement.basic_unit is None
            or requirement.basic_unit not in requirement.usable_units
            or units != {requirement.basic_unit}
        ):
            reasons.append(RequirementMappingReason(UNIT_SEMANTICS_MISMATCH, axis.semantic_key))
    elif units != {None}:
        reasons.append(RequirementMappingReason(UNIT_SEMANTICS_MISMATCH, axis.semantic_key))

    if requirement.allowed_values and requirement.value_semantics in {"ENUM", "TEXT"}:
        canonical_values = {value.canonical_value for value in axis.values}
        if not canonical_values <= set(requirement.allowed_values):
            reasons.append(RequirementMappingReason(VALUE_SEMANTICS_MISMATCH, axis.semantic_key))


def _mapping(
    requirement: MarketplaceOptionRequirementView,
    state: RequirementMappingState,
    target_kind: RequirementTargetKind,
    target_reference: str | None,
    reasons: tuple[RequirementMappingReason, ...],
) -> MarketplaceRequirementMapping:
    return MarketplaceRequirementMapping(
        provider_rule_key=requirement.provider_rule_key,
        provider_label=requirement.provider_label,
        role=requirement.role,
        requiredness=requirement.requiredness,
        provider_semantics=requirement.provider_semantics,
        semantic_key=requirement.semantic_key,
        value_semantics=requirement.value_semantics,
        basic_unit=requirement.basic_unit,
        usable_units=tuple(requirement.usable_units),
        state=state,
        target_kind=target_kind,
        target_reference=target_reference,
        reasons=reasons,
    )


__all__ = [
    "COMMON_AXIS_MISSING",
    "INTERPRETATION_REVIEW_REQUIRED",
    "LISTING_COMPOSITION_QUANTITY",
    "METADATA_MISSING",
    "METADATA_SCOPE_MISMATCH",
    "METADATA_UNADOPTED",
    "METADATA_UNREVIEWED",
    "REQUIREMENT_MAPPING_VERSION",
    "SELLING_QUANTITY_CONTRACT_MISMATCH",
    "SEPARATE_ROLE",
    "UNIT_SEMANTICS_MISMATCH",
    "VALUE_SEMANTICS_MISMATCH",
    "RequirementMappingState",
    "RequirementTargetKind",
    "ReviewedMarketplaceRequirementMetadata",
    "adopted_requirement_metadata",
    "map_marketplace_requirements",
]
