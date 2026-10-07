"""C-P2 AtomicSKU-qualified source economics and pricing, provider-zero."""

import contextlib
import sqlite3
import uuid
from collections.abc import Sequence

import pytest

from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.errors import InputValidationError
from app.stages.collect.facts import OptionAxis, OptionConfiguration, OptionsValue
from app.stages.products.atomic_sku import (
    AtomicSKUConfigurationSpec,
    AtomicSKUSelectionSpec,
)
from app.stages.products.common_option_mapping import (
    CommonOptionAxisFactMappingSpec,
    CommonOptionValueFactMappingSpec,
    ProductFactPointer,
)
from app.stages.products.common_options import (
    CommonSalesOptionAxisSpec,
    CommonSalesOptionValueSpec,
)
from app.stages.products.model import CompositionSpec, MoveReason, ReadinessStatus
from app.stages.products.pricing_service import PricingOutcome
from tests.support.collect_support import collected, confirmed
from tests.support.product_support import context, product

pytestmark = pytest.mark.integration


def _build(
    container: Container,
    configurations: Sequence[OptionConfiguration],
    *,
    composition: CompositionSpec | None = None,
):  # type: ignore[no-untyped-def]
    fields = product(
        price=10_000,
        options=confirmed(
            OptionsValue(
                axes=(OptionAxis(name="용량", values=("300mg", "500mg")),),
                configurations=tuple(configurations),
            ),
            ".options",
        ),
    )
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
                "용량",
                (
                    CommonSalesOptionValueSpec("300", "300mg", "mg"),
                    CommonSalesOptionValueSpec("500", "500mg", "mg"),
                ),
            ),
        ),
        reason="reviewed",
        decided_by="owner",
        correlation_id="options",
    )
    axis = common.axes[0]
    container.common_option_fact_mappings.record_reviewed_mapping(
        group,
        common.revision_id,
        (
            CommonOptionAxisFactMappingSpec(
                axis.axis_id,
                ProductFactPointer(revision.revision_id, "options", "$.axes[0].name"),
                tuple(
                    CommonOptionValueFactMappingSpec(
                        value.value_id,
                        ProductFactPointer(
                            revision.revision_id,
                            "options",
                            f"$.axes[0].values[{value.ordinal}]",
                        ),
                    )
                    for value in axis.values
                ),
            ),
        ),
        evidence_reference="review",
        reason="exact correspondence",
        reviewed_by="owner",
        correlation_id="mapping",
    )
    specs = []
    for index, configuration in enumerate(configurations):
        value = next(
            candidate
            for candidate in axis.values
            if candidate.display_value == configuration.selections[0]
        )
        specs.append(
            AtomicSKUConfigurationSpec(
                revision.revision_id,
                "options",
                f"$.configurations[{index}]",
                (
                    AtomicSKUSelectionSpec(
                        axis.axis_id,
                        value.value_id,
                        f"$.configurations[{index}].selections[0]",
                    ),
                ),
            )
        )
    atomic_set = container.atomic_skus.record_source_proven_set(
        group,
        tuple(specs),
        reason="reviewed source configurations",
        decided_by="owner",
        correlation_id="atomic",
    )
    listing = container.product_store.composition(
        composition or CompositionSpec.default_single_unit()
    )
    items = tuple(
        container.atomic_sku_items.item(group, listing.composition_id, sku.atomic_sku_id)
        for sku in atomic_set.atomic_skus
    )
    return group, revision, source, items


def test_each_atomic_sku_has_exact_cost_and_its_own_pricing_snapshot(
    container: Container,
) -> None:
    _group, _revision, _source, items = _build(
        container,
        (
            OptionConfiguration(
                selections=("300mg",), supplier_sku_id="sku-a", additional_price_krw=0
            ),
            OptionConfiguration(
                selections=("500mg",), supplier_sku_id="sku-b", additional_price_krw=2_000
            ),
        ),
    )
    bindings = tuple(
        container.atomic_sku_economics.bind_source_configuration(
            item.atomic_sku_item_id, decided_by="owner", correlation_id=f"bind-{index}"
        )
        for index, item in enumerate(items)
    )
    assert [binding.purchase_cost_krw for binding in bindings] == [10_000, 12_000]
    assert (
        container.atomic_sku_economics.bind_source_configuration(
            items[0].atomic_sku_item_id,
            decided_by="owner",
            correlation_id="same-proof",
        )
        == bindings[0]
    )

    ctx = context()
    before = container.product_readiness.atomic_pricing_readiness(items[0].atomic_sku_item_id, ctx)
    assert before.status is ReadinessStatus.STALE
    results = tuple(
        container.pricing.price_atomic_sku(item.atomic_sku_item_id, ctx) for item in items
    )
    assert [result.outcome for result in results] == [
        PricingOutcome.RECORDED,
        PricingOutcome.RECORDED,
    ]
    snapshots = [result.snapshot for result in results]
    assert all(snapshot is not None for snapshot in snapshots)
    assert [snapshot.purchase_cost_krw for snapshot in snapshots if snapshot is not None] == [
        10_000,
        12_000,
    ]
    assert snapshots[0] is not None and snapshots[1] is not None
    assert snapshots[0].final_sale_price_krw != snapshots[1].final_sale_price_krw
    assert (
        container.product_readiness.atomic_pricing_readiness(
            items[0].atomic_sku_item_id, ctx
        ).status
        is ReadinessStatus.READY
    )
    # Idempotence pins the already-current immutable snapshot.
    same = container.pricing.price_atomic_sku(items[0].atomic_sku_item_id, ctx)
    assert same.outcome is PricingOutcome.UNCHANGED
    assert same.snapshot == snapshots[0]


def test_missing_option_delta_is_review_required_not_fabricated(container: Container) -> None:
    _group, _revision, _source, (item,) = _build(
        container,
        (OptionConfiguration(selections=("300mg",), supplier_sku_id="sku-a"),),
    )
    with pytest.raises(InputValidationError, match="price delta explicitly"):
        container.atomic_sku_economics.bind_source_configuration(
            item.atomic_sku_item_id, decided_by="owner", correlation_id="bind"
        )
    assert container.atomic_sku_economics.current_binding(item.atomic_sku_item_id) is None


def test_composed_fulfillment_is_not_inferred_from_one_option_delta(
    container: Container,
) -> None:
    _group, _revision, _source, (item,) = _build(
        container,
        (
            OptionConfiguration(
                selections=("300mg",), supplier_sku_id="sku-a", additional_price_krw=0
            ),
        ),
        composition=CompositionSpec(quantity=2),
    )
    with pytest.raises(InputValidationError, match="composed fulfillment is not inferred"):
        container.atomic_sku_economics.bind_source_configuration(
            item.atomic_sku_item_id, decided_by="owner", correlation_id="bind"
        )


def test_source_movement_stales_binding_but_keeps_pinned_snapshot(
    container: Container, config: AppConfig
) -> None:
    _group, revision, source, (item,) = _build(
        container,
        (
            OptionConfiguration(
                selections=("300mg",), supplier_sku_id="sku-a", additional_price_krw=0
            ),
        ),
    )
    container.atomic_sku_economics.bind_source_configuration(
        item.atomic_sku_item_id, decided_by="owner", correlation_id="bind"
    )
    ctx = context()
    recorded = container.pricing.price_atomic_sku(item.atomic_sku_item_id, ctx)
    assert recorded.snapshot is not None

    successor_fields = product(
        price=10_000,
        options=confirmed(
            OptionsValue(
                axes=(OptionAxis(name="용량", values=("300mg", "500mg")),),
                configurations=(
                    OptionConfiguration(
                        selections=("300mg",),
                        supplier_sku_id="sku-a",
                        additional_price_krw=0,
                    ),
                ),
            ),
            ".options",
        ),
    )
    successor = container.revisions.append(
        collected(fields=successor_fields, images=(), captured_at=revision.captured_at)
    )
    container.product_store.record_move(
        source.source_product_uid,
        successor.revision_id,
        reason=MoveReason.NEWER_REVISION,
        decided_by="test",
        correlation_id="source-2",
    )
    evaluation = container.pricing.evaluate_atomic_sku(item.atomic_sku_item_id, ctx)
    assert {reason.code for reason in evaluation.reasons} >= {"ATOMIC_SKU_SET_STALE"}
    assert evaluation.current_snapshot == recorded.snapshot
    refused = container.pricing.price_atomic_sku(item.atomic_sku_item_id, ctx)
    assert refused.outcome is PricingOutcome.NOT_PRICED
    assert refused.snapshot is None

    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        columns = tuple(
            row[1] for row in raw.execute("PRAGMA table_info(atomic_sku_pricing_snapshots)")
        )
        projected = tuple("?" if column == "pricing_snapshot_id" else column for column in columns)
        with pytest.raises(sqlite3.IntegrityError, match="binding proof is not current"):
            raw.execute(
                "INSERT INTO atomic_sku_pricing_snapshots ("
                + ", ".join(columns)
                + ") SELECT "
                + ", ".join(projected)
                + " FROM atomic_sku_pricing_snapshots WHERE pricing_snapshot_id = ?",
                (str(uuid.uuid4()), recorded.snapshot.pricing_snapshot_id),
            )


def test_retired_group_cannot_receive_a_new_atomic_source_binding(
    container: Container, config: AppConfig
) -> None:
    group, _revision, _source, (item,) = _build(
        container,
        (
            OptionConfiguration(
                selections=("300mg",), supplier_sku_id="sku-a", additional_price_krw=0
            ),
        ),
    )
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        raw.execute(
            "UPDATE product_groups SET status = 'RETIRED',"
            " retired_at = '2026-10-07 00:00:00' WHERE product_group_id = ?",
            (group,),
        )
        raw.commit()
    with pytest.raises(InputValidationError, match="active ProductGroup"):
        container.atomic_sku_economics.bind_source_configuration(
            item.atomic_sku_item_id, decided_by="owner", correlation_id="retired"
        )


def test_raw_database_cannot_rewrite_binding_or_pricing_history(
    container: Container, config: AppConfig
) -> None:
    _group, _revision, _source, (item,) = _build(
        container,
        (
            OptionConfiguration(
                selections=("300mg",), supplier_sku_id="sku-a", additional_price_krw=0
            ),
        ),
    )
    binding = container.atomic_sku_economics.bind_source_configuration(
        item.atomic_sku_item_id, decided_by="owner", correlation_id="bind"
    )
    result = container.pricing.price_atomic_sku(item.atomic_sku_item_id, context())
    assert result.snapshot is not None
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="may only be closed"):
            raw.execute(
                "UPDATE atomic_sku_source_bindings SET purchase_cost_krw = 1 WHERE binding_id = ?",
                (binding.binding_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(
                "UPDATE atomic_sku_pricing_snapshots SET final_sale_price_krw = 1"
                " WHERE pricing_snapshot_id = ?",
                (result.snapshot.pricing_snapshot_id,),
            )
