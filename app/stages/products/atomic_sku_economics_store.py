"""Persistence for AtomicSKU-qualified M4 source bindings and pricing history."""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.core.clock import Clock
from app.platform.core.errors import InputValidationError, NotFoundError
from app.platform.db.database import Database
from app.stages.collect.facts import FieldStatus, OptionConfiguration, PricesValue, value_from_json
from app.stages.collect.models import ProductFactsField, ProductFactsRevision
from app.stages.products.atomic_sku_economics_models import (
    ATOMIC_ACQUISITION_BASIS,
    AtomicSKUPricingSnapshot,
    AtomicSKUSourceBinding,
    CurrentAtomicSKUPricingSnapshotMove,
)
from app.stages.products.atomic_sku_item_models import AtomicSKUProductItem
from app.stages.products.atomic_sku_item_store import AtomicSKUItemRecord
from app.stages.products.atomic_sku_models import AtomicSKURevisionMember
from app.stages.products.atomic_sku_store import AtomicSKUStore
from app.stages.products.model import DEFAULT_SINGLE_UNIT_SIGNATURE, GroupStatus
from app.stages.products.models import ProductGroup
from app.stages.products.pricing import (
    MINIMUM_NET_MARGIN,
    PRICING_RULE_VERSION,
    TARGET_NET_MARGIN,
    Calculation,
    GuardReason,
    PriceBasis,
    PriceGuard,
    PricingContextInput,
    PricingMoveReason,
    SourceInputs,
    digest,
)
from app.stages.products.store import ProductFoundationUnit


@dataclass(frozen=True)
class AtomicSourceBindingRecord:
    binding_id: str
    atomic_sku_item_id: str
    atomic_sku_revision_member_id: str
    group_member_id: str
    provenance_revision_id: str
    acquisition_basis: str
    base_purchase_cost_krw: int
    option_additional_price_krw: int
    purchase_cost_krw: int
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class AtomicPricingDependencies:
    item: AtomicSKUItemRecord
    membership_revision_id: str
    binding: AtomicSourceBindingRecord
    source_revision_id: str
    atomic_sku_set_revision_id: str

    def fingerprint(self, context: PricingContextInput) -> str:
        return digest(
            {
                "identity_version": "ATOMIC_SKU_ITEM_V2",
                "atomic_sku_item_id": self.item.atomic_sku_item_id,
                "product_group_id": self.item.product_group_id,
                "composition_signature": self.item.composition_signature,
                "atomic_sku_id": self.item.atomic_sku_id,
                "atomic_sku_selection_signature": self.item.atomic_sku_selection_signature,
                "membership_revision_id": self.membership_revision_id,
                "source_binding_id": self.binding.binding_id,
                "atomic_sku_revision_member_id": self.binding.atomic_sku_revision_member_id,
                "source_revision_id": self.source_revision_id,
                "atomic_sku_set_revision_id": self.atomic_sku_set_revision_id,
                "acquisition_basis": self.binding.acquisition_basis,
                "base_purchase_cost_krw": self.binding.base_purchase_cost_krw,
                "option_additional_price_krw": self.binding.option_additional_price_krw,
                "purchase_cost_krw": self.binding.purchase_cost_krw,
                "pricing_context_fingerprint": context.fingerprint,
            }
        )


@dataclass(frozen=True)
class AtomicPricingSnapshotRecord:
    pricing_snapshot_id: str
    atomic_sku_item_id: str
    product_group_id: str
    composition_signature: str
    atomic_sku_id: str
    atomic_sku_selection_signature: str
    marketplace_key: str
    account_id: str | None
    fee_table_version: str
    pricing_policy_version: str
    pricing_context_fingerprint: str
    membership_revision_id: str
    source_binding_id: str
    atomic_sku_revision_member_id: str
    source_product_facts_revision_id: str
    dependency_fingerprint: str
    pricing_rule_version: str
    purchase_cost_krw: int
    supplier_shipping_krw: int
    minimum_sale_price_krw: int | None
    platform_fee_krw: int
    other_policy_cost_krw: int
    target_margin_price_krw: int
    final_sale_price_krw: int
    price_basis: PriceBasis
    expected_net_profit_krw: int
    expected_net_margin: Fraction
    expected_net_margin_bp: int
    price_guard: PriceGuard
    guard_reasons: tuple[GuardReason, ...]
    created_at: datetime


@dataclass(frozen=True)
class AtomicPricingMove:
    move_id: str
    atomic_sku_item_id: str
    pricing_context_fingerprint: str
    sequence: int
    pricing_snapshot_id: str
    previous_pricing_snapshot_id: str | None
    reason: PricingMoveReason


class AtomicSKUEconomicsUnit:
    """One caller-owned transaction over v2 economics. It never commits itself."""

    def __init__(self, session: Session, clock: Clock) -> None:
        self.session = session
        self._clock = clock

    def item(self, atomic_sku_item_id: str) -> AtomicSKUItemRecord | None:
        row = self.session.get(AtomicSKUProductItem, atomic_sku_item_id)
        return None if row is None else _item(row)

    def current_binding(self, atomic_sku_item_id: str) -> AtomicSourceBindingRecord | None:
        row = self.session.scalars(
            select(AtomicSKUSourceBinding).where(
                AtomicSKUSourceBinding.atomic_sku_item_id == atomic_sku_item_id,
                AtomicSKUSourceBinding.valid_to.is_(None),
            )
        ).first()
        return None if row is None else _binding(row)

    def binding(self, binding_id: str) -> AtomicSourceBindingRecord | None:
        row = self.session.get(AtomicSKUSourceBinding, binding_id)
        return None if row is None else _binding(row)

    def bind_source_configuration(
        self,
        atomic_sku_item_id: str,
        *,
        decided_by: str,
        correlation_id: str,
    ) -> AtomicSourceBindingRecord:
        decided_by = _required("decided_by", decided_by)
        correlation_id = _required("correlation_id", correlation_id)
        item_row = self.session.get(AtomicSKUProductItem, atomic_sku_item_id)
        if item_row is None:
            raise NotFoundError("PRODUCTS_ATOMIC_SKU_ITEM_UNKNOWN", "no AtomicSKU Item has that id")
        group = self.session.get(ProductGroup, item_row.product_group_id)
        if group is None or group.status != GroupStatus.ACTIVE.value:
            raise InputValidationError(
                "PRODUCTS_GROUP_NOT_ACTIVE",
                "an AtomicSKU source binding requires an active ProductGroup",
            )
        if item_row.composition_signature != DEFAULT_SINGLE_UNIT_SIGNATURE:
            raise InputValidationError(
                "PRODUCTS_ATOMIC_SKU_ACQUISITION_NOT_EXACT",
                "an option delta prices only the exact single source configuration; composed"
                " fulfillment is not inferred",
            )
        current_set = AtomicSKUStore.current_for_use_row(self.session, item_row.product_group_id)
        if current_set is None:
            raise InputValidationError(
                "PRODUCTS_ATOMIC_SKU_NOT_CURRENT",
                "the AtomicSKU set or one of its reviewed source dependencies is stale",
            )
        revision_member = self.session.scalars(
            select(AtomicSKURevisionMember).where(
                AtomicSKURevisionMember.sku_set_revision_id == current_set.sku_set_revision_id,
                AtomicSKURevisionMember.atomic_sku_id == item_row.atomic_sku_id,
            )
        ).one_or_none()
        if revision_member is None:
            raise InputValidationError(
                "PRODUCTS_ATOMIC_SKU_NOT_CURRENT", "the Item is not in the current AtomicSKU set"
            )
        fact_revision = self.session.get(ProductFactsRevision, revision_member.source_revision_id)
        if fact_revision is None:  # pragma: no cover - foreign keys make this unreachable
            raise NotFoundError(
                "COLLECT_REVISION_UNKNOWN", "the AtomicSKU proof revision is missing"
            )
        foundation = ProductFoundationUnit(self.session, self._clock)
        source = foundation.source_product(
            fact_revision.supplier_key, fact_revision.source_product_id
        )
        membership = foundation.confirmed_membership(source.source_product_uid)
        if membership is None or membership.product_group_id != item_row.product_group_id:
            raise InputValidationError(
                "PRODUCTS_ATOMIC_SKU_SOURCE_NOT_MEMBER",
                "the proving source product is not a confirmed member",
            )
        current_move = foundation.current_move(source.source_product_uid)
        if current_move is None or current_move.revision_id != fact_revision.revision_id:
            raise InputValidationError(
                "PRODUCTS_ATOMIC_SKU_SOURCE_STALE", "the proving source revision is not current"
            )
        prices_row = self.session.get(ProductFactsField, (fact_revision.revision_id, "prices"))
        prices = (
            None
            if prices_row is None or prices_row.status != FieldStatus.CONFIRMED.value
            else value_from_json("prices", prices_row.value_json)
        )
        if not isinstance(prices, PricesValue) or len(prices.prices) != 1:
            raise InputValidationError(
                "PRODUCTS_ATOMIC_SKU_BASE_PRICE_UNRESOLVED",
                "one exact confirmed base purchase price is required",
            )
        configuration = OptionConfiguration.model_validate_json(
            revision_member.source_configuration_json
        )
        if configuration.additional_price_krw is None:
            raise InputValidationError(
                "PRODUCTS_ATOMIC_SKU_OPTION_DELTA_UNRESOLVED",
                "the source configuration must state its option price delta explicitly",
            )
        base_cost = prices.prices[0].amount_krw
        purchase_cost = base_cost + configuration.additional_price_krw
        if purchase_cost < 0:
            raise InputValidationError(
                "PRODUCTS_ATOMIC_SKU_PURCHASE_COST_INVALID",
                "the exact base price plus option delta cannot be negative",
            )
        current = self.current_binding(atomic_sku_item_id)
        if current is not None and (
            current.atomic_sku_revision_member_id == revision_member.revision_member_id
            and current.group_member_id == membership.member_id
            and current.provenance_revision_id == fact_revision.revision_id
            and current.acquisition_basis == ATOMIC_ACQUISITION_BASIS
            and current.base_purchase_cost_krw == base_cost
            and current.option_additional_price_krw == configuration.additional_price_krw
            and current.purchase_cost_krw == purchase_cost
        ):
            return current
        now = self._clock.now()
        for open_row in self.session.scalars(
            select(AtomicSKUSourceBinding).where(
                AtomicSKUSourceBinding.atomic_sku_item_id == atomic_sku_item_id,
                AtomicSKUSourceBinding.valid_to.is_(None),
            )
        ):
            open_row.valid_to = now
        self.session.flush()
        row = AtomicSKUSourceBinding(
            binding_id=str(uuid.uuid4()),
            atomic_sku_item_id=atomic_sku_item_id,
            atomic_sku_revision_member_id=revision_member.revision_member_id,
            group_member_id=membership.member_id,
            provenance_revision_id=fact_revision.revision_id,
            acquisition_basis=ATOMIC_ACQUISITION_BASIS,
            base_purchase_cost_krw=base_cost,
            option_additional_price_krw=configuration.additional_price_krw,
            purchase_cost_krw=purchase_cost,
            decided_by=decided_by,
            correlation_id=correlation_id,
            valid_from=now,
            valid_to=None,
        )
        self.session.add(row)
        self.session.flush()
        return _binding(row)

    def current_move(
        self, atomic_sku_item_id: str, context_fingerprint: str
    ) -> AtomicPricingMove | None:
        row = self.session.scalars(
            select(CurrentAtomicSKUPricingSnapshotMove)
            .where(
                CurrentAtomicSKUPricingSnapshotMove.atomic_sku_item_id == atomic_sku_item_id,
                CurrentAtomicSKUPricingSnapshotMove.pricing_context_fingerprint
                == context_fingerprint,
            )
            .order_by(CurrentAtomicSKUPricingSnapshotMove.sequence.desc())
            .limit(1)
        ).first()
        return None if row is None else _move(row)

    def snapshot(self, pricing_snapshot_id: str) -> AtomicPricingSnapshotRecord | None:
        row = self.session.get(AtomicSKUPricingSnapshot, pricing_snapshot_id)
        return None if row is None else _snapshot(row)

    def record_snapshot(
        self,
        *,
        dependencies: AtomicPricingDependencies,
        context: PricingContextInput,
        inputs: SourceInputs,
        calculation: Calculation,
    ) -> AtomicPricingSnapshotRecord:
        fee_rate = context.fee_rate_value
        other_rate = context.other_cost_rate_value
        item = dependencies.item
        binding = dependencies.binding
        row = AtomicSKUPricingSnapshot(
            pricing_snapshot_id=str(uuid.uuid4()),
            atomic_sku_item_id=item.atomic_sku_item_id,
            product_group_id=item.product_group_id,
            composition_signature=item.composition_signature,
            atomic_sku_id=item.atomic_sku_id,
            atomic_sku_selection_signature=item.atomic_sku_selection_signature,
            marketplace_key=context.marketplace_key,
            account_id=context.account_id,
            fee_table_version=context.fee_table_version,
            pricing_policy_version=context.pricing_policy_version,
            pricing_context_fingerprint=context.fingerprint,
            pricing_context_json=json.dumps(context.canonical(), sort_keys=True),
            membership_revision_id=dependencies.membership_revision_id,
            source_binding_id=binding.binding_id,
            atomic_sku_revision_member_id=binding.atomic_sku_revision_member_id,
            source_product_facts_revision_id=dependencies.source_revision_id,
            dependency_fingerprint=dependencies.fingerprint(context),
            pricing_rule_version=PRICING_RULE_VERSION,
            purchase_cost_krw=inputs.purchase_cost_krw,
            supplier_shipping_krw=inputs.supplier_shipping_krw,
            minimum_sale_price_krw=inputs.minimum_sale_price_krw,
            platform_fee_krw=calculation.platform_fee_krw,
            other_policy_cost_krw=calculation.other_policy_cost_krw,
            target_margin_price_krw=calculation.target_margin_price_krw,
            final_sale_price_krw=calculation.final_sale_price_krw,
            price_basis=calculation.price_basis.value,
            expected_net_profit_krw=calculation.expected_net_profit_krw,
            expected_net_margin_bp=calculation.expected_net_margin_bp,
            price_guard=calculation.price_guard.value,
            guard_reasons_json=json.dumps([reason.value for reason in calculation.guard_reasons]),
            fee_rate_numerator=fee_rate.numerator,
            fee_rate_denominator=fee_rate.denominator,
            fee_fixed_krw=context.fee_fixed_krw,
            other_cost_rate_numerator=other_rate.numerator,
            other_cost_rate_denominator=other_rate.denominator,
            other_cost_fixed_krw=context.other_cost_fixed_krw,
            cost_rounding=context.cost_rounding.value,
            price_rounding=context.price_rounding.value,
            target_margin_numerator=TARGET_NET_MARGIN.numerator,
            target_margin_denominator=TARGET_NET_MARGIN.denominator,
            minimum_margin_numerator=MINIMUM_NET_MARGIN.numerator,
            minimum_margin_denominator=MINIMUM_NET_MARGIN.denominator,
            created_at=self._clock.now(),
        )
        self.session.add(row)
        self.session.flush()
        return _snapshot(row)

    def record_move(
        self,
        snapshot: AtomicPricingSnapshotRecord,
        *,
        decided_by: str,
        correlation_id: str,
        rule_version: str,
    ) -> AtomicPricingMove:
        last = self.current_move(snapshot.atomic_sku_item_id, snapshot.pricing_context_fingerprint)
        row = CurrentAtomicSKUPricingSnapshotMove(
            move_id=str(uuid.uuid4()),
            atomic_sku_item_id=snapshot.atomic_sku_item_id,
            pricing_context_fingerprint=snapshot.pricing_context_fingerprint,
            sequence=1 if last is None else last.sequence + 1,
            pricing_snapshot_id=snapshot.pricing_snapshot_id,
            previous_pricing_snapshot_id=None if last is None else last.pricing_snapshot_id,
            reason=(
                PricingMoveReason.INITIAL if last is None else PricingMoveReason.REPRICED
            ).value,
            rule_version=rule_version,
            decided_by=decided_by,
            correlation_id=correlation_id,
            moved_at=self._clock.now(),
        )
        self.session.add(row)
        self.session.flush()
        return _move(row)


class AtomicSKUEconomicsStore:
    """Source-binding owner and transaction factory; it performs no price calculation."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    @contextmanager
    def transaction(self) -> Iterator[AtomicSKUEconomicsUnit]:
        with self._db.write() as session:
            yield AtomicSKUEconomicsUnit(session, self._clock)

    @contextmanager
    def reading(self) -> Iterator[AtomicSKUEconomicsUnit]:
        with self._db.read() as session:
            yield AtomicSKUEconomicsUnit(session, self._clock)

    def bind_source_configuration(
        self,
        atomic_sku_item_id: str,
        *,
        decided_by: str,
        correlation_id: str,
    ) -> AtomicSourceBindingRecord:
        with self.transaction() as unit:
            return unit.bind_source_configuration(
                atomic_sku_item_id, decided_by=decided_by, correlation_id=correlation_id
            )

    def current_binding(self, atomic_sku_item_id: str) -> AtomicSourceBindingRecord | None:
        with self.reading() as unit:
            return unit.current_binding(atomic_sku_item_id)


def _required(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        raise ValueError(f"{name} must contain 1 to 64 characters")
    return normalized


def _item(row: AtomicSKUProductItem) -> AtomicSKUItemRecord:
    return AtomicSKUItemRecord(
        row.atomic_sku_item_id,
        row.product_group_id,
        row.composition_id,
        row.composition_signature,
        row.atomic_sku_id,
        row.atomic_sku_selection_signature,
    )


def _binding(row: AtomicSKUSourceBinding) -> AtomicSourceBindingRecord:
    return AtomicSourceBindingRecord(
        row.binding_id,
        row.atomic_sku_item_id,
        row.atomic_sku_revision_member_id,
        row.group_member_id,
        row.provenance_revision_id,
        row.acquisition_basis,
        row.base_purchase_cost_krw,
        row.option_additional_price_krw,
        row.purchase_cost_krw,
        row.valid_from,
        row.valid_to,
    )


def _snapshot(row: AtomicSKUPricingSnapshot) -> AtomicPricingSnapshotRecord:
    return AtomicPricingSnapshotRecord(
        row.pricing_snapshot_id,
        row.atomic_sku_item_id,
        row.product_group_id,
        row.composition_signature,
        row.atomic_sku_id,
        row.atomic_sku_selection_signature,
        row.marketplace_key,
        row.account_id,
        row.fee_table_version,
        row.pricing_policy_version,
        row.pricing_context_fingerprint,
        row.membership_revision_id,
        row.source_binding_id,
        row.atomic_sku_revision_member_id,
        row.source_product_facts_revision_id,
        row.dependency_fingerprint,
        row.pricing_rule_version,
        row.purchase_cost_krw,
        row.supplier_shipping_krw,
        row.minimum_sale_price_krw,
        row.platform_fee_krw,
        row.other_policy_cost_krw,
        row.target_margin_price_krw,
        row.final_sale_price_krw,
        PriceBasis(row.price_basis),
        row.expected_net_profit_krw,
        Fraction(row.expected_net_profit_krw, row.final_sale_price_krw),
        row.expected_net_margin_bp,
        PriceGuard(row.price_guard),
        tuple(GuardReason(reason) for reason in json.loads(row.guard_reasons_json)),
        row.created_at,
    )


def _move(row: CurrentAtomicSKUPricingSnapshotMove) -> AtomicPricingMove:
    return AtomicPricingMove(
        row.move_id,
        row.atomic_sku_item_id,
        row.pricing_context_fingerprint,
        row.sequence,
        row.pricing_snapshot_id,
        row.previous_pricing_snapshot_id,
        PricingMoveReason(row.reason),
    )


__all__ = [
    "AtomicPricingDependencies",
    "AtomicPricingMove",
    "AtomicPricingSnapshotRecord",
    "AtomicSKUEconomicsStore",
    "AtomicSKUEconomicsUnit",
    "AtomicSourceBindingRecord",
]
