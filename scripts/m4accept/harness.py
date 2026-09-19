"""The M4 offline acceptance run (Issue #80 PR-F, kickoff 5739459941).

One invocation, one fresh dedicated root, one exact clean checkout, no provider capability. The run
proceeds in order:
1. Refuse a root that is not fresh and dedicated, then a checkout that is not exactly its commit
   (``checkout.py``). Only then claim the root, own its data directory and migrate to head.
2. **S1 BASE_PRODUCT, revision R1.**
   - Current source revision, Product, member, default Item and BASE_PRODUCT binding.
   - A context-specific snapshot whose final price is the stated minimum sale price.
   - A completed derivation, an operator selection and exact-binary QA, reaching READY.
3. **Restart.** Close every owner, reopen the same root, read everything back unchanged, and show
   that replays write nothing.
4. **S1 R2.**
   - Same identities, R1 untouched, the binding replaced, the old snapshot and image decisions
     kept but no longer current (STALE).
   - An explicit refresh reaches READY again.
5. **S2 QuantityOffer, revision Q1.**
   - Three exact offers, three Items and three SOURCE_OFFER bindings, priced from the offer totals.
   - Each Item needs its own selection (REVIEW_REQUIRED until then).
6. **S2 Q2.** The same Items, new offers and bindings, the old history kept, pricing and images
   STALE, then refreshed.
7. **S3.** SOLD_OUT stock and a minimum sale price that is a loss: BLOCKED.
8. **Final restart.** The full read-back matches durable state, and replays write nothing.
9. **Evidence.** The checkout measured again, hard-zero, boundary and immutability evidence, and
   the sanitized, digested report.

Every canonical row is written by a production owner; the harness never writes SQL. A check that
fails is a problem, and any problem fails the run.
"""

import traceback
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.audit.models import AuditEventType
from app.collect.facts import ImageRole
from app.db.migrate import head_revision
from app.products.image_model import (
    CompletedDerivation,
    DerivationInput,
    ExecutionClass,
    ImageAssetKind,
    OperationOutcome,
    OperationRecord,
    QaVerdict,
    SelectedOutput,
    SourceDecision,
    SourceDecisionKind,
)
from app.products.images import (
    IMAGE_DERIVATION_STALE,
    IMAGE_QA_MISSING,
    IMAGE_QA_STALE,
    IMAGE_SELECTION_MISSING,
    IMAGE_SELECTION_STALE,
)
from app.products.materialization import MaterializationStatus
from app.products.model import (
    DEFAULT_SINGLE_UNIT_SIGNATURE,
    BindingKind,
    CompositionSpec,
    MoveReason,
    ReadinessStatus,
    composition_signature,
)
from app.products.pricing import (
    PriceBasis,
    PriceGuard,
    PricingContextInput,
    Rounding,
)
from app.products.pricing_service import PricingOutcome
from app.products.quantity import QuantityOfferEvidence
from app.products.readiness import (
    PRICING_SNAPSHOT_SUPERSEDED,
    SOURCE_STOCK_SOLD_OUT,
    Readiness,
)
from scripts.m4accept import evidence
from scripts.m4accept.checkout import CheckoutRefused, probe_checkout
from scripts.m4accept.guards import GuardEvidence, offline
from scripts.m4accept.owners import Owners, open_owners
from scripts.m4accept.root import DATA, RootRefused, claim_root, root_problems, settle_root
from scripts.m4accept.synthetic import SUPPLIER, Facts, png, record_revision, sha256

# One explicit, invented pricing context. No marketplace fee is known to the repository.
CONTEXT = PricingContextInput(
    marketplace_key="m4_market",
    account_id=None,
    fee_table_version="m4-acceptance-fee-v1",
    pricing_policy_version="m4-acceptance-policy-v1",
    fee_rate="0.1",
    fee_fixed_krw=0,
    other_cost_rate="0",
    other_cost_fixed_krw=0,
    cost_rounding=Rounding.CEIL_KRW_1,
    price_rounding=Rounding.CEIL_KRW_1,
)
OPERATOR = "m4-acceptance-operator"
QA = "m4-acceptance-qa"
TRANSFORM_VERSION = "m4-acceptance-transform-v1"
EXECUTED = datetime(2026, 9, 19, 1, tzinfo=UTC)

S1, S2, S3 = "m4-s1-base", "m4-s2-quantity", "m4-s3-blocked"
S1_R1 = Facts(prices=(10000,), shipping_fee=2500, minimum=18000, tiers=None)
S1_R2 = Facts(prices=(11000,), shipping_fee=2500, minimum=18000, tiers=None)
# The generic prices of S2 can equal no tier total, no multiple and no quotient of one.
S2_PRICES = (11111, 33333)
S2_Q1 = Facts(S2_PRICES, None, None, ((1, 19900), (2, 37900), (3, 53900)))
S2_Q2 = Facts(S2_PRICES, None, None, ((1, 19900), (2, 36900), (3, 52900)))
S3_R1 = Facts(prices=(10000,), shipping_fee=2500, minimum=11000, tiers=None, sold_out=True)


@dataclass(frozen=True)
class Expected:
    """One exact pricing result under ``CONTEXT``, fixed by hand (not by the code under test)."""

    purchase: int
    shipping: int
    minimum: int | None
    target: int
    final: int
    basis: PriceBasis
    fee: int
    profit: int
    margin_bp: int
    guard: PriceGuard
    reasons: tuple[str, ...] = ()


# The target is the least whole KRW with a net margin of at least 35% after the 10% fee; the
# final price is the minimum sale price whenever one exists (CLAUDE.md §6.1).
S1_R1_PRICE = Expected(
    10000, 2500, 18000, 22728, 18000, PriceBasis.MINIMUM_SALE_PRICE, 1800, 3700, 2055, PriceGuard.OK
)
S1_R2_PRICE = Expected(
    11000, 2500, 18000, 24547, 18000, PriceBasis.MINIMUM_SALE_PRICE, 1800, 2700, 1500, PriceGuard.OK
)
Q1_PRICES = {
    1: Expected(
        19900, 0, None, 36184, 36184, PriceBasis.TARGET_MARGIN, 3619, 12665, 3500, PriceGuard.OK
    ),
    2: Expected(
        37900, 0, None, 68910, 68910, PriceBasis.TARGET_MARGIN, 6891, 24119, 3500, PriceGuard.OK
    ),
    3: Expected(
        53900, 0, None, 98000, 98000, PriceBasis.TARGET_MARGIN, 9800, 34300, 3500, PriceGuard.OK
    ),
}
Q2_PRICES = {
    1: Q1_PRICES[1],
    2: Expected(
        36900, 0, None, 67093, 67093, PriceBasis.TARGET_MARGIN, 6710, 23483, 3500, PriceGuard.OK
    ),
    3: Expected(
        52900, 0, None, 96184, 96184, PriceBasis.TARGET_MARGIN, 9619, 33665, 3500, PriceGuard.OK
    ),
}
S3_PRICE = Expected(
    10000, 2500, 11000, 22728, 11000, PriceBasis.MINIMUM_SALE_PRICE, 1100, -2600, -2364,
    PriceGuard.LOSS, ("PRICE_LOSS", "PRICE_BELOW_MIN_MARGIN"),
)  # fmt: skip

S1_IMAGES = (
    (ImageRole.REPRESENTATIVE, png("m4-s1-representative")),
    (ImageRole.DETAIL, png("m4-s1-detail")),
)
S1_DERIVED = png("m4-s1-derived-frame", 64, 64)
S2_IMAGES = (
    (ImageRole.REPRESENTATIVE, png("m4-s2-representative")),
    (ImageRole.DETAIL, png("m4-s2-detail")),
)
S3_IMAGES = ((ImageRole.REPRESENTATIVE, png("m4-s3-representative")),)
# Audit event families no M4 acceptance run may produce: supplier CONNECT and marketplace.
PROVIDER_AUDIT = tuple(
    t.value for t in AuditEventType if t.value.startswith(("SUPPLIER_", "MARKETPLACE_"))
)


class Failed(Exception):
    """A phase could not go on: the checks already recorded say why."""


@dataclass
class Checks:
    items: list[dict[str, object]] = field(default_factory=list)

    def check(self, name: str, passed: bool, **observed: object) -> bool:
        entry: dict[str, object] = {"name": name, "passed": bool(passed)}
        if observed:
            entry["observed"] = observed
        self.items.append(entry)
        return bool(passed)

    def require(self, name: str, passed: bool, **observed: object) -> None:
        if not self.check(name, passed, **observed):
            raise Failed(name)

    @property
    def problems(self) -> list[str]:
        return [str(item["name"]) for item in self.items if not item["passed"]]


@dataclass
class Tracked:
    """The identities a later phase, a restart or the report must find again."""

    groups: dict[str, str] = field(default_factory=dict)  # scenario → product group
    sources: dict[str, str] = field(default_factory=dict)  # scenario → source product uid
    revisions: dict[str, str] = field(default_factory=dict)  # label → revision id
    items: dict[str, str] = field(default_factory=dict)  # label → Item id
    derivations: dict[str, str] = field(default_factory=dict)  # label → derivation id


@dataclass
class Run:
    root: Path
    owners: Owners
    checks: Checks = field(default_factory=Checks)
    tracked: Tracked = field(default_factory=Tracked)

    @property
    def data_dir(self) -> Path:
        return self.root / DATA

    def restart(self) -> None:
        self.owners.close()
        self.owners = open_owners(self.data_dir, migrate=False)


# ---------------------------------------------------------------- read-back helpers


def _codes(readiness: Readiness) -> list[str]:
    return [reason.code for reason in readiness.reasons]


def _readiness(o: Owners, item: str) -> tuple[Readiness, Readiness]:
    return o.product_readiness.base_readiness(item), o.product_readiness.pricing_readiness(
        item, CONTEXT
    )


def _items_by_quantity(o: Owners, group: str) -> dict[int, Any]:
    items = o.products.product(group).items
    return dict(sorted((item.composition.quantity, item) for item in items))


def _snapshot_evidence(snapshot: Any) -> dict[str, object]:
    return {
        "pricing_snapshot_id": snapshot.pricing_snapshot_id,
        "purchase_cost_krw": snapshot.purchase_cost_krw,
        "supplier_shipping_krw": snapshot.supplier_shipping_krw,
        "minimum_sale_price_krw": snapshot.minimum_sale_price_krw,
        "target_margin_price_krw": snapshot.target_margin_price_krw,
        "final_sale_price_krw": snapshot.final_sale_price_krw,
        "price_basis": snapshot.price_basis.value,
        "platform_fee_krw": snapshot.platform_fee_krw,
        "expected_net_profit_krw": snapshot.expected_net_profit_krw,
        "expected_net_margin_bp": snapshot.expected_net_margin_bp,
        "price_guard": snapshot.price_guard.value,
        "guard_reasons": [reason.value for reason in snapshot.guard_reasons],
    }


def _price(run: Run, label: str, item: str, expected: Expected) -> dict[str, object]:
    """Price one Item through the pricing owner, then read the current snapshot back from durable
    state and hold every amount to the expected result."""
    o = run.owners
    result = o.pricing.price(item, CONTEXT)
    run.checks.require(f"{label}.priced", result.outcome is PricingOutcome.RECORDED)
    snapshot = o.pricing.current_pricing_snapshot(item, CONTEXT)
    run.checks.require(f"{label}.snapshot_read_back", snapshot is not None)
    assert snapshot is not None and result.snapshot is not None
    observed = _snapshot_evidence(snapshot)
    run.checks.check(
        f"{label}.snapshot_is_the_recorded_one",
        snapshot.pricing_snapshot_id == result.snapshot.pricing_snapshot_id,
    )
    run.checks.check(
        f"{label}.exact_amounts",
        (
            snapshot.purchase_cost_krw,
            snapshot.supplier_shipping_krw,
            snapshot.minimum_sale_price_krw,
            snapshot.target_margin_price_krw,
            snapshot.final_sale_price_krw,
            snapshot.price_basis,
            snapshot.platform_fee_krw,
            snapshot.expected_net_profit_krw,
            snapshot.expected_net_margin_bp,
            snapshot.price_guard,
            tuple(reason.value for reason in snapshot.guard_reasons),
        )
        == (
            expected.purchase,
            expected.shipping,
            expected.minimum,
            expected.target,
            expected.final,
            expected.basis,
            expected.fee,
            expected.profit,
            expected.margin_bp,
            expected.guard,
            expected.reasons,
        ),
        **observed,
    )
    return observed


def _select_and_qa(
    run: Run,
    label: str,
    item: str,
    revision: str,
    decisions: list[SourceDecision],
    *,
    qa_required: bool = True,
) -> dict[str, object]:
    """Record an explicit operator selection that places every decision's image in order, then
    PASS every selected exact binary under the current QA rule for ``revision``.

    A verdict belongs to one exact binary under one revision, not to an Item. When
    ``qa_required``, no verdict exists yet for the selected binaries under ``revision``, and
    readiness must say so before QA runs. Otherwise another Item's QA already covers them, and
    recording it again returns the same verdict."""
    o = run.owners
    outputs = [
        SelectedOutput(role=d.role, source_role=d.role, source_ordinal=d.ordinal) for d in decisions
    ]
    selection, move = o.images.record_operator_selection(
        item,
        source_revision_id=revision,
        decisions=decisions,
        outputs=outputs,
        decided_by=OPERATOR,
    )
    before_qa = o.product_readiness.base_readiness(item)
    if qa_required:
        codes = _codes(before_qa)
        run.checks.check(
            f"{label}.exact_binary_qa_is_required",
            before_qa.status is not ReadinessStatus.READY
            and bool({IMAGE_QA_MISSING, IMAGE_QA_STALE} & set(codes)),
            codes=codes,
        )
    qa_ids = []
    for output in selection.outputs:
        qa = o.images.record_qa(
            asset_kind=output.asset_kind,
            sha256=output.sha256,
            derivation_id=output.derivation_id,
            validated_source_revision_id=revision,
            verdict=QaVerdict.PASS,
            decided_by=QA,
        )
        qa_ids.append(qa.qa_result_id)
    current = o.images.current_selection(item)
    run.checks.check(
        f"{label}.selection_read_back",
        current is not None and current.selection_revision_id == selection.selection_revision_id,
    )
    return {
        "selection_revision_id": selection.selection_revision_id,
        "selection_move": move.reason.value,
        "outputs": [
            [output.asset_kind.value, output.sha256, output.derivation_id]
            for output in selection.outputs
        ],
        "qa_result_ids": qa_ids,
    }


def _derive(run: Run, label: str, revision: str, source_sha: str) -> str:
    """Hand the image owner one already-completed deterministic derivation of ``source_sha``."""
    o = run.owners
    record = o.images.record_completed_derivation(
        CompletedDerivation(
            output=S1_DERIVED,
            validated_source_revision_id=revision,
            inputs=(DerivationInput(kind=ImageAssetKind.SOURCE_ASSET, sha256=source_sha),),
            transformation_spec={"frame": "square"},
            transformation_version=TRANSFORM_VERSION,
            policy_version=None,
            operations=(
                OperationRecord(
                    capability="SYNTHETIC_FRAME",
                    execution_class=ExecutionClass.LOCAL,
                    outcome=OperationOutcome.COMPLETED,
                    input_digest=source_sha,
                    output_digest=sha256(S1_DERIVED),
                    executed_at=EXECUTED,
                ),
            ),
            produced_at=EXECUTED,
        ),
        decided_by=OPERATOR,
    )
    read = o.images.read_derivation(record.derivation_id)
    run.checks.check(
        f"{label}.derivation_read_back",
        read.artifact_sha256 == sha256(S1_DERIVED) == record.artifact_sha256
        and read.validated_source_revision_id == revision
        and read.roots == (source_sha,),
    )
    run.checks.check(
        f"{label}.derived_bytes_apart_from_source_assets",
        o.derived_images.read(read.artifact_sha256) == S1_DERIVED
        and o.source_assets.get(read.artifact_sha256) is None
        and o.derived_images.path(read.artifact_sha256).is_relative_to(o.derived_images.directory)
        and not o.derived_images.path(read.artifact_sha256).is_relative_to(
            o.config.source_assets_dir
        ),
    )
    return record.derivation_id


# ---------------------------------------------------------------- manifest (read-back)


def manifest(o: Owners, tracked: Tracked) -> dict[str, Any]:
    """Everything the run tracks, read back through production read services only."""
    products: dict[str, Any] = {}
    items: dict[str, Any] = {}
    for scenario, group in sorted(tracked.groups.items()):
        readback = o.products.product(group)
        current = {m.member_id: m.current_source_revision_id for m in readback.members}
        products[scenario] = {
            "product_group_id": readback.product_group_id,
            "status": readback.status.value,
            "membership_revision_id": readback.membership_revision_id,
            "membership_revision_no": readback.membership_revision_no,
            "members": [
                [m.member_id, m.source_product_uid, m.current_source_revision_id]
                for m in readback.members
            ],
        }
        for item in readback.items:
            binding = item.current_binding
            revision = None if binding is None else current.get(binding.group_member_id)
            snapshot = o.pricing.current_pricing_snapshot(item.item_id, CONTEXT)
            selection = o.images.current_selection(item.item_id)
            qa = []
            if selection is not None and revision is not None:
                for output in selection.outputs:
                    record = o.images.qa_for_selected_asset(
                        asset_kind=output.asset_kind,
                        sha256=output.sha256,
                        derivation_id=output.derivation_id,
                        validated_source_revision_id=revision,
                    )
                    qa.append(
                        None if record is None else [record.qa_result_id, record.verdict.value]
                    )
            base, pricing = _readiness(o, item.item_id)
            items[item.item_id] = {
                "product_group_id": group,
                "composition_signature": item.composition.composition_signature,
                "quantity": item.composition.quantity,
                "binding": None
                if binding is None
                else [
                    binding.binding_id,
                    binding.binding_kind.value,
                    binding.quantity_offer_id,
                    binding.fulfillment_quantity,
                    binding.provenance_revision_id,
                ],
                "current_pricing_snapshot_id": None
                if snapshot is None
                else snapshot.pricing_snapshot_id,
                "selection_revision_id": None
                if selection is None
                else selection.selection_revision_id,
                "qa": qa,
                "base_readiness": [base.status.value, _codes(base), base.dependency_fingerprint],
                "pricing_readiness": [
                    pricing.status.value,
                    _codes(pricing),
                    pricing.dependency_fingerprint,
                ],
            }
    derivations = {
        label: o.images.read_derivation(derivation).artifact_sha256
        for label, derivation in sorted(tracked.derivations.items())
    }
    offers: dict[str, Any] = {}
    with o.product_store.reading() as unit:
        for label, revision in sorted(tracked.revisions.items()):
            offers[label] = [
                [x.quantity_offer_id, x.tier_ordinal, x.quantity, x.total_price_krw]
                for x in unit.quantity_offers_of_revision(revision)
            ]
    sources = {
        scenario: o.product_store.current_source_revision(uid)
        for scenario, uid in sorted(tracked.sources.items())
    }
    return {
        "products": products,
        "items": items,
        "derivations": derivations,
        "offers": offers,
        "current_source_revisions": sources,
    }


def _state(o: Owners) -> dict[str, Any]:
    return {
        "database": evidence.database_digest(o.database_file),
        "source_assets": evidence.file_digests(o.config.source_assets_dir),
        "derived_images": evidence.file_digests(o.config.derived_images_dir),
    }


def restart_phase(run: Run, label: str) -> dict[str, object]:
    """Close every owner, reopen the same root, read everything back, and replay: nothing may
    change, and reading must write nothing."""
    before = manifest(run.owners, run.tracked)
    state_before = _state(run.owners)
    run.restart()
    o = run.owners
    run.checks.check(f"{label}.schema_at_head", o.database_revision() == head_revision())
    after = manifest(o, run.tracked)
    run.checks.check(f"{label}.read_back_identical", after == before)
    run.checks.check(f"{label}.reading_wrote_nothing", _state(o) == state_before)
    for scenario in sorted(run.tracked.sources):
        replay = o.materializer.materialize_source(SUPPLIER, scenario)
        run.checks.check(
            f"{label}.{scenario}.materialization_replay_unchanged",
            replay.status is MaterializationStatus.UNCHANGED,
            status=replay.status.value,
        )
    for item in sorted(after["items"]):
        if after["items"][item]["current_pricing_snapshot_id"] is None:
            continue
        replay_price = o.pricing.price(item, CONTEXT)
        run.checks.check(
            f"{label}.pricing_replay_unchanged",
            replay_price.outcome is PricingOutcome.UNCHANGED,
            outcome=replay_price.outcome.value,
        )
    run.checks.check(f"{label}.replays_wrote_nothing", _state(o) == state_before)
    return {
        "products": len(after["products"]),
        "items": len(after["items"]),
        "read_back_identical": after == before,
        "database_digest_unchanged": _state(o)["database"] == state_before["database"],
        "manifest_digest": evidence.digest(after),
    }


# ---------------------------------------------------------------- S1 BASE_PRODUCT


def scenario_base(run: Run) -> dict[str, object]:
    o, c, t = run.owners, run.checks, run.tracked
    r1 = record_revision(o, product=S1, facts=S1_R1, images=S1_IMAGES, sequence=1)
    result = o.materializer.materialize_run(r1.run_id)
    c.require("s1.r1.materialized", result.status is MaterializationStatus.MATERIALIZED)
    group, uid = str(result.product_group_id), str(result.source_product_uid)
    t.groups[S1], t.sources[S1], t.revisions["s1.r1"] = group, uid, r1.revision.revision_id
    c.check(
        "s1.r1.source_identity",
        o.product_store.source_product(SUPPLIER, S1).source_product_uid == uid,
    )
    c.check(
        "s1.r1.current_source_revision",
        o.product_store.current_source_revision(uid) == r1.revision.revision_id,
    )
    c.check("s1.r1.move_initial", result.move is MoveReason.INITIAL)
    product = o.products.product(group)
    c.check(
        "s1.r1.one_group_one_confirmed_member",
        len(product.members) == 1 and product.membership_revision_no == 1,
    )
    (item,) = product.items
    t.items["s1.default"] = item.item_id
    c.check(
        "s1.r1.default_single_unit_item",
        item.composition.composition_signature == DEFAULT_SINGLE_UNIT_SIGNATURE
        and item.composition.quantity == 1,
    )
    binding = item.current_binding
    c.require(
        "s1.r1.base_product_binding",
        binding is not None
        and binding.binding_kind is BindingKind.BASE_PRODUCT
        and binding.quantity_offer_id is None
        and binding.fulfillment_quantity == 1
        and binding.provenance_revision_id == r1.revision.revision_id,
    )
    assert binding is not None
    pricing = _price(run, "s1.r1.pricing", item.item_id, S1_R1_PRICE)
    c.check(
        "s1.r1.canonical_minimum_precedence",
        pricing["final_sale_price_krw"] == S1_R1.minimum
        and pricing["price_basis"] == PriceBasis.MINIMUM_SALE_PRICE.value
        and int(str(pricing["target_margin_price_krw"])) > int(str(S1_R1.minimum)),
    )
    # Images: a derived artifact, an explicit operator selection, exact-binary QA.
    representative = r1.revision.images[0].sha256
    detail = r1.revision.images[1].sha256
    assert representative is not None and detail is not None
    derivation = _derive(run, "s1.r1.image", r1.revision.revision_id, representative)
    t.derivations["s1.r1"] = derivation
    unselected = o.product_readiness.base_readiness(item.item_id)
    c.check(
        "s1.r1.operator_selection_is_required",
        _codes(unselected) == [IMAGE_SELECTION_MISSING],
        codes=_codes(unselected),
    )
    images = _select_and_qa(
        run,
        "s1.r1.image",
        item.item_id,
        r1.revision.revision_id,
        [
            SourceDecision(
                ImageRole.REPRESENTATIVE,
                0,
                representative,
                SourceDecisionKind.USE_DERIVED,
                derivation,
            ),
            SourceDecision(ImageRole.DETAIL, 0, detail, SourceDecisionKind.USE_SOURCE),
        ],
    )
    base, priced = _readiness(o, item.item_id)
    c.check("s1.r1.base_ready", base.status is ReadinessStatus.READY, codes=_codes(base))
    c.check("s1.r1.pricing_ready", priced.status is ReadinessStatus.READY, codes=_codes(priced))
    return {
        "source_product_uid": uid,
        "product_group_id": group,
        "member_id": binding.group_member_id,
        "item_id": item.item_id,
        "composition_signature": item.composition.composition_signature,
        "revision_r1": r1.revision.revision_id,
        "binding_r1": binding.binding_id,
        "pricing_r1": pricing,
        "derivation_r1": derivation,
        "derived_artifact_sha256": sha256(S1_DERIVED),
        "images_r1": images,
        "readiness_r1": [base.status.value, priced.status.value],
    }


def scenario_base_revision(run: Run, s1: dict[str, Any]) -> dict[str, object]:
    o, c, t = run.owners, run.checks, run.tracked
    history = evidence.snapshot(o.database_file)
    files = _state(o)
    r1_id, item = s1["revision_r1"], s1["item_id"]
    r1_before = o.revisions.get(r1_id)
    r2 = record_revision(o, product=S1, facts=S1_R2, images=S1_IMAGES, sequence=2)
    result = o.materializer.materialize_run(r2.run_id)
    c.require("s1.r2.materialized", result.status is MaterializationStatus.MATERIALIZED)
    r2_id = r2.revision.revision_id
    t.revisions["s1.r2"] = r2_id
    c.check(
        "s1.r2.source_is_distinct",
        r2.revision.source_fingerprint != (r1_before.source_fingerprint if r1_before else None),
    )
    c.check("s1.r2.same_source_product", result.source_product_uid == s1["source_product_uid"])
    c.check("s1.r2.same_product_group", result.product_group_id == s1["product_group_id"])
    c.check("s1.r2.same_default_item", result.item_id == item and not result.item_created)
    c.check(
        "s1.r2.pointer_advanced",
        result.move is MoveReason.NEWER_REVISION
        and o.product_store.current_source_revision(s1["source_product_uid"]) == r2_id,
    )
    c.check(
        "s1.r2.r1_unchanged",
        o.revisions.get(r1_id) == r1_before
        and r1_before is not None
        and r1_before.fingerprints_intact(),
    )
    binding = o.products.product(s1["product_group_id"]).items[0].current_binding
    c.check(
        "s1.r2.binding_replaced",
        result.bindings_closed == (s1["binding_r1"],)
        and binding is not None
        and binding.binding_id == result.binding_opened
        and binding.provenance_revision_id == r2_id
        and binding.binding_kind is BindingKind.BASE_PRODUCT,
    )
    # The old price and image decisions stay as they were, and none of them is current.
    base, priced = _readiness(o, item)
    old = o.pricing.current_pricing_snapshot(item, CONTEXT)
    c.check(
        "s1.r2.old_snapshot_kept_as_pointer_target",
        old is not None and old.pricing_snapshot_id == s1["pricing_r1"]["pricing_snapshot_id"],
    )
    c.check(
        "s1.r2.old_pricing_stale",
        priced.status is ReadinessStatus.STALE and _codes(priced) == [PRICING_SNAPSHOT_SUPERSEDED],
        codes=_codes(priced),
    )
    c.check(
        "s1.r2.old_image_decisions_not_current",
        base.status is ReadinessStatus.STALE
        and IMAGE_SELECTION_STALE in _codes(base)
        and IMAGE_DERIVATION_STALE in _codes(base),
        codes=_codes(base),
    )
    stale = [base.status.value, priced.status.value]
    c.check(
        "s1.r2.history_unchanged",
        not evidence.history_changes(history, evidence.snapshot(o.database_file)),
        changes=evidence.history_changes(history, evidence.snapshot(o.database_file)),
    )
    c.check(
        "s1.r2.stored_bytes_unchanged",
        all(_state(o)[k] == files[k] for k in ("source_assets", "derived_images")),
    )
    # Refresh explicitly: reprice, derive again under R2, reselect, QA the R2 binaries.
    history = evidence.snapshot(o.database_file)
    pricing = _price(run, "s1.r2.pricing", item, S1_R2_PRICE)
    moved = o.pricing.current_pricing_snapshot(item, CONTEXT)
    c.check(
        "s1.r2.repriced",
        moved is not None and moved.pricing_snapshot_id != s1["pricing_r1"]["pricing_snapshot_id"],
    )
    representative = r2.revision.images[0].sha256
    detail = r2.revision.images[1].sha256
    assert representative is not None and detail is not None
    derivation = _derive(run, "s1.r2.image", r2_id, representative)
    t.derivations["s1.r2"] = derivation
    c.check("s1.r2.new_derivation_same_artifact", derivation != s1["derivation_r1"])
    images = _select_and_qa(
        run,
        "s1.r2.image",
        item,
        r2_id,
        [
            SourceDecision(
                ImageRole.REPRESENTATIVE,
                0,
                representative,
                SourceDecisionKind.USE_DERIVED,
                derivation,
            ),
            SourceDecision(ImageRole.DETAIL, 0, detail, SourceDecisionKind.USE_SOURCE),
        ],
    )
    c.check("s1.r2.reselected", images["selection_move"] == "RESELECTED")
    base, priced = _readiness(o, item)
    c.check("s1.r2.base_ready_again", base.status is ReadinessStatus.READY, codes=_codes(base))
    c.check(
        "s1.r2.pricing_ready_again", priced.status is ReadinessStatus.READY, codes=_codes(priced)
    )
    c.check(
        "s1.r2.refresh_rewrote_no_history",
        not evidence.history_changes(history, evidence.snapshot(o.database_file)),
    )
    return {
        "revision_r2": r2_id,
        "binding_r2": result.binding_opened,
        "pricing_r2": pricing,
        "derivation_r2": derivation,
        "images_r2": images,
        "readiness_after_r2": stale,
        "readiness_after_refresh": [base.status.value, priced.status.value],
    }


# ---------------------------------------------------------------- S2 QuantityOffer


def _offers(o: Owners, revision: str) -> list[Any]:
    with o.product_store.reading() as unit:
        return list(unit.quantity_offers_of_revision(revision))


def _schema_names(o: Owners) -> tuple[list[str], list[str]]:
    with evidence.read_only(o.database_file) as connection:
        names = evidence.tables(connection)
        columns = [
            f"{table}.{row[1]}"
            for table in names
            for row in connection.execute(f"PRAGMA table_info({table})")
        ]
    return names, columns


def scenario_quantity(run: Run) -> dict[str, object]:
    o, c, t = run.owners, run.checks, run.tracked
    q1 = record_revision(o, product=S2, facts=S2_Q1, images=S2_IMAGES, sequence=1)
    result = o.materializer.materialize_run(q1.run_id)
    c.require("s2.q1.materialized", result.status is MaterializationStatus.MATERIALIZED)
    q1_id, group = q1.revision.revision_id, str(result.product_group_id)
    t.groups[S2], t.sources[S2], t.revisions["s2.q1"] = group, str(result.source_product_uid), q1_id
    c.check(
        "s2.q1.product_level_offers", result.quantity_offers is QuantityOfferEvidence.PRODUCT_LEVEL
    )
    offers = _offers(o, q1_id)
    c.check(
        "s2.q1.three_exact_offers",
        [(x.tier_ordinal, x.quantity, x.total_price_krw, x.currency) for x in offers]
        == [(0, 1, 19900, "KRW"), (1, 2, 37900, "KRW"), (2, 3, 53900, "KRW")],
    )
    totals = {x.total_price_krw for x in offers}
    c.check(
        "s2.q1.no_unit_price_or_multiple",
        totals.isdisjoint({19900 * 2, 19900 * 3, 37900 // 2, 53900 // 3}),
    )
    c.check("s2.q1.generic_prices_unused", totals.isdisjoint(S2_PRICES))
    tables, columns = _schema_names(o)
    c.check("s2.q1.no_source_sku", not [n for n in tables + columns if "sku" in n.lower()])
    items = _items_by_quantity(o, group)
    c.require("s2.q1.three_items", sorted(items) == [1, 2, 3])
    for quantity, item in items.items():
        t.items[f"s2.q{quantity}"] = item.item_id
    c.check(
        "s2.q1.q1_is_the_default_single_unit",
        items[1].composition.composition_signature == DEFAULT_SINGLE_UNIT_SIGNATURE,
    )
    c.check(
        "s2.q1.distinct_quantity_items",
        len({i.item_id for i in items.values()}) == 3
        and all(
            i.composition.composition_signature
            == composition_signature(CompositionSpec(quantity=q))
            for q, i in items.items()
        ),
    )
    by_quantity = {x.quantity: x for x in offers}
    c.check(
        "s2.q1.exact_source_offer_bindings",
        all(
            (b := i.current_binding) is not None
            and b.binding_kind is BindingKind.SOURCE_OFFER
            and b.quantity_offer_id == by_quantity[q].quantity_offer_id
            and b.fulfillment_quantity == q
            and b.provenance_revision_id == q1_id
            for q, i in items.items()
        ),
    )
    # Each Item owns its image selection: none exists yet, and one never flows to another.
    unselected = {q: _readiness(o, i.item_id)[0] for q, i in items.items()}
    c.check(
        "s2.q1.every_item_needs_its_own_selection",
        all(
            r.status is ReadinessStatus.REVIEW_REQUIRED and _codes(r) == [IMAGE_SELECTION_MISSING]
            for r in unselected.values()
        ),
    )
    pricing = {
        q: _price(run, f"s2.q1.q{q}.pricing", i.item_id, Q1_PRICES[q]) for q, i in items.items()
    }
    c.check(
        "s2.q1.costs_are_offer_totals",
        [pricing[q]["purchase_cost_krw"] for q in (1, 2, 3)] == [19900, 37900, 53900],
    )
    decisions = _source_decisions(q1.revision)
    images = {1: _select_and_qa(run, "s2.q1.q1.image", items[1].item_id, q1_id, decisions)}
    c.check(
        "s2.q1.q2_q3_do_not_inherit_q1_selection",
        all(
            o.images.current_selection(items[q].item_id) is None
            and _codes(_readiness(o, items[q].item_id)[0]) == [IMAGE_SELECTION_MISSING]
            for q in (2, 3)
        ),
    )
    for q in (2, 3):
        images[q] = _select_and_qa(
            run, f"s2.q1.q{q}.image", items[q].item_id, q1_id, decisions, qa_required=False
        )
    selections = {str(images[q]["selection_revision_id"]) for q in (1, 2, 3)}
    c.check("s2.q1.one_selection_per_item", len(selections) == 3)
    ready = {q: _readiness(o, i.item_id) for q, i in items.items()}
    c.check(
        "s2.q1.every_item_ready",
        all(
            b.status is ReadinessStatus.READY and p.status is ReadinessStatus.READY
            for b, p in ready.values()
        ),
    )
    return {
        "product_group_id": group,
        "revision_q1": q1_id,
        "offers_q1": [[x.quantity_offer_id, x.quantity, x.total_price_krw] for x in offers],
        "items": {str(q): i.item_id for q, i in items.items()},
        "bindings_q1": {
            str(q): i.current_binding.binding_id for q, i in items.items() if i.current_binding
        },
        "pricing_q1": {str(q): v for q, v in pricing.items()},
        "readiness_before_selection": {str(q): r.status.value for q, r in unselected.items()},
        "images_q1": {str(q): v for q, v in images.items()},
    }


def _source_decisions(revision: Any) -> list[SourceDecision]:
    return [
        SourceDecision(ref.role, ref.ordinal, str(ref.sha256), SourceDecisionKind.USE_SOURCE)
        for ref in revision.images
    ]


def scenario_quantity_revision(run: Run, s2: dict[str, Any]) -> dict[str, object]:
    o, c, t = run.owners, run.checks, run.tracked
    history = evidence.snapshot(o.database_file)
    old_offers = [x.quantity_offer_id for x in _offers(o, s2["revision_q1"])]
    q2 = record_revision(o, product=S2, facts=S2_Q2, images=S2_IMAGES, sequence=2)
    result = o.materializer.materialize_run(q2.run_id)
    c.require("s2.q2.materialized", result.status is MaterializationStatus.MATERIALIZED)
    q2_id = q2.revision.revision_id
    t.revisions["s2.q2"] = q2_id
    items = _items_by_quantity(o, s2["product_group_id"])
    c.check(
        "s2.q2.same_group_and_items",
        result.product_group_id == s2["product_group_id"]
        and {str(q): i.item_id for q, i in items.items()} == s2["items"]
        and result.quantity_items_created == (),
    )
    offers = _offers(o, q2_id)
    c.check(
        "s2.q2.new_revision_scoped_offers",
        [(x.quantity, x.total_price_krw) for x in offers] == [(1, 19900), (2, 36900), (3, 52900)]
        and {x.quantity_offer_id for x in offers}.isdisjoint(old_offers),
    )
    c.check(
        "s2.q2.old_offers_kept",
        [x.quantity_offer_id for x in _offers(o, s2["revision_q1"])] == old_offers,
    )
    c.check(
        "s2.q2.old_bindings_closed_new_opened",
        sorted(result.bindings_closed) == sorted(s2["bindings_q1"].values())
        and len(result.offer_bindings_opened) == 3,
    )
    by_quantity = {x.quantity: x for x in offers}
    c.check(
        "s2.q2.exact_new_bindings",
        all(
            (b := i.current_binding) is not None
            and b.quantity_offer_id == by_quantity[q].quantity_offer_id
            and b.provenance_revision_id == q2_id
            for q, i in items.items()
        ),
    )
    c.check(
        "s2.q2.history_unchanged",
        not evidence.history_changes(history, evidence.snapshot(o.database_file)),
    )
    stale = {q: _readiness(o, i.item_id) for q, i in items.items()}
    c.check(
        "s2.q2.old_pricing_stale",
        all(
            p.status is ReadinessStatus.STALE and _codes(p) == [PRICING_SNAPSHOT_SUPERSEDED]
            for _b, p in stale.values()
        ),
    )
    c.check(
        "s2.q2.old_image_decisions_not_current",
        all(
            b.status is ReadinessStatus.STALE and IMAGE_SELECTION_STALE in _codes(b)
            for b, _p in stale.values()
        ),
    )
    history = evidence.snapshot(o.database_file)
    pricing = {
        q: _price(run, f"s2.q2.q{q}.pricing", i.item_id, Q2_PRICES[q]) for q, i in items.items()
    }
    c.check(
        "s2.q2.repriced_from_new_totals",
        [pricing[q]["purchase_cost_krw"] for q in (1, 2, 3)] == [19900, 36900, 52900],
    )
    decisions = _source_decisions(q2.revision)
    images = {
        q: _select_and_qa(run, f"s2.q2.q{q}.image", i.item_id, q2_id, decisions, qa_required=q == 1)
        for q, i in sorted(items.items())
    }
    ready = {q: _readiness(o, i.item_id) for q, i in items.items()}
    c.check(
        "s2.q2.every_item_ready_again",
        all(
            b.status is ReadinessStatus.READY and p.status is ReadinessStatus.READY
            for b, p in ready.values()
        ),
    )
    c.check(
        "s2.q2.refresh_rewrote_no_history",
        not evidence.history_changes(history, evidence.snapshot(o.database_file)),
    )
    return {
        "revision_q2": q2_id,
        "offers_q2": [[x.quantity_offer_id, x.quantity, x.total_price_krw] for x in offers],
        "pricing_q2": {str(q): v for q, v in pricing.items()},
        "readiness_after_q2": {
            str(q): [b.status.value, p.status.value] for q, (b, p) in stale.items()
        },
        "images_q2": {str(q): v for q, v in images.items()},
    }


# ---------------------------------------------------------------- S3 BLOCKED


def scenario_blocked(run: Run) -> dict[str, object]:
    o, c, t = run.owners, run.checks, run.tracked
    r1 = record_revision(o, product=S3, facts=S3_R1, images=S3_IMAGES, sequence=1)
    result = o.materializer.materialize_run(r1.run_id)
    c.require(
        "s3.materialized",
        result.status is MaterializationStatus.MATERIALIZED and result.item_id is not None,
    )
    item = str(result.item_id)
    t.groups[S3], t.sources[S3], t.revisions["s3.r1"] = (
        str(result.product_group_id),
        str(result.source_product_uid),
        r1.revision.revision_id,
    )
    t.items["s3.default"] = item
    pricing = _price(run, "s3.pricing", item, S3_PRICE)
    base, priced = _readiness(o, item)
    c.check(
        "s3.base_blocked_by_sold_out",
        base.status is ReadinessStatus.BLOCKED and SOURCE_STOCK_SOLD_OUT in _codes(base),
        codes=_codes(base),
    )
    c.check(
        "s3.pricing_blocked_by_loss",
        priced.status is ReadinessStatus.BLOCKED and "PRICE_LOSS" in _codes(priced),
        codes=_codes(priced),
    )
    return {
        "item_id": item,
        "pricing": pricing,
        "readiness": [base.status.value, priced.status.value],
        "base_reasons": _codes(base),
        "pricing_reasons": _codes(priced),
    }


# ---------------------------------------------------------------- boundary evidence


def boundary(run: Run) -> dict[str, object]:
    o, c = run.owners, run.checks
    tables, _columns = _schema_names(o)
    count = o.register.registration_candidate_count()
    c.check("boundary.registration_candidates_zero", count == 0, count=count)
    # M5 PR-B (migration 0016) adds the registration tables. M4 registers nothing, so every one of
    # them holds no row: the claim is the absence of registration state, not of its schema.
    registration = [
        n for n in tables if any(w in n for w in ("registration", "draft", "listing_item"))
    ]
    with evidence.read_only(o.database_file) as connection:
        held = {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in registration
        }
        marks = ", ".join("?" for _ in PROVIDER_AUDIT)
        provider_events = connection.execute(
            f"SELECT COUNT(*) FROM audit_events WHERE event_type IN ({marks})", PROVIDER_AUDIT
        ).fetchone()[0]
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    c.check(
        "boundary.no_supplier_or_marketplace_audit", provider_events == 0, count=provider_events
    )
    c.check("boundary.no_job_ran", jobs == 0, count=jobs)
    c.check("boundary.no_registration_state", not any(held.values()), rows=held)
    stored = evidence.file_digests(o.config.source_assets_dir)
    derived = evidence.file_digests(o.config.derived_images_dir)
    c.check(
        "boundary.stored_bytes_content_addressed",
        evidence.content_addressed(stored) and evidence.content_addressed(derived),
    )
    c.check("boundary.derived_apart_from_source", not set(stored) & set(derived))
    return {
        "registration_candidate_count": count,
        "registration_rows": held,
        "supplier_or_marketplace_audit_events": provider_events,
        "jobs": jobs,
        "source_assets": len(stored),
        "derived_artifacts": len(derived),
    }


def _readiness_examples(report: Mapping[str, Any]) -> dict[str, object]:
    s1, s2, s3 = report["s1_base_product"], report["s2_quantity_offer"], report["s3_blocked"]
    return {
        "READY": [
            "s1 after R1 (base and pricing)",
            "s1 after the R2 refresh",
            "s2 q1..q3 after selection and QA",
        ],
        "REVIEW_REQUIRED": {
            "s2 q1..q3 before their own selection": s2["readiness_before_selection"]
        },
        "STALE": {
            "s1 after R2 (base, pricing)": s1["readiness_after_r2"],
            "s2 after Q2": s2["readiness_after_q2"],
        },
        "BLOCKED": {
            "s3 (base, pricing)": s3["readiness"],
            "reasons": [s3["base_reasons"], s3["pricing_reasons"]],
        },
        "DUPLICATE": "NOT_EXERCISED: no M4 rule produces it, because M4 performs no MERGE"
        " (ADR-0013 section 4); its place in the precedence is pinned by the harness unit tests",
    }


# ---------------------------------------------------------------- the run


def hard_zero(checks: Checks, guards: GuardEvidence) -> dict[str, object]:
    """The measured hard-zero evidence. The four provider counters are 0 only when every guard
    that makes them impossible held; otherwise they are unknown, never an invented 0."""
    checks.check(
        "hard_zero.external_network_attempts",
        guards.external_network_attempts == 0,
        count=guards.external_network_attempts,
    )
    checks.check(
        "hard_zero.egress_grants",
        guards.egress_grants_opened == 0,
        count=guards.egress_grants_opened,
    )
    checks.check(
        "hard_zero.no_forbidden_module_preloaded",
        not guards.forbidden_modules_preloaded,
        modules=list(guards.forbidden_modules_preloaded),
    )
    checks.check(
        "hard_zero.forbidden_imports",
        not guards.forbidden_imports_blocked and not guards.forbidden_modules_loaded_during_run,
    )
    checks.check(
        "boundary.preserved_campaign_access",
        guards.preserved_campaign_paths_refused == 0,
        count=guards.preserved_campaign_paths_refused,
    )
    checks.check(
        "boundary.writes_outside_root",
        guards.writes_outside_root == 0,
        count=guards.writes_outside_root,
    )
    held = (
        guards.external_network_attempts == 0
        and guards.egress_grants_opened == 0
        and not guards.forbidden_modules_preloaded
        and not guards.forbidden_imports_blocked
        and not guards.forbidden_modules_loaded_during_run
    )
    zero = 0 if held else None
    return {
        **guards.as_json(),
        "supplier_writes": zero,
        "marketplace_reads_writes": zero,
        "ai_calls": zero,
        "ocr_calls": zero,
        "basis": [
            "network: the process egress audit hook counts every blocked non-loopback attempt",
            "grants: no supplier egress grant was opened during the run",
            "imports: no AI, OCR, marketplace, CONNECT, supplier transport, browser or HTTP-client"
            " module was loaded when the guard armed; the armed guard refuses them, and none was"
            " loaded during the run",
            "composition: no supplier gateway, collection transport, SmartStore caller, AI or"
            " OCR client exists in the run",
        ],
    }


def _run_phases(run: Run, report: dict[str, Any]) -> None:
    checks = run.checks
    report["database_revision"] = run.owners.database_revision()
    checks.check("schema.at_head", report["database_revision"] == head_revision())
    s1 = report["s1_base_product"] = scenario_base(run)
    history = evidence.snapshot(run.owners.database_file)
    stored = _state(run.owners)
    report["restart_after_s1"] = restart_phase(run, "restart.s1")
    s1.update(scenario_base_revision(run, s1))
    s2 = report["s2_quantity_offer"] = scenario_quantity(run)
    s2.update(scenario_quantity_revision(run, s2))
    report["s3_blocked"] = scenario_blocked(run)
    report["final_restart"] = restart_phase(run, "restart.final")
    # Everything S1 made durable before its restart is still there, row for row and byte for byte;
    # only an open binding may have closed its window.
    changes = evidence.history_changes(history, evidence.snapshot(run.owners.database_file))
    checks.check("history.since_s1_unchanged", not changes, changes=changes)
    final = _state(run.owners)
    checks.check(
        "history.stored_bytes_since_s1_unchanged",
        all(
            final[kind].get(name) == value
            for kind in ("source_assets", "derived_images")
            for name, value in stored[kind].items()
        ),
    )
    report["boundary"] = boundary(run)
    report["readiness_states"] = _readiness_examples(report)


def run_acceptance(root: Path, environ: Mapping[str, str]) -> dict[str, Any]:
    """One full offline acceptance run on a fresh dedicated ``root``; the sanitized report.

    A refused root raises ``RootRefused``, and a checkout that is not exactly its commit raises
    ``CheckoutRefused``, both before anything is created. Everything after the claim is a check: a
    failure of any kind is recorded as a problem, and the report says so.
    """
    run_id = str(uuid.uuid4())
    if problems := root_problems(root, environ):
        raise RootRefused(problems)
    checkout = probe_checkout()
    if checkout.problems:
        raise CheckoutRefused(checkout.problems)
    claimed = claim_root(root, environ, run_id)
    report: dict[str, Any] = {
        "schema": evidence.REPORT_SCHEMA,
        "mode": "OFFLINE_SYNTHETIC",
        "claim": "HARNESS_RUN: only the post-merge exact-main run, reviewed by the architect,"
        " can close M4",
        "run_id": run_id,
        "code_sha": checkout.code_sha,
        "checkout": checkout.as_json(),
        "alembic_head": head_revision(),
        "pricing_context": {
            "marketplace_key": CONTEXT.marketplace_key,
            "fingerprint": CONTEXT.fingerprint,
            "fee_rate": CONTEXT.fee_rate,
        },
    }
    checks = Checks()
    with offline(claimed) as guarded:
        run: Run | None = None
        try:
            run = Run(claimed, open_owners(claimed / DATA, migrate=True), checks)
            _run_phases(run, report)
        except Failed:
            pass
        except Exception as error:  # every failure is a problem, never a crash
            code = getattr(error, "reason_code", None)
            checks.check(
                "run.completed",
                False,
                error=type(error).__name__,
                code=None if code is None else str(code),
            )
            report["failure_trace"] = [
                f"{frame.name}:{frame.lineno}"
                for frame in traceback.extract_tb(error.__traceback__)
            ][-6:]
        finally:
            if run is not None:
                run.owners.close()
    assert guarded.evidence is not None
    after = probe_checkout()
    checks.check(
        "checkout.unchanged_during_run",
        after == checkout and not after.problems,
        code_sha=after.code_sha,
        **after.as_json(),
    )
    report["external_hard_zero"] = hard_zero(checks, guarded.evidence)
    report["preserved_campaign_access"] = guarded.evidence.preserved_campaign_paths_refused
    report["checks"] = checks.items
    report["checks_total"] = len(checks.items)
    report["checks_passed"] = sum(1 for item in checks.items if item["passed"])
    report["problems"] = checks.problems
    if found := evidence.leaks(report, (str(root), str(claimed))):
        report = {
            "schema": evidence.REPORT_SCHEMA,
            "run_id": run_id,
            "problems": [f"report withheld: it would leak {what}" for what in found],
        }
    report[evidence.DIGEST_FIELD] = evidence.report_digest(report)
    settle_root(claimed, run_id, passed=not report["problems"])
    return report
