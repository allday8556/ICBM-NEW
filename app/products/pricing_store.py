"""PRODUCT DB persistence for M4 pricing (Issue #80 PR-D, ADR-0013 §7).

It writes and reads pricing snapshots and their current-snapshot pointer history inside a caller's
unit of work, and decides nothing: the pricing service calculates and chooses; migration 0013
refuses any snapshot that does not price the current state it names, and any pointer move out of
order.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.products.models import CurrentPricingSnapshotMove, PricingSnapshot
from app.products.pricing import (
    MINIMUM_NET_MARGIN,
    PRICING_RULE_VERSION,
    TARGET_NET_MARGIN,
    Calculation,
    GuardReason,
    PriceBasis,
    PriceGuard,
    PricingContextInput,
    PricingDependencies,
    PricingMoveReason,
    SourceInputs,
)


@dataclass(frozen=True)
class PricingSnapshotRecord:
    """One immutable snapshot, as the database holds it."""

    pricing_snapshot_id: str
    item_id: str
    product_group_id: str
    composition_signature: str
    marketplace_key: str
    account_id: str | None
    fee_table_version: str
    pricing_policy_version: str
    pricing_context_fingerprint: str
    membership_revision_id: str
    source_binding_id: str
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
class PricingMove:
    move_id: str
    item_id: str
    pricing_context_fingerprint: str
    sequence: int
    pricing_snapshot_id: str
    previous_pricing_snapshot_id: str | None
    reason: PricingMoveReason


class PricingUnit:
    """Pricing writes and reads over one caller-owned session. It never commits."""

    def __init__(self, session: Session, clock: Clock) -> None:
        self.session = session
        self._clock = clock

    def current_move(self, item_id: str, context_fingerprint: str) -> PricingMove | None:
        row = self.session.scalars(
            select(CurrentPricingSnapshotMove)
            .where(
                CurrentPricingSnapshotMove.item_id == item_id,
                CurrentPricingSnapshotMove.pricing_context_fingerprint == context_fingerprint,
            )
            .order_by(CurrentPricingSnapshotMove.sequence.desc())
            .limit(1)
        ).first()
        return None if row is None else _move(row)

    def snapshot(self, pricing_snapshot_id: str) -> PricingSnapshotRecord | None:
        row = self.session.get(PricingSnapshot, pricing_snapshot_id)
        return None if row is None else _snapshot(row)

    def record_snapshot(
        self,
        *,
        dependencies: PricingDependencies,
        context: PricingContextInput,
        inputs: SourceInputs,
        calculation: Calculation,
    ) -> PricingSnapshotRecord:
        fee_rate = context.fee_rate_value
        other_rate = context.other_cost_rate_value
        row = PricingSnapshot(
            pricing_snapshot_id=str(uuid.uuid4()),
            item_id=dependencies.item_id,
            product_group_id=dependencies.product_group_id,
            composition_signature=dependencies.composition_signature,
            marketplace_key=context.marketplace_key,
            account_id=context.account_id,
            fee_table_version=context.fee_table_version,
            pricing_policy_version=context.pricing_policy_version,
            pricing_context_fingerprint=context.fingerprint,
            pricing_context_json=json.dumps(context.canonical(), sort_keys=True),
            membership_revision_id=dependencies.membership_revision_id,
            source_binding_id=dependencies.source_binding_id,
            source_product_facts_revision_id=dependencies.source_revision_id,
            dependency_fingerprint=dependencies.fingerprint(context),
            pricing_rule_version=PRICING_RULE_VERSION,
            purchase_cost_krw=inputs.purchase_cost_krw,
            supplier_shipping_krw=inputs.supplier_shipping_krw,
            minimum_sale_price_krw=inputs.minimum_sale_price_krw,
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
            platform_fee_krw=calculation.platform_fee_krw,
            other_policy_cost_krw=calculation.other_policy_cost_krw,
            target_margin_price_krw=calculation.target_margin_price_krw,
            final_sale_price_krw=calculation.final_sale_price_krw,
            price_basis=calculation.price_basis.value,
            expected_net_profit_krw=calculation.expected_net_profit_krw,
            expected_net_margin_bp=calculation.expected_net_margin_bp,
            price_guard=calculation.price_guard.value,
            guard_reasons_json=json.dumps([reason.value for reason in calculation.guard_reasons]),
            created_at=self._clock.now(),
        )
        self.session.add(row)
        self.session.flush()
        return _snapshot(row)

    def record_move(
        self,
        snapshot: PricingSnapshotRecord,
        *,
        decided_by: str,
        correlation_id: str,
        rule_version: str,
    ) -> PricingMove:
        last = self.current_move(snapshot.item_id, snapshot.pricing_context_fingerprint)
        row = CurrentPricingSnapshotMove(
            move_id=str(uuid.uuid4()),
            item_id=snapshot.item_id,
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


def _snapshot(row: PricingSnapshot) -> PricingSnapshotRecord:
    return PricingSnapshotRecord(
        pricing_snapshot_id=row.pricing_snapshot_id,
        item_id=row.item_id,
        product_group_id=row.product_group_id,
        composition_signature=row.composition_signature,
        marketplace_key=row.marketplace_key,
        account_id=row.account_id,
        fee_table_version=row.fee_table_version,
        pricing_policy_version=row.pricing_policy_version,
        pricing_context_fingerprint=row.pricing_context_fingerprint,
        membership_revision_id=row.membership_revision_id,
        source_binding_id=row.source_binding_id,
        source_product_facts_revision_id=row.source_product_facts_revision_id,
        dependency_fingerprint=row.dependency_fingerprint,
        pricing_rule_version=row.pricing_rule_version,
        purchase_cost_krw=row.purchase_cost_krw,
        supplier_shipping_krw=row.supplier_shipping_krw,
        minimum_sale_price_krw=row.minimum_sale_price_krw,
        platform_fee_krw=row.platform_fee_krw,
        other_policy_cost_krw=row.other_policy_cost_krw,
        target_margin_price_krw=row.target_margin_price_krw,
        final_sale_price_krw=row.final_sale_price_krw,
        price_basis=PriceBasis(row.price_basis),
        expected_net_profit_krw=row.expected_net_profit_krw,
        expected_net_margin=Fraction(row.expected_net_profit_krw, row.final_sale_price_krw),
        expected_net_margin_bp=row.expected_net_margin_bp,
        price_guard=PriceGuard(row.price_guard),
        guard_reasons=tuple(GuardReason(r) for r in json.loads(row.guard_reasons_json)),
        created_at=row.created_at,
    )


def _move(row: CurrentPricingSnapshotMove) -> PricingMove:
    return PricingMove(
        row.move_id,
        row.item_id,
        row.pricing_context_fingerprint,
        row.sequence,
        row.pricing_snapshot_id,
        row.previous_pricing_snapshot_id,
        PricingMoveReason(row.reason),
    )
