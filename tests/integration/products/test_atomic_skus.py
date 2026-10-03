"""Source-proven Atomic SKUs; inventories never create Cartesian combinations."""

import contextlib
import sqlite3
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.errors import InputValidationError
from app.stages.collect.facts import FieldFact, OptionAxis, OptionConfiguration, OptionsValue
from app.stages.products.atomic_sku import (
    ATOMIC_SKU_SIGNATURE_VERSION,
    AtomicSKUConfigurationSpec,
    AtomicSKUSelectionSpec,
)
from app.stages.products.common_option_mapping import (
    CommonOptionAxisFactMappingSpec,
    CommonOptionValueFactMappingSpec,
    ProductFactPointer,
)
from app.stages.products.common_option_mapping_store import CommonOptionFactMappingRecord
from app.stages.products.common_option_store import CommonSalesOptionRevisionRecord
from app.stages.products.common_options import (
    CommonSalesOptionAxisSpec,
    CommonSalesOptionValueSpec,
)
from app.stages.products.model import CompositionSpec, MoveReason
from app.stages.register.model import registration_item_key_v2
from tests.support.collect_support import base_fields, collected, confirmed

pytestmark = pytest.mark.integration


def _raw(config: AppConfig) -> sqlite3.Connection:
    raw = sqlite3.connect(database_path(config.data_dir))
    raw.execute("PRAGMA foreign_keys=ON")
    return raw


def _raw_atomic_item_insert(
    raw: sqlite3.Connection,
    group: str,
    composition_id: str,
    composition_signature: str,
    atomic_sku_id: str,
    selection_signature: str,
) -> None:
    raw.execute(
        "INSERT INTO atomic_sku_product_items VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            group,
            composition_id,
            composition_signature,
            atomic_sku_id,
            selection_signature,
            "2026-10-04 00:00:00",
        ),
    )


def _option_fields(
    configurations: tuple[OptionConfiguration, ...] = (
        OptionConfiguration(selections=("300mg", "30정"), supplier_sku_id="sku-a"),
        OptionConfiguration(selections=("500mg", "60정"), supplier_sku_id="sku-b"),
    ),
) -> dict[str, FieldFact]:
    fields = base_fields()
    fields["options"] = confirmed(
        OptionsValue(
            axes=(
                OptionAxis(name="개별 중량/용량", values=("300mg", "500mg")),
                OptionAxis(name="수량", values=("30정", "60정")),
            ),
            configurations=configurations,
        ),
        ".options",
    )
    return fields


def _foundation(
    container: Container,
    configurations: tuple[OptionConfiguration, ...] | None = None,
):  # type: ignore[no-untyped-def]
    fields = _option_fields() if configurations is None else _option_fields(configurations)
    revision = container.revisions.append(collected(fields=fields, images=()))
    source = container.product_store.source_product("kmretail", "1234")
    container.product_store.record_move(
        source.source_product_uid,
        revision.revision_id,
        reason=MoveReason.INITIAL,
        decided_by="test",
        correlation_id="source",
    )
    group = container.product_store.create_group(decided_by="test")
    container.product_store.confirm_new_member(
        group,
        source.source_product_uid,
        reason="reviewed",
        decided_by="owner",
        correlation_id="member",
    )
    common = container.common_sales_options.record_reviewed_revision(
        group,
        (
            CommonSalesOptionAxisSpec(
                "per_unit_weight",
                "개별 중량/용량",
                (
                    CommonSalesOptionValueSpec("300", "300mg", "mg"),
                    CommonSalesOptionValueSpec("500", "500mg", "mg"),
                ),
            ),
            CommonSalesOptionAxisSpec(
                "quantity",
                "수량",
                (
                    CommonSalesOptionValueSpec("30", "30정", "tablet"),
                    CommonSalesOptionValueSpec("60", "60정", "tablet"),
                ),
            ),
        ),
        reason="reviewed",
        decided_by="owner",
        correlation_id="options",
    )
    mapping_specs = tuple(
        CommonOptionAxisFactMappingSpec(
            axis.axis_id,
            ProductFactPointer(revision.revision_id, "options", f"$.axes[{axis.ordinal}].name"),
            tuple(
                CommonOptionValueFactMappingSpec(
                    value.value_id,
                    ProductFactPointer(
                        revision.revision_id,
                        "options",
                        f"$.axes[{axis.ordinal}].values[{value.ordinal}]",
                    ),
                )
                for value in axis.values
            ),
        )
        for axis in common.axes
    )
    container.common_option_fact_mappings.record_reviewed_mapping(
        group,
        common.revision_id,
        mapping_specs,
        evidence_reference="review",
        reason="exact correspondence",
        reviewed_by="owner",
        correlation_id="mapping",
    )
    return group, revision.revision_id, common


def _record_mapping_for_common(
    container: Container,
    group: str,
    common: CommonSalesOptionRevisionRecord,
    revision_id: str,
) -> CommonOptionFactMappingRecord:
    mapping_specs = tuple(
        CommonOptionAxisFactMappingSpec(
            axis.axis_id,
            ProductFactPointer(revision_id, "options", f"$.axes[{axis.ordinal}].name"),
            tuple(
                CommonOptionValueFactMappingSpec(
                    value.value_id,
                    ProductFactPointer(
                        revision_id,
                        "options",
                        f"$.axes[{axis.ordinal}].values[{value.ordinal}]",
                    ),
                )
                for value in axis.values
            ),
        )
        for axis in common.axes
    )
    return container.common_option_fact_mappings.record_reviewed_mapping(
        group,
        common.revision_id,
        mapping_specs,
        evidence_reference="review",
        reason="exact correspondence",
        reviewed_by="owner",
        correlation_id="mapping-currentness",
    )


def _configuration(common, revision_id: str, configuration_index: int, displays: tuple[str, str]):  # type: ignore[no-untyped-def]
    selections = []
    for source_index, (axis, display) in enumerate(zip(common.axes, displays, strict=True)):
        value = next(value for value in axis.values if value.display_value == display)
        selections.append(
            AtomicSKUSelectionSpec(
                axis.axis_id,
                value.value_id,
                f"$.configurations[{configuration_index}].selections[{source_index}]",
            )
        )
    return AtomicSKUConfigurationSpec(
        revision_id,
        "options",
        f"$.configurations[{configuration_index}]",
        tuple(selections),
    )


def _record_options_and_mapping(
    container: Container,
    group: str,
    revision_id: str,
    axes: tuple[CommonSalesOptionAxisSpec, ...],
) -> CommonSalesOptionRevisionRecord:
    common = container.common_sales_options.record_reviewed_revision(
        group,
        axes,
        reason="reviewed revision",
        decided_by="owner",
        correlation_id="options-revision",
    )
    source_axis = {"per_unit_weight": 0, "quantity": 1}
    source_value = {
        "per_unit_weight": {"300": 0, "0.3": 0, "500": 1},
        "quantity": {"30": 0, "60": 1},
    }
    mapping_specs = tuple(
        CommonOptionAxisFactMappingSpec(
            axis.axis_id,
            ProductFactPointer(
                revision_id, "options", f"$.axes[{source_axis[axis.semantic_key]}].name"
            ),
            tuple(
                CommonOptionValueFactMappingSpec(
                    value.value_id,
                    ProductFactPointer(
                        revision_id,
                        "options",
                        f"$.axes[{source_axis[axis.semantic_key]}].values["
                        f"{source_value[axis.semantic_key][value.canonical_value]}]",
                    ),
                )
                for value in axis.values
            ),
        )
        for axis in common.axes
    )
    container.common_option_fact_mappings.record_reviewed_mapping(
        group,
        common.revision_id,
        mapping_specs,
        evidence_reference="review",
        reason="exact correspondence",
        reviewed_by="owner",
        correlation_id="mapping-revision",
    )
    return common


def _semantic_configuration(
    common: CommonSalesOptionRevisionRecord, revision_id: str
) -> AtomicSKUConfigurationSpec:
    source_axis = {"per_unit_weight": 0, "quantity": 1}
    selections = []
    for axis in common.axes:
        source_index = source_axis[axis.semantic_key]
        selected = next(
            value
            for value in axis.values
            if (axis.semantic_key == "per_unit_weight" and value.canonical_value in {"300", "0.3"})
            or (axis.semantic_key == "quantity" and value.canonical_value == "30")
        )
        selections.append(
            AtomicSKUSelectionSpec(
                axis.axis_id,
                selected.value_id,
                f"$.configurations[0].selections[{source_index}]",
            )
        )
    return AtomicSKUConfigurationSpec(
        revision_id,
        "options",
        "$.configurations[0]",
        tuple(selections),
    )


def test_materializes_only_observed_configurations_not_the_cartesian_product(
    container: Container,
) -> None:
    group, revision, common = _foundation(container)
    specs = (
        _configuration(common, revision, 0, ("300mg", "30정")),
        _configuration(common, revision, 1, ("500mg", "60정")),
    )
    recorded = container.atomic_skus.record_source_proven_set(
        group,
        specs,
        reason="source configurations reviewed",
        decided_by="owner",
        correlation_id="atomic",
    )
    same = container.atomic_skus.record_source_proven_set(
        group,
        specs,
        reason="repeat",
        decided_by="owner",
        correlation_id="repeat",
    )

    assert same == recorded
    assert recorded.signature_version == ATOMIC_SKU_SIGNATURE_VERSION
    assert len(recorded.atomic_skus) == 2
    assert [
        [item.source_value_json for item in sku.selections] for sku in recorded.atomic_skus
    ] == [
        ['"300mg"', '"30정"'],
        ['"500mg"', '"60정"'],
    ]
    assert [sku.supplier_sku_id for sku in recorded.atomic_skus] == ["sku-a", "sku-b"]
    revised = container.atomic_skus.record_source_proven_set(
        group,
        specs[:1],
        reason="reviewed removal",
        decided_by="owner",
        correlation_id="atomic-2",
    )
    assert revised.revision_no == 2
    assert revised.atomic_skus[0].atomic_sku_id == recorded.atomic_skus[0].atomic_sku_id
    assert revised.atomic_skus[0].revision_member_id != recorded.atomic_skus[0].revision_member_id
    assert container.atomic_skus.current(group) == revised


@pytest.mark.parametrize("supplier_sku_id", ["forged-sku", "", None])
def test_member_supplier_sku_id_is_exact_source_evidence(
    config: AppConfig,
    container: Container,
    supplier_sku_id: str | None,
) -> None:
    group, revision, common = _foundation(container)
    recorded = container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(common, revision, 0, ("300mg", "30정")),),
        reason="reviewed",
        decided_by="owner",
        correlation_id="atomic",
    )
    member = recorded.atomic_skus[0]

    with (
        contextlib.closing(_raw(config)) as raw,
        pytest.raises(
            sqlite3.IntegrityError,
            match="source configuration is not exact current evidence",
        ),
    ):
        raw.execute(
            "INSERT INTO atomic_sku_revision_members ("
            "revision_member_id, sku_set_revision_id, atomic_sku_id, source_revision_id, "
            "source_field_key, source_configuration_path, source_configuration_json, "
            "source_field_fingerprint, supplier_sku_id, ordinal) "
            "SELECT '00000000-0000-0000-0000-000000000001', sku_set_revision_id, "
            "atomic_sku_id, source_revision_id, source_field_key, source_configuration_path, "
            "source_configuration_json, source_field_fingerprint, ?, ordinal + 1 "
            "FROM atomic_sku_revision_members WHERE revision_member_id = ?",
            (supplier_sku_id, member.revision_member_id),
        )


def test_member_allows_missing_supplier_sku_when_source_evidence_has_none(
    container: Container,
) -> None:
    group, revision, common = _foundation(
        container,
        (
            OptionConfiguration(
                selections=("300mg", "30정"),
                supplier_sku_id=None,
            ),
        ),
    )
    recorded = container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(common, revision, 0, ("300mg", "30정")),),
        reason="reviewed",
        decided_by="owner",
        correlation_id="atomic-without-supplier-sku",
    )

    assert recorded.atomic_skus[0].supplier_sku_id is None


@pytest.mark.parametrize(
    ("source_json_path", "source_value_json"),
    [
        ("$.axes[0].values[0]", '"300mg"'),
        ("$.configurations[0].selections[1]", '"30정"'),
    ],
)
def test_selection_evidence_is_inside_the_configuration_and_matches_the_reviewed_value(
    config: AppConfig,
    container: Container,
    source_json_path: str,
    source_value_json: str,
) -> None:
    group, revision, common = _foundation(container)
    recorded = container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(common, revision, 0, ("300mg", "30정")),),
        reason="reviewed",
        decided_by="owner",
        correlation_id="atomic",
    )
    member = recorded.atomic_skus[0]

    with (
        contextlib.closing(_raw(config)) as raw,
        pytest.raises(sqlite3.IntegrityError, match="selection evidence is not exact"),
    ):
        raw.execute(
            "INSERT INTO atomic_sku_revision_selection_evidence ("
            "evidence_id, revision_member_id, atomic_sku_selection_id, "
            "common_option_axis_id, common_option_value_id, source_revision_id, "
            "source_field_key, source_json_path, source_value_json, source_field_fingerprint) "
            "SELECT '00000000-0000-0000-0000-000000000002', e.revision_member_id, "
            "e.atomic_sku_selection_id, e.common_option_axis_id, e.common_option_value_id, "
            "e.source_revision_id, e.source_field_key, ?, ?, e.source_field_fingerprint "
            "FROM atomic_sku_revision_selection_evidence e "
            "JOIN atomic_sku_selections s ON s.selection_id = e.atomic_sku_selection_id "
            "WHERE e.revision_member_id = ? AND s.semantic_key = 'per_unit_weight'",
            (source_json_path, source_value_json, member.revision_member_id),
        )


def test_rejects_an_unobserved_cross_combination(container: Container) -> None:
    group, revision, common = _foundation(container)
    fabricated = _configuration(common, revision, 0, ("300mg", "60정"))
    with pytest.raises(InputValidationError, match="exactly match"):
        container.atomic_skus.record_source_proven_set(
            group,
            (fabricated,),
            reason="must refuse",
            decided_by="owner",
            correlation_id="fabricated",
        )
    assert container.atomic_skus.current(group) is None


def test_atomic_sku_rows_are_append_only(config: AppConfig, container: Container) -> None:
    group, revision, common = _foundation(container)
    container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(common, revision, 0, ("300mg", "30정")),),
        reason="reviewed",
        decided_by="owner",
        correlation_id="atomic",
    )
    with contextlib.closing(_raw(config)) as raw:
        for statement in (
            "UPDATE atomic_sku_set_revisions SET reason = 'other'",
            "DELETE FROM atomic_skus",
            "UPDATE atomic_sku_selections SET ordinal = 9",
            "DELETE FROM current_atomic_sku_set_moves",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(statement)


def test_atomic_sku_identity_survives_non_semantic_option_revisions(
    container: Container,
) -> None:
    group, revision, _ = _foundation(container)
    variants = (
        (
            CommonSalesOptionAxisSpec(
                "per_unit_weight",
                "개별 중량/용량",
                (CommonSalesOptionValueSpec("300", "300mg", "mg"),),
            ),
            CommonSalesOptionAxisSpec(
                "quantity", "수량", (CommonSalesOptionValueSpec("30", "30정", "tablet"),)
            ),
        ),
        (
            CommonSalesOptionAxisSpec(
                "per_unit_weight",
                "1개당 내용량",
                (CommonSalesOptionValueSpec("300", "0.3 g", "mg"),),
            ),
            CommonSalesOptionAxisSpec(
                "quantity", "포장 수량", (CommonSalesOptionValueSpec("30", "30 tablets", "tablet"),)
            ),
        ),
        (
            CommonSalesOptionAxisSpec(
                "quantity", "수량", (CommonSalesOptionValueSpec("30", "30정", "tablet"),)
            ),
            CommonSalesOptionAxisSpec(
                "per_unit_weight",
                "개별 중량/용량",
                (CommonSalesOptionValueSpec("300", "300mg", "mg"),),
            ),
        ),
        (
            CommonSalesOptionAxisSpec(
                "per_unit_weight",
                "개별 중량/용량",
                (
                    CommonSalesOptionValueSpec("500", "500mg", "mg"),
                    CommonSalesOptionValueSpec("300", "300mg", "mg"),
                ),
            ),
            CommonSalesOptionAxisSpec(
                "quantity",
                "수량",
                (
                    CommonSalesOptionValueSpec("60", "60정", "tablet"),
                    CommonSalesOptionValueSpec("30", "30정", "tablet"),
                ),
            ),
        ),
    )

    identities = []
    for axes in variants:
        common = _record_options_and_mapping(container, group, revision, axes)
        recorded = container.atomic_skus.record_source_proven_set(
            group,
            (_semantic_configuration(common, revision),),
            reason="semantic identity check",
            decided_by="owner",
            correlation_id="atomic-revision",
        )
        identities.append(recorded.atomic_skus[0].atomic_sku_id)
        assert container.atomic_skus.current_for_use(group) == recorded

    assert len(set(identities)) == 1


@pytest.mark.parametrize(
    ("canonical_value", "unit_code"),
    (("0.3", "mg"), ("300", "g")),
)
def test_atomic_sku_identity_changes_with_canonical_value_or_unit(
    container: Container, canonical_value: str, unit_code: str
) -> None:
    group, revision, initial_common = _foundation(container)
    initial = container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(initial_common, revision, 0, ("300mg", "30정")),),
        reason="initial",
        decided_by="owner",
        correlation_id="initial-atomic",
    )
    changed = _record_options_and_mapping(
        container,
        group,
        revision,
        (
            CommonSalesOptionAxisSpec(
                "per_unit_weight",
                "개별 중량/용량",
                (CommonSalesOptionValueSpec(canonical_value, "300mg", unit_code),),
            ),
            CommonSalesOptionAxisSpec(
                "quantity", "수량", (CommonSalesOptionValueSpec("30", "30정", "tablet"),)
            ),
        ),
    )
    revised = container.atomic_skus.record_source_proven_set(
        group,
        (_semantic_configuration(changed, revision),),
        reason="semantic change",
        decided_by="owner",
        correlation_id="changed-atomic",
    )

    assert revised.atomic_skus[0].atomic_sku_id != initial.atomic_skus[0].atomic_sku_id


def test_current_for_use_fails_closed_on_source_revision_and_recovers_same_identity(
    config: AppConfig,
    container: Container,
) -> None:
    group, revision_id, common = _foundation(container)
    initial = container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(common, revision_id, 0, ("300mg", "30정")),),
        reason="initial",
        decided_by="owner",
        correlation_id="atomic-initial",
    )
    composition = container.product_store.composition(CompositionSpec.default_single_unit())
    initial_item = container.atomic_sku_items.item(
        group, composition.composition_id, initial.atomic_skus[0].atomic_sku_id
    )
    initial_rik2 = registration_item_key_v2(
        "icbm-listing-1",
        group,
        composition.composition_signature,
        initial.atomic_skus[0].atomic_sku_id,
    )
    source = container.product_store.source_product("kmretail", "1234")
    replacement = container.revisions.append(collected(fields=_option_fields(), images=()))
    container.product_store.record_move(
        source.source_product_uid,
        replacement.revision_id,
        reason=MoveReason.NEWER_REVISION,
        decided_by="owner",
        correlation_id="source-advanced",
    )

    assert container.atomic_skus.current(group) == initial
    assert container.atomic_skus.current_for_use(group) is None
    with pytest.raises(InputValidationError, match="not current"):
        container.atomic_sku_items.item(
            group, composition.composition_id, initial.atomic_skus[0].atomic_sku_id
        )
    with (
        contextlib.closing(_raw(config)) as raw,
        pytest.raises(sqlite3.IntegrityError, match="not current for use"),
    ):
        _raw_atomic_item_insert(
            raw,
            group,
            composition.composition_id,
            composition.composition_signature,
            initial.atomic_skus[0].atomic_sku_id,
            initial.atomic_skus[0].selection_signature,
        )

    _record_mapping_for_common(container, group, common, replacement.revision_id)
    recovered = container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(common, replacement.revision_id, 0, ("300mg", "30정")),),
        reason="reproved",
        decided_by="owner",
        correlation_id="atomic-recovered",
    )

    assert recovered.atomic_skus[0].atomic_sku_id == initial.atomic_skus[0].atomic_sku_id
    assert container.atomic_skus.current_for_use(group) == recovered
    recovered_item = container.atomic_sku_items.item(
        group, composition.composition_id, recovered.atomic_skus[0].atomic_sku_id
    )
    assert recovered_item.atomic_sku_item_id == initial_item.atomic_sku_item_id
    assert (
        registration_item_key_v2(
            "icbm-listing-1",
            group,
            composition.composition_signature,
            recovered.atomic_skus[0].atomic_sku_id,
        )
        == initial_rik2
    )


def test_current_for_use_fails_closed_when_common_options_advance(
    config: AppConfig,
    container: Container,
) -> None:
    group, revision_id, common = _foundation(container)
    historical = container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(common, revision_id, 0, ("300mg", "30정")),),
        reason="initial",
        decided_by="owner",
        correlation_id="atomic-initial",
    )
    container.common_sales_options.record_reviewed_revision(
        group,
        (
            CommonSalesOptionAxisSpec(
                "per_unit_weight",
                "1개당 내용량",
                (
                    CommonSalesOptionValueSpec("300", "300mg", "mg"),
                    CommonSalesOptionValueSpec("500", "500mg", "mg"),
                ),
            ),
            CommonSalesOptionAxisSpec(
                "quantity",
                "포장 수량",
                (
                    CommonSalesOptionValueSpec("30", "30정", "tablet"),
                    CommonSalesOptionValueSpec("60", "60정", "tablet"),
                ),
            ),
        ),
        reason="label revision",
        decided_by="owner",
        correlation_id="common-advanced",
    )

    assert container.atomic_skus.current(group) == historical
    assert container.atomic_skus.current_for_use(group) is None
    composition = container.product_store.composition(CompositionSpec.default_single_unit())
    with pytest.raises(InputValidationError, match="not current"):
        container.atomic_sku_items.item(
            group, composition.composition_id, historical.atomic_skus[0].atomic_sku_id
        )
    with (
        contextlib.closing(_raw(config)) as raw,
        pytest.raises(sqlite3.IntegrityError, match="not current for use"),
    ):
        _raw_atomic_item_insert(
            raw,
            group,
            composition.composition_id,
            composition.composition_signature,
            historical.atomic_skus[0].atomic_sku_id,
            historical.atomic_skus[0].selection_signature,
        )


def test_current_for_use_fails_closed_when_mapping_evidence_source_advances(
    config: AppConfig,
    container: Container,
) -> None:
    group, configuration_revision_id, common = _foundation(container)
    mapping_revision = container.revisions.append(
        collected(
            fields=_option_fields(),
            images=(),
            supplier_key="mapping-source",
            source_product_id="5678",
            source_url="https://mapping.example/products/5678",
        )
    )
    mapping_source = container.product_store.source_product("mapping-source", "5678")
    container.product_store.record_move(
        mapping_source.source_product_uid,
        mapping_revision.revision_id,
        reason=MoveReason.INITIAL,
        decided_by="owner",
        correlation_id="mapping-source-initial",
    )
    container.product_store.confirm_new_member(
        group,
        mapping_source.source_product_uid,
        reason="reviewed",
        decided_by="owner",
        correlation_id="mapping-source-member",
    )
    _record_mapping_for_common(container, group, common, mapping_revision.revision_id)
    historical = container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(common, configuration_revision_id, 0, ("300mg", "30정")),),
        reason="cross-source proof",
        decided_by="owner",
        correlation_id="cross-source-atomic",
    )
    assert container.atomic_skus.current_for_use(group) == historical

    replacement = container.revisions.append(
        collected(
            fields=_option_fields(),
            images=(),
            supplier_key="mapping-source",
            source_product_id="5678",
            source_url="https://mapping.example/products/5678",
        )
    )
    container.product_store.record_move(
        mapping_source.source_product_uid,
        replacement.revision_id,
        reason=MoveReason.NEWER_REVISION,
        decided_by="owner",
        correlation_id="mapping-source-advanced",
    )

    assert container.atomic_skus.current(group) == historical
    assert container.atomic_skus.current_for_use(group) is None
    composition = container.product_store.composition(CompositionSpec.default_single_unit())
    with pytest.raises(InputValidationError, match="not current"):
        container.atomic_sku_items.item(
            group, composition.composition_id, historical.atomic_skus[0].atomic_sku_id
        )
    with (
        contextlib.closing(_raw(config)) as raw,
        pytest.raises(sqlite3.IntegrityError, match="not current for use"),
    ):
        _raw_atomic_item_insert(
            raw,
            group,
            composition.composition_id,
            composition.composition_signature,
            historical.atomic_skus[0].atomic_sku_id,
            historical.atomic_skus[0].selection_signature,
        )


def test_current_for_use_fails_closed_when_mapping_advances(
    config: AppConfig,
    container: Container,
) -> None:
    group, revision_id, common = _foundation(container)
    historical = container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(common, revision_id, 0, ("300mg", "30정")),),
        reason="initial",
        decided_by="owner",
        correlation_id="atomic-initial",
    )
    other_revision = container.revisions.append(
        collected(
            fields=_option_fields(),
            images=(),
            supplier_key="other",
            source_product_id="5678",
            source_url="https://other.example/products/5678",
        )
    )
    other = container.product_store.source_product("other", "5678")
    container.product_store.record_move(
        other.source_product_uid,
        other_revision.revision_id,
        reason=MoveReason.INITIAL,
        decided_by="owner",
        correlation_id="other-source",
    )
    container.product_store.confirm_new_member(
        group,
        other.source_product_uid,
        reason="reviewed",
        decided_by="owner",
        correlation_id="other-member",
    )
    _record_mapping_for_common(container, group, common, other_revision.revision_id)

    assert container.atomic_skus.current(group) == historical
    assert container.atomic_skus.current_for_use(group) is None
    composition = container.product_store.composition(CompositionSpec.default_single_unit())
    with pytest.raises(InputValidationError, match="not current"):
        container.atomic_sku_items.item(
            group, composition.composition_id, historical.atomic_skus[0].atomic_sku_id
        )
    with (
        contextlib.closing(_raw(config)) as raw,
        pytest.raises(sqlite3.IntegrityError, match="not current for use"),
    ):
        _raw_atomic_item_insert(
            raw,
            group,
            composition.composition_id,
            composition.composition_signature,
            historical.atomic_skus[0].atomic_sku_id,
            historical.atomic_skus[0].selection_signature,
        )


def test_one_stale_source_dependency_invalidates_the_entire_multi_source_set(
    config: AppConfig, container: Container
) -> None:
    group, first_revision, common = _foundation(container)
    second_revision = container.revisions.append(
        collected(
            fields=_option_fields(),
            images=(),
            supplier_key="other",
            source_product_id="5678",
            source_url="https://other.example/products/5678",
        )
    )
    second_source = container.product_store.source_product("other", "5678")
    container.product_store.record_move(
        second_source.source_product_uid,
        second_revision.revision_id,
        reason=MoveReason.INITIAL,
        decided_by="owner",
        correlation_id="second-source",
    )
    container.product_store.confirm_new_member(
        group,
        second_source.source_product_uid,
        reason="reviewed",
        decided_by="owner",
        correlation_id="second-member",
    )
    atomic_set = container.atomic_skus.record_source_proven_set(
        group,
        (
            _configuration(common, first_revision, 0, ("300mg", "30정")),
            _configuration(common, second_revision.revision_id, 1, ("500mg", "60정")),
        ),
        reason="multi-source proof",
        decided_by="owner",
        correlation_id="multi-source",
    )
    composition = container.product_store.composition(CompositionSpec.default_single_unit())
    first_item = container.atomic_sku_items.item(
        group, composition.composition_id, atomic_set.atomic_skus[0].atomic_sku_id
    )
    replacement = container.revisions.append(
        collected(
            fields=_option_fields(),
            images=(),
            supplier_key="other",
            source_product_id="5678",
            source_url="https://other.example/products/5678",
        )
    )
    container.product_store.record_move(
        second_source.source_product_uid,
        replacement.revision_id,
        reason=MoveReason.NEWER_REVISION,
        decided_by="owner",
        correlation_id="second-source-advanced",
    )

    assert container.atomic_skus.current_for_use(group) is None
    with pytest.raises(InputValidationError, match="not current"):
        container.atomic_sku_items.item(
            group, composition.composition_id, atomic_set.atomic_skus[0].atomic_sku_id
        )
    assert (
        container.atomic_sku_items.find(
            group, composition.composition_signature, atomic_set.atomic_skus[0].atomic_sku_id
        )
        == first_item
    )
    with (
        contextlib.closing(_raw(config)) as raw,
        pytest.raises(sqlite3.IntegrityError, match="not current for use"),
    ):
        _raw_atomic_item_insert(
            raw,
            group,
            composition.composition_id,
            composition.composition_signature,
            atomic_set.atomic_skus[0].atomic_sku_id,
            atomic_set.atomic_skus[0].selection_signature,
        )


def test_atomic_sku_items_extend_legacy_items_without_reinterpreting_them(
    config: AppConfig, container: Container
) -> None:
    group, revision, common = _foundation(container)
    atomic_set = container.atomic_skus.record_source_proven_set(
        group,
        (
            _configuration(common, revision, 0, ("300mg", "30정")),
            _configuration(common, revision, 1, ("500mg", "60정")),
        ),
        reason="reviewed",
        decided_by="owner",
        correlation_id="atomic",
    )
    composition = container.product_store.composition(CompositionSpec.default_single_unit())
    legacy = container.product_store.item(group, composition.composition_id)

    first = container.atomic_sku_items.item(
        group, composition.composition_id, atomic_set.atomic_skus[0].atomic_sku_id
    )
    same = container.atomic_sku_items.item(
        group, composition.composition_id, atomic_set.atomic_skus[0].atomic_sku_id
    )
    second = container.atomic_sku_items.item(
        group, composition.composition_id, atomic_set.atomic_skus[1].atomic_sku_id
    )

    assert same == first
    assert first.atomic_sku_item_id != second.atomic_sku_item_id
    assert first.atomic_sku_id != second.atomic_sku_id
    assert first.composition_signature == legacy.composition_signature
    assert first.atomic_sku_item_id != legacy.item_id
    assert container.product_store.item(group, composition.composition_id) == legacy
    assert (
        container.atomic_sku_items.find(
            group, composition.composition_signature, first.atomic_sku_id
        )
        == first
    )

    with contextlib.closing(_raw(config)) as raw:
        for statement in (
            "UPDATE atomic_sku_product_items SET composition_signature = '" + "0" * 64 + "'",
            "DELETE FROM atomic_sku_product_items",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(statement)


def test_atomic_sku_item_requires_an_active_product_group(
    config: AppConfig, container: Container
) -> None:
    group, revision, common = _foundation(container)
    atomic_set = container.atomic_skus.record_source_proven_set(
        group,
        (_configuration(common, revision, 0, ("300mg", "30정")),),
        reason="reviewed",
        decided_by="owner",
        correlation_id="atomic",
    )
    composition = container.product_store.composition(CompositionSpec.default_single_unit())
    atomic_sku = atomic_set.atomic_skus[0]

    with contextlib.closing(_raw(config)) as raw:
        raw.execute(
            "UPDATE product_groups SET status = 'RETIRED',"
            " retired_at = '2026-10-04 00:00:00' WHERE product_group_id = ?",
            (group,),
        )
        raw.commit()

    with pytest.raises(IntegrityError, match="ACTIVE group"):
        container.atomic_sku_items.item(group, composition.composition_id, atomic_sku.atomic_sku_id)

    with (
        contextlib.closing(_raw(config)) as raw,
        pytest.raises(sqlite3.IntegrityError, match="ACTIVE group"),
    ):
        _raw_atomic_item_insert(
            raw,
            group,
            composition.composition_id,
            composition.composition_signature,
            atomic_sku.atomic_sku_id,
            atomic_sku.selection_signature,
        )


def test_atomic_sku_item_requires_current_membership(container: Container) -> None:
    group, revision, common = _foundation(container)
    specs = (
        _configuration(common, revision, 0, ("300mg", "30정")),
        _configuration(common, revision, 1, ("500mg", "60정")),
    )
    first_set = container.atomic_skus.record_source_proven_set(
        group,
        specs,
        reason="reviewed",
        decided_by="owner",
        correlation_id="atomic-1",
    )
    container.atomic_skus.record_source_proven_set(
        group,
        specs[:1],
        reason="reviewed removal",
        decided_by="owner",
        correlation_id="atomic-2",
    )
    composition = container.product_store.composition(CompositionSpec.default_single_unit())

    with pytest.raises(InputValidationError, match="not current"):
        container.atomic_sku_items.item(
            group, composition.composition_id, first_set.atomic_skus[1].atomic_sku_id
        )
