"""M4 pricing owner: one exact PricingSnapshot per Item and explicit pricing context (PR-D).

Issue #80 kickoff 5737440897, ADR-0013 §7. Only this owner calculates a selling price; no UI,
adapter or other service re-decides one.

A snapshot prices exactly the **current procurement** of an Item:
- the Item's one open binding, which must be ``BASE_PRODUCT`` (``SOURCE_OFFER`` is unavailable);
- that binding's member, CONFIRMED in the Item's group;
- the member's current source revision, which must be the binding's own provenance;
- the group's current membership revision.

When any of that does not hold, nothing is priced: stale procurement is never priced. The source
inputs then come from that revision alone (``app.products.pricing``), and the calculation from the
supplied context.

``price`` is idempotent: when the current snapshot for the Item and exact context already has the
current dependency fingerprint, nothing is written. Otherwise a new immutable snapshot, its
current-pointer move and both audit events commit in one unit of work, or none of them does.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.collect.revisions import ProductFactsRevisionStore
from app.core.clock import Clock
from app.core.correlation import get_correlation_id, new_correlation_id
from app.core.errors import NotFoundError
from app.products.model import (
    DEFAULT_SINGLE_UNIT_SIGNATURE,
    BindingKind,
    GroupStatus,
    MemberStatus,
    ReadinessStatus,
    Reason,
)
from app.products.pricing import (
    PRICING_RULE_VERSION,
    Calculation,
    PricingContextInput,
    PricingDependencies,
    SourceInputs,
    calculate,
    source_inputs,
)
from app.products.pricing_store import PricingMove, PricingSnapshotRecord, PricingUnit
from app.products.store import (
    BindingRecord,
    ItemDetail,
    MemberDetail,
    ProductFoundationStore,
    ProductFoundationUnit,
)

logger = logging.getLogger("icbm.products")

CURRENT_PRICING_RULE_VERSION = "current-pricing-rule/v1"
DECIDED_BY = "products.pricing"

# Procurement reason codes: our own, never page content.
GROUP_RETIRED = "PRODUCT_GROUP_RETIRED"
MEMBERSHIP_REVISION_MISSING = "MEMBERSHIP_REVISION_MISSING"
MEMBERSHIP_REVISION_NOT_CURRENT = "MEMBERSHIP_REVISION_NOT_CURRENT"
BINDING_MISSING = "BINDING_MISSING"
BINDING_KIND_UNPRICEABLE = "BINDING_KIND_UNPRICEABLE"
BINDING_COMPOSITION_INVALID = "BINDING_COMPOSITION_INVALID"
BINDING_MEMBER_NOT_CONFIRMED = "BINDING_MEMBER_NOT_CONFIRMED"
BINDING_PROVENANCE_STALE = "BINDING_PROVENANCE_STALE"


@dataclass(frozen=True)
class Procurement:
    """An Item's current procurement, and every reason it cannot be priced as it stands."""

    item: ItemDetail
    group_status: GroupStatus | None
    membership_revision_id: str | None
    binding: BindingRecord | None
    member: MemberDetail | None
    current_revision_id: str | None
    reasons: tuple[Reason, ...]

    @property
    def dependencies(self) -> PricingDependencies | None:
        if (
            self.reasons
            or self.binding is None
            or self.membership_revision_id is None
            or self.current_revision_id is None
        ):
            return None
        return PricingDependencies(
            item_id=self.item.item_id,
            product_group_id=self.item.product_group_id,
            composition_signature=self.item.composition_signature,
            signature_version=self.item.signature_version,
            membership_revision_id=self.membership_revision_id,
            source_binding_id=self.binding.binding_id,
            source_revision_id=self.current_revision_id,
        )


def current_procurement(unit: ProductFoundationUnit, item_id: str) -> Procurement:
    item = unit.item_detail(item_id)
    if item is None:
        raise NotFoundError("PRODUCTS_ITEM_UNKNOWN", "no Item has that identifier")
    reasons: list[Reason] = []
    group_status = unit.group_status(item.product_group_id)
    if group_status is not GroupStatus.ACTIVE:
        reasons.append(Reason(GROUP_RETIRED, ReadinessStatus.BLOCKED))
    membership = unit.current_membership_revision(item.product_group_id)
    if membership is None:
        reasons.append(Reason(MEMBERSHIP_REVISION_MISSING, ReadinessStatus.REVIEW_REQUIRED))
    elif not unit.membership_is_current(item.product_group_id):
        reasons.append(Reason(MEMBERSHIP_REVISION_NOT_CURRENT, ReadinessStatus.REVIEW_REQUIRED))
    binding = unit.open_binding_of_item(item_id)
    member = None
    current_revision = None
    if binding is None:
        reasons.append(Reason(BINDING_MISSING, ReadinessStatus.REVIEW_REQUIRED))
    else:
        if binding.binding_kind is not BindingKind.BASE_PRODUCT:
            reasons.append(Reason(BINDING_KIND_UNPRICEABLE, ReadinessStatus.REVIEW_REQUIRED))
        if item.composition_signature != DEFAULT_SINGLE_UNIT_SIGNATURE:
            reasons.append(Reason(BINDING_COMPOSITION_INVALID, ReadinessStatus.REVIEW_REQUIRED))
        member = unit.member_detail(binding.group_member_id)
        if (
            member is None
            or member.status is not MemberStatus.CONFIRMED
            or member.product_group_id != item.product_group_id
        ):
            reasons.append(Reason(BINDING_MEMBER_NOT_CONFIRMED, ReadinessStatus.REVIEW_REQUIRED))
        else:
            move = unit.current_move(member.source_product_uid)
            current_revision = None if move is None else move.revision_id
            if current_revision != binding.provenance_revision_id:
                # The binding claims a revision that is no longer its member's current one.
                reasons.append(Reason(BINDING_PROVENANCE_STALE, ReadinessStatus.STALE))
    return Procurement(
        item=item,
        group_status=group_status,
        membership_revision_id=None if membership is None else membership.membership_revision_id,
        binding=binding,
        member=member,
        current_revision_id=current_revision,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class PricingEvaluation:
    """What pricing an Item under one context would do now, and the snapshot current for them.
    Read-only: an evaluation writes nothing."""

    item_id: str
    context_fingerprint: str
    procurement: Procurement
    reasons: tuple[Reason, ...]
    dependencies: PricingDependencies | None = None
    dependency_fingerprint: str | None = None
    inputs: SourceInputs | None = None
    calculation: Calculation | None = None
    current_snapshot: PricingSnapshotRecord | None = None


class PricingOutcome(StrEnum):
    RECORDED = "RECORDED"  # a new snapshot, now current, with its audit events
    UNCHANGED = "UNCHANGED"  # the current snapshot already prices the current dependencies
    NOT_PRICED = "NOT_PRICED"  # every reason is returned; nothing was written


@dataclass(frozen=True)
class PricingResult:
    outcome: PricingOutcome
    item_id: str
    context_fingerprint: str
    snapshot: PricingSnapshotRecord | None = None
    move: PricingMove | None = None
    reasons: tuple[Reason, ...] = field(default_factory=tuple)


class ProductPricingService:
    def __init__(
        self,
        *,
        store: ProductFoundationStore,
        revisions: ProductFactsRevisionStore,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        self._store = store
        self._revisions = revisions
        self._audit = audit
        self._clock = clock

    def evaluate(self, item_id: str, context: PricingContextInput) -> PricingEvaluation:
        with self._store.reading() as unit:
            return self._evaluate(unit, item_id, context)

    def current_pricing_snapshot(
        self, item_id: str, context: PricingContextInput
    ) -> PricingSnapshotRecord | None:
        """The snapshot the pointer names for this Item and exact context, current or not in its
        dependencies; :meth:`evaluate` says whether it still prices the current state."""
        with self._store.reading() as unit:
            return self._current_snapshot(PricingUnit(unit.session, self._clock), item_id, context)

    def price(
        self, item_id: str, context: PricingContextInput, *, correlation_id: str | None = None
    ) -> PricingResult:
        correlation = correlation_id or get_correlation_id() or new_correlation_id()
        fingerprint = context.fingerprint
        with self._store.transaction() as unit:
            evaluation = self._evaluate(unit, item_id, context)
            if (
                evaluation.reasons
                or evaluation.dependencies is None
                or evaluation.inputs is None
                or evaluation.calculation is None
            ):
                return PricingResult(
                    PricingOutcome.NOT_PRICED, item_id, fingerprint, reasons=evaluation.reasons
                )
            current = evaluation.current_snapshot
            if (
                current is not None
                and current.dependency_fingerprint == evaluation.dependency_fingerprint
            ):
                return PricingResult(PricingOutcome.UNCHANGED, item_id, fingerprint, current)
            pricing = PricingUnit(unit.session, self._clock)
            snapshot = pricing.record_snapshot(
                dependencies=evaluation.dependencies,
                context=context,
                inputs=evaluation.inputs,
                calculation=evaluation.calculation,
            )
            self._audit.append(_recorded_entry(snapshot, correlation), session=unit.session)
            move = pricing.record_move(
                snapshot,
                decided_by=DECIDED_BY,
                correlation_id=correlation,
                rule_version=CURRENT_PRICING_RULE_VERSION,
            )
            self._audit.append(_moved_entry(move, correlation), session=unit.session)
        logger.info(
            "products.priced",
            extra={
                "item_id": item_id,
                "pricing_snapshot_id": snapshot.pricing_snapshot_id,
                "price_basis": snapshot.price_basis.value,
                "price_guard": snapshot.price_guard.value,
            },
        )
        return PricingResult(PricingOutcome.RECORDED, item_id, fingerprint, snapshot, move)

    # ------------------------------------------------------------------ internals

    def _current_snapshot(
        self, pricing: PricingUnit, item_id: str, context: PricingContextInput
    ) -> PricingSnapshotRecord | None:
        move = pricing.current_move(item_id, context.fingerprint)
        return None if move is None else pricing.snapshot(move.pricing_snapshot_id)

    def _evaluate(
        self, unit: ProductFoundationUnit, item_id: str, context: PricingContextInput
    ) -> PricingEvaluation:
        procurement = current_procurement(unit, item_id)
        current = self._current_snapshot(PricingUnit(unit.session, self._clock), item_id, context)
        base = PricingEvaluation(
            item_id=item_id,
            context_fingerprint=context.fingerprint,
            procurement=procurement,
            reasons=procurement.reasons,
            current_snapshot=current,
        )
        dependencies = procurement.dependencies
        if dependencies is None:
            return base
        stored = self._revisions.get(dependencies.source_revision_id)
        if stored is None:  # pragma: no cover - a foreign key guarantees the revision
            raise NotFoundError("COLLECT_REVISION_UNKNOWN", "the bound revision is missing")
        inputs = source_inputs(stored.fields)
        if isinstance(inputs, tuple):
            return _with(base, reasons=inputs)
        calculation = calculate(inputs, context)
        if isinstance(calculation, Reason):
            return _with(base, reasons=(calculation,))
        return PricingEvaluation(
            item_id=item_id,
            context_fingerprint=context.fingerprint,
            procurement=procurement,
            reasons=(),
            dependencies=dependencies,
            dependency_fingerprint=dependencies.fingerprint(context),
            inputs=inputs,
            calculation=calculation,
            current_snapshot=current,
        )


def _with(evaluation: PricingEvaluation, *, reasons: Sequence[Reason]) -> PricingEvaluation:
    return PricingEvaluation(
        item_id=evaluation.item_id,
        context_fingerprint=evaluation.context_fingerprint,
        procurement=evaluation.procurement,
        reasons=tuple(reasons),
        current_snapshot=evaluation.current_snapshot,
    )


def _recorded_entry(snapshot: PricingSnapshotRecord, correlation: str) -> AuditEntry:
    """Identifiers, versions, enums, fingerprints and the calculated amounts; no page text."""
    return AuditEntry(
        event_type=AuditEventType.PRODUCT_PRICING_SNAPSHOT_RECORDED,
        action="pricing_snapshot.record",
        actor=DECIDED_BY,
        outcome=AuditOutcome.RECORDED,
        target_ref=snapshot.pricing_snapshot_id,
        reason_code=snapshot.price_guard.value,
        details={
            "item_id": snapshot.item_id,
            "product_group_id": snapshot.product_group_id,
            "marketplace_key": snapshot.marketplace_key,
            "account_id": snapshot.account_id,
            "fee_table_version": snapshot.fee_table_version,
            "pricing_policy_version": snapshot.pricing_policy_version,
            "pricing_context_fingerprint": snapshot.pricing_context_fingerprint,
            "pricing_rule_version": PRICING_RULE_VERSION,
            "dependency_fingerprint": snapshot.dependency_fingerprint,
            "membership_revision_id": snapshot.membership_revision_id,
            "source_binding_id": snapshot.source_binding_id,
            "source_product_facts_revision_id": snapshot.source_product_facts_revision_id,
            "price_basis": snapshot.price_basis.value,
            "guard_reasons": [reason.value for reason in snapshot.guard_reasons],
            "purchase_cost_krw": snapshot.purchase_cost_krw,
            "supplier_shipping_krw": snapshot.supplier_shipping_krw,
            "minimum_sale_price_krw": snapshot.minimum_sale_price_krw,
            "platform_fee_krw": snapshot.platform_fee_krw,
            "other_policy_cost_krw": snapshot.other_policy_cost_krw,
            "target_margin_price_krw": snapshot.target_margin_price_krw,
            "final_sale_price_krw": snapshot.final_sale_price_krw,
            "expected_net_profit_krw": snapshot.expected_net_profit_krw,
            "expected_net_margin_bp": snapshot.expected_net_margin_bp,
        },
        correlation_id=correlation,
    )


def _moved_entry(move: PricingMove, correlation: str) -> AuditEntry:
    return AuditEntry(
        event_type=AuditEventType.PRODUCT_CURRENT_PRICING_SNAPSHOT_MOVED,
        action="current_pricing_snapshot.move",
        actor=DECIDED_BY,
        outcome=AuditOutcome.RECORDED,
        target_ref=move.item_id,
        reason_code=move.reason.value,
        before=None
        if move.previous_pricing_snapshot_id is None
        else {"pricing_snapshot_id": move.previous_pricing_snapshot_id},
        after={"pricing_snapshot_id": move.pricing_snapshot_id, "sequence": move.sequence},
        details={
            "move_id": move.move_id,
            "pricing_context_fingerprint": move.pricing_context_fingerprint,
            "rule_version": CURRENT_PRICING_RULE_VERSION,
        },
        correlation_id=correlation,
    )
