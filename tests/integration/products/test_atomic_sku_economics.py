"""C-P2 AtomicSKU-qualified source economics and pricing, provider-zero."""

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

import pytest

from app.capabilities.overseas.fx import FxSnapshotStore, RefreshTrigger
from app.capabilities.overseas.settings import SaveOverseasSettingsRequest
from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.errors import InputValidationError
from app.stages.collect.facts import (
    FieldFact,
    ForeignPrice,
    ForeignPricesValue,
    ForeignShippingValue,
    OptionAxis,
    OptionConfiguration,
    OptionsValue,
    ShippingKind,
)
from app.stages.collect.revisions import ProductFactsRevisionStore, StoredRevision
from app.stages.products import pricing_service
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
from app.stages.products.landed_cost import (
    FX_UNAVAILABLE,
    LANDED_COST_COLUMNS,
    OVERSEAS_SHIPPING_UNSET,
)
from app.stages.products.model import CompositionSpec, MoveReason, ReadinessStatus, Reason
from app.stages.products.overseas_costs import SaveOverseasCostsRequest
from app.stages.products.pricing import (
    MINIMUM_SALE_PRICE_NOT_APPLIED,
    PURCHASE_PRICE_UNRESOLVED,
    PriceBasis,
)
from app.stages.products.pricing_service import PricingOutcome
from app.stages.products.sales_policy import SaveSalesPolicyRequest
from tests.integration.overseas.test_fx_snapshots import publication
from tests.support.collect_support import collected, confirmed
from tests.support.jobs_support import FakeClock
from tests.support.product_support import SUPPLIER, context, minimum, product

pytestmark = pytest.mark.integration


def _build(
    container: Container,
    configurations: Sequence[OptionConfiguration],
    *,
    composition: CompositionSpec | None = None,
    minimum_sale_price: FieldFact | None = None,
    **overrides: FieldFact,
):  # type: ignore[no-untyped-def]
    fields = product(
        price=10_000,
        minimum_sale_price=minimum_sale_price,
        options=confirmed(
            OptionsValue(
                axes=(OptionAxis(name="용량", values=("300mg", "500mg")),),
                configurations=tuple(configurations),
            ),
            ".options",
        ),
        **overrides,
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


def test_a_supplier_minimum_the_policy_does_not_apply_is_kept_beside_the_atomic_price(
    container: Container, config: AppConfig
) -> None:
    # ADR-0039 §3: the AtomicSKU path reads the bound member's supplier's sales policy as well.
    _group, _revision, _source, (item,) = _build(
        container,
        (
            OptionConfiguration(
                selections=("300mg",), supplier_sku_id="sku-a", additional_price_krw=0
            ),
        ),
        minimum_sale_price=minimum(50_000),
    )
    container.atomic_sku_economics.bind_source_configuration(
        item.atomic_sku_item_id, decided_by="owner", correlation_id="bind"
    )
    ctx = context()
    applied = container.pricing.price_atomic_sku(item.atomic_sku_item_id, ctx)
    assert applied.snapshot is not None
    assert applied.snapshot.price_basis is PriceBasis.MINIMUM_SALE_PRICE
    assert applied.snapshot.minimum_applied and applied.snapshot.final_sale_price_krw == 50_000

    def policy(expected: int | None, *, minimum_price: bool) -> None:
        container.sales_policies.save(
            SUPPLIER,
            SaveSalesPolicyRequest(
                expected_revision_no=expected,
                apply_channel_restrictions=True,
                apply_minimum_price=minimum_price,
            ),
            actor="owner",
            correlation_id="policy",
        )

    policy(None, minimum_price=False)
    stale = container.product_readiness.atomic_pricing_readiness(item.atomic_sku_item_id, ctx)
    assert stale.status is ReadinessStatus.STALE  # the policy is in the dependency fingerprint
    waived = container.pricing.price_atomic_sku(item.atomic_sku_item_id, ctx)
    assert waived.outcome is PricingOutcome.RECORDED and waived.snapshot is not None
    assert waived.snapshot.price_basis is PriceBasis.TARGET_MARGIN
    assert waived.snapshot.minimum_sale_price_krw is None
    assert not waived.snapshot.minimum_applied
    assert waived.snapshot.stated_minimum_sale_price_krw == 50_000
    assert waived.snapshot.final_sale_price_krw < 50_000
    ready = container.product_readiness.atomic_pricing_readiness(item.atomic_sku_item_id, ctx)
    assert ready.status is ReadinessStatus.READY
    assert MINIMUM_SALE_PRICE_NOT_APPLIED in {reason.code for reason in ready.reasons}
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        details = raw.execute(
            "SELECT details_json FROM audit_events WHERE target_ref = ?",
            (waived.snapshot.pricing_snapshot_id,),
        ).fetchone()
    assert details is not None
    recorded = json.loads(details[0])
    assert (recorded["minimum_applied"], recorded["stated_minimum_sale_price_krw"]) == (
        False,
        50_000,
    )

    policy(1, minimum_price=True)
    again = container.pricing.price_atomic_sku(item.atomic_sku_item_id, ctx)
    assert again.outcome is PricingOutcome.RECORDED and again.snapshot is not None
    assert again.snapshot.minimum_applied and again.snapshot.final_sale_price_krw == 50_000
    assert again.snapshot.dependency_fingerprint == applied.snapshot.dependency_fingerprint


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


# ---------------------------------------------------------------- ADR-0038: source currency


def _cny_overrides() -> dict[str, FieldFact]:
    return {
        "prices": confirmed(
            ForeignPricesValue(prices=(ForeignPrice(label="p", currency="CNY", amount="9.50"),)),
            ".price",
        ),
        "shipping": confirmed(
            ForeignShippingValue(kind=ShippingKind.FREE, policy_text="free", currency="CNY"),
            ".delivery",
        ),
    }


def _cny_configurations() -> tuple[OptionConfiguration, ...]:
    return (
        OptionConfiguration(
            selections=("300mg",), supplier_sku_id="sku-a", unit_price="9.50", currency="CNY"
        ),
        OptionConfiguration(
            selections=("500mg",), supplier_sku_id="sku-b", unit_price="12.00", currency="CNY"
        ),
    )


def _rates_and_shipping(container: Container, clock: FakeClock) -> None:
    FxSnapshotStore(container.db, clock, container.audit).record(
        publication(date(2026, 9, 11)),
        trigger=RefreshTrigger.MANUAL,
        actor="test",
        correlation_id="cid-fx",
    )
    container.overseas_settings.save(
        SaveOverseasSettingsRequest(
            expected_revision_no=None,
            fx_margin_percent="5",
            overseas_shipping_default_krw=8000,
            duties_included_over_usd150=True,
            duty_rate_percent="8",
            smartstore_overseas_shipping_address_id=None,
            overseas_delivery_days_min=7,
            overseas_delivery_days_max=14,
            importer_label=None,
        ),
        actor="operator:local",
        correlation_id="cid-settings",
    )


def test_a_source_currency_configuration_is_bound_at_its_own_unit_price_and_landed_cost(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    _group, revision, _source, items = _build(container, _cny_configurations(), **_cny_overrides())
    assert revision.source_currency == "CNY"
    bindings = tuple(
        container.atomic_sku_economics.bind_source_configuration(
            item.atomic_sku_item_id, decided_by="owner", correlation_id=f"bind-{index}"
        )
        for index, item in enumerate(items)
    )
    # The binding states no won: the unit price as stated, in its currency.
    assert [(b.source_currency, b.source_unit_price) for b in bindings] == [
        ("CNY", "9.50"),
        ("CNY", "12.00"),
    ]
    assert {
        (b.base_purchase_cost_krw, b.option_additional_price_krw, b.purchase_cost_krw)
        for b in bindings
    } == {(0, 0, 0)}
    again = container.atomic_sku_economics.bind_source_configuration(
        items[0].atomic_sku_item_id, decided_by="owner", correlation_id="same"
    )
    assert again == bindings[0]
    ctx = context()
    # No rate and no overseas shipping: nothing is priced.
    refused = container.pricing.price_atomic_sku(items[0].atomic_sku_item_id, ctx)
    assert refused.outcome is PricingOutcome.NOT_PRICED
    assert {(r.code, r.subject) for r in refused.reasons} == {
        (FX_UNAVAILABLE, "fx:CNY"),
        (FX_UNAVAILABLE, "fx:USD"),
        (OVERSEAS_SHIPPING_UNSET, "overseas_shipping"),
    }
    _rates_and_shipping(container, clock)
    results = [container.pricing.price_atomic_sku(item.atomic_sku_item_id, ctx) for item in items]
    assert [r.outcome for r in results] == [PricingOutcome.RECORDED, PricingOutcome.RECORDED]
    snapshots = [r.snapshot for r in results]
    assert snapshots[0] is not None and snapshots[1] is not None
    # 9.50 × 195.12 × 1.05 = 1946.322 → 1947; 12.00 × 195.12 × 1.05 = 2458.512 → 2459.
    assert [s.purchase_cost_krw for s in snapshots if s is not None] == [1947, 2459]
    assert {s.supplier_shipping_krw for s in snapshots if s is not None} == {8000}
    pin = snapshots[0].landed_cost
    assert pin is not None
    assert (pin.source_currency, pin.source_price, pin.fx_publication_date) == (
        "CNY",
        "9.50",
        "2026-09-11",
    )
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        raw.row_factory = sqlite3.Row
        row = raw.execute(
            "SELECT * FROM atomic_sku_pricing_snapshots WHERE pricing_snapshot_id = ?",
            (snapshots[0].pricing_snapshot_id,),
        ).fetchone()
        (details,) = raw.execute(
            "SELECT details_json FROM audit_events WHERE target_ref = ?"
            " AND action = 'atomic_sku_pricing_snapshot.record'",
            (snapshots[0].pricing_snapshot_id,),
        ).fetchone()
    assert {name: row[name] for name in LANDED_COST_COLUMNS} == pin.columns()
    assert json.loads(details)["landed_cost"] == pin.columns()
    readiness = container.product_readiness.atomic_pricing_readiness(
        items[0].atomic_sku_item_id, ctx
    )
    assert readiness.status is ReadinessStatus.READY, readiness.reasons
    # The product's own overseas shipping makes the current price STALE.
    container.overseas_costs.save(
        SUPPLIER,
        "1234",
        SaveOverseasCostsRequest(
            expected_revision_no=None, overseas_shipping_krw=12000, duty_rate_percent=None
        ),
        actor="operator:local",
        correlation_id="cid-costs",
    )
    stale = container.product_readiness.atomic_pricing_readiness(items[0].atomic_sku_item_id, ctx)
    assert stale.status is ReadinessStatus.STALE
    repriced = container.pricing.price_atomic_sku(items[0].atomic_sku_item_id, ctx)
    assert repriced.outcome is PricingOutcome.RECORDED and repriced.snapshot is not None
    assert repriced.snapshot.supplier_shipping_krw == 12000


def test_a_source_currency_configuration_without_its_own_price_is_not_bound(
    container: Container, config: AppConfig
) -> None:
    _group, revision, _source, (item,) = _build(
        container,
        (OptionConfiguration(selections=("300mg",), supplier_sku_id="sku-a"),),
        **_cny_overrides(),
    )
    assert revision.source_currency == "CNY"
    with pytest.raises(InputValidationError) as caught:
        container.atomic_sku_economics.bind_source_configuration(
            item.atomic_sku_item_id, decided_by="owner", correlation_id="bind"
        )
    assert caught.value.code == "PRODUCTS_ATOMIC_SKU_UNIT_PRICE_UNRESOLVED"
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        assert raw.execute("SELECT COUNT(*) FROM atomic_sku_source_bindings").fetchone() == (0,)
    result = container.pricing.price_atomic_sku(item.atomic_sku_item_id, context())
    assert result.outcome is PricingOutcome.NOT_PRICED and result.snapshot is None


def test_the_atomic_path_never_reads_a_won_binding_as_a_source_currency_price(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    _group, _revision, _source, (item,) = _build(
        container,
        (OptionConfiguration(selections=("300mg",), additional_price_krw=0),),
    )
    container.atomic_sku_economics.bind_source_configuration(
        item.atomic_sku_item_id, decided_by="owner", correlation_id="bind"
    )
    # The bound revision as a source-currency revision reads back: its won binding states no
    # unit price in that currency, so nothing is read and nothing is priced.
    read = ProductFactsRevisionStore.get

    def as_cny(self: ProductFactsRevisionStore, revision_id: str) -> StoredRevision | None:
        stored = read(self, revision_id)
        return None if stored is None else replace(stored, source_currency="CNY")

    def no_amount_is_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an amount of a source-currency revision was read")

    monkeypatch.setattr(ProductFactsRevisionStore, "get", as_cny)
    monkeypatch.setattr(pricing_service, "source_inputs", no_amount_is_read)
    monkeypatch.setattr(pricing_service, "foreign_source_inputs", no_amount_is_read)
    result = container.pricing.price_atomic_sku(item.atomic_sku_item_id, context())
    assert result.outcome is PricingOutcome.NOT_PRICED
    assert result.reasons == (
        Reason(PURCHASE_PRICE_UNRESOLVED, ReadinessStatus.REVIEW_REQUIRED, "options"),
    )


def test_the_database_keeps_a_foreign_binding_and_its_price_exact(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    _group, _revision, _source, (item, _other) = _build(
        container, _cny_configurations(), **_cny_overrides()
    )
    binding = container.atomic_sku_economics.bind_source_configuration(
        item.atomic_sku_item_id, decided_by="owner", correlation_id="bind"
    )
    _rates_and_shipping(container, clock)
    result = container.pricing.price_atomic_sku(item.atomic_sku_item_id, context())
    assert result.snapshot is not None
    with contextlib.closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        # Close-only: the unit price of an open binding never changes.
        with pytest.raises(sqlite3.IntegrityError, match="may only be closed"):
            raw.execute(
                "UPDATE atomic_sku_source_bindings SET source_unit_price = '1.00'"
                " WHERE binding_id = ?",
                (binding.binding_id,),
            )
        columns = [r[1] for r in raw.execute("PRAGMA table_info(atomic_sku_source_bindings)")]

        def copy_binding(**changes: object) -> None:
            values = {"binding_id": "b-x", "valid_to": None, **changes}
            select = ", ".join("?" if name in values else name for name in columns)
            raw.execute(
                f"INSERT INTO atomic_sku_source_bindings ({', '.join(columns)}) SELECT {select}"
                " FROM atomic_sku_source_bindings WHERE binding_id = ?",
                (*[values[name] for name in columns if name in values], binding.binding_id),
            )

        # A foreign binding states no won, and a scale its currency allows.
        with pytest.raises(sqlite3.IntegrityError, match="foreign_unit_price_recorded"):
            copy_binding(base_purchase_cost_krw=1000, purchase_cost_krw=1000)
        with pytest.raises(sqlite3.IntegrityError, match="foreign_unit_price_recorded"):
            copy_binding(source_unit_price="9.505")
        snapshot_columns = [
            r[1] for r in raw.execute("PRAGMA table_info(atomic_sku_pricing_snapshots)")
        ]
        values = {"pricing_snapshot_id": "s-x", "source_price": "1.00"}
        select = ", ".join("?" if name in values else name for name in snapshot_columns)
        # A foreign snapshot prices its open binding's own unit price, and no other.
        with pytest.raises(sqlite3.IntegrityError, match="exact open binding"):
            raw.execute(
                f"INSERT INTO atomic_sku_pricing_snapshots ({', '.join(snapshot_columns)})"
                f" SELECT {select} FROM atomic_sku_pricing_snapshots"
                " WHERE pricing_snapshot_id = ?",
                (
                    *[values[name] for name in snapshot_columns if name in values],
                    result.snapshot.pricing_snapshot_id,
                ),
            )
