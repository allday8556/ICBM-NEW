"""M4 product readiness: base and per-context pricing, derived and never stored (ADR-0013 §8).

Issue #80 PR-D (kickoff 5737440897). This is PRODUCT-domain readiness. It is not
``app.system.readiness``, which is application and process health.

Two separate layers, never merged into one Item readiness:
- **Base readiness** — one Item, context-free: group, membership, binding, the bound current source
  revision's CORE facts, stock, and the image state. A COVERAGE field is never a base gate:
  shipping and the minimum sale price belong to pricing readiness, and notices, category and
  platform-dependent coverage to M5's registration preflight, per target.
- **Pricing readiness** — one Item under one exact pricing context: the current procurement, the
  source pricing inputs, and the current PricingSnapshot for that Item and context.

Each evaluation returns one status, **every** applicable reason, its rule version and a dependency
fingerprint. Within one evaluation the status is the highest of
``BLOCKED > DUPLICATE > STALE > REVIEW_REQUIRED > READY`` (ruling D). Nothing here writes, and no
``REGISTERABLE`` or ``ready`` value exists anywhere: M5's registration preflight combines these
layers with the marketplace/account checks, and until it exists nothing is a registration
candidate.

**Images.** The derived-image selection and its artifact-bound QA (ADR-0013 §9) arrive in PR-E.
Until then base readiness can never be READY: ``IMAGE_SELECTION_QA_PENDING`` is always a
REVIEW_REQUIRED reason, rather than a silent pass.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.collect.facts import Availability, FieldLevel, FieldStatus, StockValue
from app.collect.revisions import ProductFactsRevisionStore
from app.products.model import (
    READINESS_PRECEDENCE,
    ReadinessStatus,
    Reason,
    precedence_status,
)
from app.products.pricing import PriceGuard, PricingContextInput, digest
from app.products.pricing_service import (
    Procurement,
    ProductPricingService,
    current_procurement,
)
from app.products.store import ProductFoundationStore

BASE_READINESS_RULE_VERSION = "base-readiness/v1"
PRICING_READINESS_RULE_VERSION = "pricing-readiness/v1"

# Reason codes: our own, never page content.
GROUP_CANDIDATE_PENDING = "GROUP_MEMBER_CANDIDATE_PENDING"
SOURCE_CORE_FIELD_REVIEW_REQUIRED = "SOURCE_CORE_FIELD_REVIEW_REQUIRED"
SOURCE_CORE_FIELD_ABSENT = "SOURCE_CORE_FIELD_ABSENT"
SOURCE_STOCK_SOLD_OUT = "SOURCE_STOCK_SOLD_OUT"
IMAGE_SELECTION_QA_PENDING = "IMAGE_SELECTION_QA_PENDING"
PRICING_SNAPSHOT_MISSING = "PRICING_SNAPSHOT_MISSING"
PRICING_SNAPSHOT_SUPERSEDED = "PRICING_SNAPSHOT_SUPERSEDED"
# ABSENT is a legitimate reading of these (M3 capability boundary): never a failure by itself.
_ABSENCE_ALLOWED = frozenset({"options", "quantity_tiers"})
# The image-selection owner of ADR-0013 §9 does not exist until PR-E.
_IMAGE_STATE = "SELECTION_QA_PENDING"


class ReadinessLayer(StrEnum):
    BASE = "BASE"
    PRICING = "PRICING"


@dataclass(frozen=True)
class Readiness:
    """One derived evaluation. ``pricing_context_fingerprint`` is set only on the pricing layer:
    a pricing result belongs to exactly one context and says so."""

    layer: ReadinessLayer
    item_id: str
    status: ReadinessStatus
    reasons: tuple[Reason, ...]
    rule_version: str
    dependency_fingerprint: str
    pricing_context_fingerprint: str | None = None


def _ordered(reasons: list[Reason]) -> tuple[Reason, ...]:
    rank = {status: index for index, status in enumerate(READINESS_PRECEDENCE)}
    unique = {(reason.code, reason.subject): reason for reason in reasons}
    return tuple(sorted(unique.values(), key=lambda r: (rank[r.status], r.code, r.subject or "")))


def _procurement_state(procurement: Procurement) -> dict[str, object]:
    return {
        "item_id": procurement.item.item_id,
        "product_group_id": procurement.item.product_group_id,
        "composition_signature": procurement.item.composition_signature,
        "group_status": None
        if procurement.group_status is None
        else procurement.group_status.value,
        "membership_revision_id": procurement.membership_revision_id,
        "binding_id": None if procurement.binding is None else procurement.binding.binding_id,
        "binding_provenance_revision_id": None
        if procurement.binding is None
        else procurement.binding.provenance_revision_id,
        "current_source_revision_id": procurement.current_revision_id,
    }


class ProductReadinessService:
    def __init__(
        self,
        *,
        store: ProductFoundationStore,
        revisions: ProductFactsRevisionStore,
        pricing: ProductPricingService,
    ) -> None:
        self._store = store
        self._revisions = revisions
        self._pricing = pricing

    def base_readiness(self, item_id: str) -> Readiness:
        with self._store.reading() as unit:
            procurement = current_procurement(unit, item_id)
            candidates = unit.pending_candidates(procurement.item.product_group_id)
            membership_current = unit.membership_is_current(procurement.item.product_group_id)
        reasons = list(procurement.reasons)
        if candidates:
            reasons.append(Reason(GROUP_CANDIDATE_PENDING, ReadinessStatus.REVIEW_REQUIRED))
        if procurement.binding is not None and procurement.current_revision_id is not None:
            reasons.extend(self._source_fact_reasons(procurement.current_revision_id))
        reasons.append(
            Reason(IMAGE_SELECTION_QA_PENDING, ReadinessStatus.REVIEW_REQUIRED, "images")
        )
        ordered = _ordered(reasons)
        return Readiness(
            layer=ReadinessLayer.BASE,
            item_id=item_id,
            status=precedence_status(ordered),
            reasons=ordered,
            rule_version=BASE_READINESS_RULE_VERSION,
            dependency_fingerprint=digest(
                {
                    "layer": ReadinessLayer.BASE.value,
                    "rule_version": BASE_READINESS_RULE_VERSION,
                    **_procurement_state(procurement),
                    "membership_current": membership_current,
                    "pending_candidates": candidates,
                    "image_state": _IMAGE_STATE,
                }
            ),
        )

    def pricing_readiness(self, item_id: str, context: PricingContextInput) -> Readiness:
        evaluation = self._pricing.evaluate(item_id, context)
        reasons = list(evaluation.reasons)
        current = evaluation.current_snapshot
        if not reasons:
            # Otherwise priceable: what remains is whether the current snapshot prices it.
            if current is None:
                reasons.append(Reason(PRICING_SNAPSHOT_MISSING, ReadinessStatus.STALE))
            elif current.dependency_fingerprint != evaluation.dependency_fingerprint:
                reasons.append(Reason(PRICING_SNAPSHOT_SUPERSEDED, ReadinessStatus.STALE))
            elif current.price_guard is not PriceGuard.OK:
                reasons.extend(
                    Reason(guard.value, ReadinessStatus.BLOCKED) for guard in current.guard_reasons
                )
        ordered = _ordered(reasons)
        return Readiness(
            layer=ReadinessLayer.PRICING,
            item_id=item_id,
            status=precedence_status(ordered),
            reasons=ordered,
            rule_version=PRICING_READINESS_RULE_VERSION,
            pricing_context_fingerprint=context.fingerprint,
            dependency_fingerprint=digest(
                {
                    "layer": ReadinessLayer.PRICING.value,
                    "rule_version": PRICING_READINESS_RULE_VERSION,
                    **_procurement_state(evaluation.procurement),
                    "pricing_context_fingerprint": context.fingerprint,
                    "pricing_dependency_fingerprint": evaluation.dependency_fingerprint,
                    "current_pricing_snapshot_id": None
                    if current is None
                    else current.pricing_snapshot_id,
                }
            ),
        )

    def _source_fact_reasons(self, revision_id: str) -> list[Reason]:
        stored = self._revisions.get(revision_id)
        if stored is None:  # pragma: no cover - a foreign key guarantees the revision
            return [Reason(SOURCE_CORE_FIELD_ABSENT, ReadinessStatus.REVIEW_REQUIRED, "revision")]
        reasons = []
        # CORE fields only (PR #84 review 5253693574). A COVERAGE field is not a universal base
        # gate: shipping and the minimum sale price are pricing readiness's inputs, and notices,
        # category and platform-dependent coverage are M5 preflight's, per target.
        for key, field in stored.fields.items():
            if field.level is not FieldLevel.CORE:
                continue
            if field.status is FieldStatus.REVIEW_REQUIRED:
                reasons.append(
                    Reason(SOURCE_CORE_FIELD_REVIEW_REQUIRED, ReadinessStatus.REVIEW_REQUIRED, key)
                )
            elif field.status is FieldStatus.ABSENT and key not in _ABSENCE_ALLOWED:
                reasons.append(
                    Reason(SOURCE_CORE_FIELD_ABSENT, ReadinessStatus.REVIEW_REQUIRED, key)
                )
        stock = stored.fields.get("stock")
        if (
            stock is not None
            and stock.status is FieldStatus.CONFIRMED
            and isinstance(stock.value, StockValue)
            and stock.value.availability is Availability.SOLD_OUT
        ):
            reasons.append(Reason(SOURCE_STOCK_SOLD_OUT, ReadinessStatus.BLOCKED, "stock"))
        return reasons
