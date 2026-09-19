"""M5 PR-C: the pure preflight rules, fingerprint and payload builder (Issue #89 kickoff
5742880432; ADR-0014 §2–§5, §7, §10, §13, §15, §18–§21).

Every input is invented and fully resolved here, so each rule is proven on its own: no database,
no provider, no AI. The DB-backed paths are in ``tests/integration/test_m5_registration_preflight``.
"""

import json
from dataclasses import replace
from typing import Any

import pytest

from app.collect.facts import ImageRole
from app.connect.accounts import AccountBinding
from app.connect.marketplace.capability import (
    AuthStatus,
    WorkflowScope,
    WorkflowState,
    WriteScopeStatus,
)
from app.products.image_model import ImageAssetKind, QaVerdict
from app.products.model import ReadinessStatus, Reason
from app.products.pricing import PriceBasis, PriceGuard, PricingContextInput, Rounding
from app.register import sanitize
from app.register.model import ListingShape, registration_item_key, sanitized_digest
from app.register.payload import PAYLOAD_BUILDER_VERSION, PayloadNotReadyError, build_payload
from app.register.policy import (
    AssetPolicy,
    CategoryMetadata,
    DuplicateKeyKind,
    FieldRule,
    NoticePolicy,
    OptionPolicy,
    Provenance,
    TargetPolicy,
)
from app.register.preparation import (
    FINGERPRINT_VERSION,
    M4_BASE_PREFIX,
    M4_PRICING_PREFIX,
    PREFLIGHT_RULE_VERSION,
    REASON_CODES,
    AccountState,
    BindingCopy,
    CategoryConfirmation,
    CategorySelection,
    ConflictState,
    DetailComposition,
    DraftItemState,
    DuplicateEvidence,
    DuplicateMatch,
    DuplicateVerdict,
    FieldValue,
    ListingValues,
    LiveRegistration,
    OverrideCoverage,
    PinnedPrice,
    PreflightRequest,
    PreflightResult,
    PreflightStage,
    PreparedAsset,
    PublicationImage,
    ReadinessInput,
    ResaleAdvisory,
    ResolvedItem,
    ResolvedUnit,
    UnitRequest,
    evaluate,
    listing_identity,
    resolve_unit,
)

MARKET = "market_a"
ACCOUNT = f"mpa-{'a' * 32}"
OTHER_ACCOUNT = f"mpa-{'b' * 32}"
DRAFT = "draft-1"
IDENTITY = listing_identity(MARKET, ACCOUNT, DRAFT, [("g1", "s" * 64), ("g2", "t" * 64)], 0)


def _context(**overrides: object) -> PricingContextInput:
    values: dict[str, object] = {
        "marketplace_key": MARKET,
        "account_id": None,
        "fee_table_version": "fee-1",
        "pricing_policy_version": "policy-1",
        "fee_rate": "0.1",
        "fee_fixed_krw": 0,
        "other_cost_rate": "0",
        "other_cost_fixed_krw": 0,
        "cost_rounding": Rounding.CEIL_KRW_1,
        "price_rounding": Rounding.CEIL_KRW_1,
    }
    values.update(overrides)
    return PricingContextInput(**values)  # type: ignore[arg-type]


CONTEXT = _context()
READY_READINESS = ReadinessInput(ReadinessStatus.READY, (), "m4-rule/v", "f" * 64)


def target(**overrides: Any) -> TargetPolicy:
    values: dict[str, Any] = {
        "marketplace_key": MARKET,
        "marketplace_account_id": ACCOUNT,
        "policy_revision": "policy-rev-1",
        "taxonomy_revision": "taxonomy-1",
        "pricing_context": CONTEXT,
        "sanitizer_profile_version": "sanitizer-1",
        "asset_policy": AssetPolicy(profile="asset-profile-1"),
        "templates": {"shipping": "ship-1", "returns": "ret-1"},
    }
    values.update(overrides)
    return TargetPolicy(**values)


METADATA = CategoryMetadata(
    taxonomy_revision="taxonomy-1",
    category_id="cat-1",
    metadata_revision="meta-1",
    reviewed=True,
    attributes=(FieldRule("brand", required=True), FieldRule("color", required=False)),
    notice=NoticePolicy(
        "notice-type-1",
        (
            FieldRule("manufacturer", required=True),
            FieldRule("origin", required=True, detail_page_reference_allowed=True),
            FieldRule("care", required=False),
        ),
    ),
    options=OptionPolicy(options_supported=True, max_options=5, max_dimensions=1),
    required_templates=frozenset({"shipping", "returns"}),
)


def item(item_id: str, ordinal: int, group: str, signature: str, **overrides: Any) -> ResolvedItem:
    pin = PinnedPrice(
        pricing_snapshot_id=f"price-{item_id}",
        item_id=item_id,
        marketplace_key=MARKET,
        account_id=None,
        pricing_context_fingerprint=CONTEXT.fingerprint,
        dependency_fingerprint="d" * 64,
        membership_revision_id=f"membership-{group}",
        source_binding_id=f"binding-{item_id}",
        source_product_facts_revision_id=f"revision-{item_id}",
        final_sale_price_krw=19900 + ordinal,
        price_basis=PriceBasis.TARGET_MARGIN,
        price_guard=PriceGuard.OK,
    )
    values: dict[str, Any] = {
        "item_id": item_id,
        "ordinal": ordinal,
        "product_group_id": group,
        "composition_id": f"composition-{item_id}",
        "composition_signature": signature,
        "pin": pin,
        "current_price_id": pin.pricing_snapshot_id,
        "base": READY_READINESS,
        "pricing": READY_READINESS,
        "binding": BindingCopy(
            f"binding-{item_id}", "BASE_PRODUCT", "member-1", "uid-1", "revision-1", 1, None
        ),
        "selection_revision_id": f"selection-{item_id}",
        "images": (
            PublicationImage(
                ImageRole.REPRESENTATIVE,
                0,
                ImageAssetKind.SOURCE_ASSET,
                f"{ordinal}" * 64,
                None,
                f"qa-{item_id}",
                QaVerdict.PASS,
            ),
        ),
    }
    values.update(overrides)
    return ResolvedItem(**values)


ITEMS = (item("i1", 0, "g1", "s" * 64), item("i2", 1, "g2", "t" * 64))


def _pin0() -> PinnedPrice:
    pin = ITEMS[0].pin
    assert pin is not None
    return pin


def resolved(**overrides: Any) -> ResolvedUnit:
    items = overrides.pop("items", ITEMS)
    values: dict[str, Any] = {
        "marketplace_key": MARKET,
        "marketplace_account_id": ACCOUNT,
        "draft_id": DRAFT,
        "draft_revision": 3,
        "listing_shape": ListingShape.SINGLE_LISTING_WITH_OPTIONS,
        "open_items": tuple(
            DraftItemState(i.item_id, i.ordinal, i.pin.pricing_snapshot_id)  # type: ignore[union-attr]
            for i in items
        ),
        "unit_item_ids": tuple(i.item_id for i in items),
        "items": items,
        "account": AccountState(
            AccountBinding.BOUND, True, AuthStatus.READY, WriteScopeStatus.UNKNOWN
        ),
        "listing_identity": IDENTITY,
        "identity_generation": 0,
        "conflicts": (),
        "live_registrations": (),
        "metadata": METADATA,
        "target": target(),
    }
    values.update(overrides)
    return ResolvedUnit(**values)


def evidence(**overrides: Any) -> DuplicateEvidence:
    values: dict[str, Any] = {
        "marketplace_key": MARKET,
        "marketplace_account_id": ACCOUNT,
        "listing_identity": IDENTITY,
        "lookup_contract_version": "lookup-1",
        "evidence_digest": "e" * 64,
        "verdict": DuplicateVerdict.NO_MATCH,
        "keys_checked": frozenset({DuplicateKeyKind.SELLER_CODE, DuplicateKeyKind.GTIN}),
    }
    values.update(overrides)
    return DuplicateEvidence(**values)


LISTING = ListingValues(
    name=FieldValue("invented listing name"),
    tags=frozenset({"tag-a", "tag-b"}),
    attributes={"brand": FieldValue("invented brand")},
    notices={
        "manufacturer": FieldValue("invented maker", Provenance.SOURCE_FACT),
        "origin": FieldValue(detail_page_reference=True),
    },
    options={"i1": {"size": "S"}, "i2": {"size": "M"}},
)


def request(**overrides: Any) -> PreflightRequest:
    values: dict[str, Any] = {
        "unit": UnitRequest(DRAFT, 3),
        "category": CategorySelection(
            "cat-1", "mapping-1", "taxonomy-1", CategoryConfirmation.OPERATOR_CONFIRMED
        ),
        "listing": LISTING,
        "detail": DetailComposition("detail-1", "invented body text"),
        "duplicate_evidence": evidence(),
    }
    values.update(overrides)
    return PreflightRequest(**values)


def candidate(
    req: PreflightRequest | None = None, unit: ResolvedUnit | None = None
) -> PreflightResult:
    return evaluate(req or request(), unit or resolved(), PreflightStage.CANDIDATE)


def prepared_for(result: PreflightResult, **overrides: Any) -> tuple[PreparedAsset, ...]:
    return tuple(
        PreparedAsset(
            asset_kind=image.asset_kind,
            sha256=image.sha256,
            derivation_id=image.derivation_id,
            asset_profile=overrides.get("asset_profile", "asset-profile-1"),
            candidate_fingerprint=overrides.get("fingerprint", result.candidate_fingerprint),
            provider_asset_ref=overrides.get("ref", f"provider-asset-{image.sha256[:8]}"),
        )
        for item in result.resolved.items
        for image in item.images
    )


def final(
    req: PreflightRequest | None = None,
    unit: ResolvedUnit | None = None,
    prepared: tuple[PreparedAsset, ...] | None = None,
) -> PreflightResult:
    req, unit = req or request(), unit or resolved()
    if prepared is None:
        prepared = prepared_for(evaluate(req, unit, PreflightStage.CANDIDATE))
    return evaluate(req, unit, PreflightStage.FINAL, prepared)


def codes(result: PreflightResult) -> set[str]:
    return set(result.codes)


# ---------------------------------------------------------------- READY and determinism


def test_a_complete_operator_confirmed_unit_is_ready_at_both_stages() -> None:
    first = candidate()
    assert (first.status, first.reasons, first.upload_permitted) == (
        ReadinessStatus.READY,
        (),
        True,
    )
    assert (first.rule_version, first.fingerprint_version) == (
        PREFLIGHT_RULE_VERSION,
        FINGERPRINT_VERSION,
    )
    ready = final()
    assert (ready.status, ready.upload_permitted) == (ReadinessStatus.READY, False)
    assert ready.candidate_fingerprint == first.candidate_fingerprint
    assert ready.dependency_fingerprint != first.dependency_fingerprint


def test_same_canonical_inputs_give_the_same_fingerprint_and_payload_digest() -> None:
    # Kickoff test 1.
    one, two = final(), final()
    assert one.dependency_fingerprint == two.dependency_fingerprint
    assert build_payload(one).payload_digest == build_payload(two).payload_digest
    assert build_payload(one).payload_digest == sanitized_digest(build_payload(one).payload)


def test_semantically_irrelevant_order_never_changes_the_fingerprint() -> None:
    # Kickoff test 2: maps, sets and a request's own item order.
    shuffled = replace(
        LISTING,
        tags=frozenset({"tag-b", "tag-a"}),
        attributes=dict(reversed(list(LISTING.attributes.items()))),
        notices=dict(reversed(list(LISTING.notices.items()))),
        options=dict(reversed(list(LISTING.options.items()))),
    )
    base = request(
        duplicate_evidence=evidence(
            verdict=DuplicateVerdict.MATCH,
            matches=(
                DuplicateMatch(DuplicateKeyKind.NORMALIZED_NAME, "p-1"),
                DuplicateMatch(DuplicateKeyKind.NORMALIZED_NAME, "p-2"),
            ),
        ),
    )
    other = replace(
        base,
        listing=shuffled,
        duplicate_evidence=evidence(
            verdict=DuplicateVerdict.MATCH,
            keys_checked=frozenset({DuplicateKeyKind.GTIN, DuplicateKeyKind.SELLER_CODE}),
            matches=(
                DuplicateMatch(DuplicateKeyKind.NORMALIZED_NAME, "p-2"),
                DuplicateMatch(DuplicateKeyKind.NORMALIZED_NAME, "p-1"),
            ),
        ),
    )
    covered = tuple(
        replace(i, overrides=(OverrideCoverage("o-2", i.product_group_id, None),
                              OverrideCoverage("o-1", i.product_group_id, None)))
        for i in ITEMS
    )  # fmt: skip
    reversed_overrides = tuple(replace(i, overrides=tuple(reversed(i.overrides))) for i in covered)
    first = resolved(
        items=covered, target=target(templates={"shipping": "ship-1", "returns": "ret-1"})
    )
    second = resolved(
        items=reversed_overrides,
        target=target(templates={"returns": "ret-1", "shipping": "ship-1"}),
    )
    a, b = candidate(base, first), candidate(other, second)
    assert a.candidate_fingerprint == b.candidate_fingerprint
    assert a.status is ReadinessStatus.READY  # the weak signal is covered by the overrides
    one = prepared_for(a)
    assert (
        evaluate(base, first, PreflightStage.FINAL, one).dependency_fingerprint
        == evaluate(
            other, second, PreflightStage.FINAL, tuple(reversed(one))
        ).dependency_fingerprint
    )


def _item_change(**changes: Any) -> ResolvedUnit:
    return resolved(items=(replace(ITEMS[0], **changes), ITEMS[1]))


# Kickoff §5: every named dependency, changed on its own, changes the candidate fingerprint.
DEPENDENCY_CHANGES: dict[str, tuple[PreflightRequest, ResolvedUnit]] = {
    "draft revision": (request(), resolved(draft_revision=4)),
    "listing shape": (request(), resolved(listing_shape=ListingShape.SELECTED_OFFERS)),
    "canonical account": (request(), resolved(marketplace_account_id=OTHER_ACCOUNT)),
    "account binding": (request(), resolved(account=AccountState(AccountBinding.MISMATCHED, True))),
    "item identity": (request(), _item_change(item_id="i9")),
    "composition signature": (request(), _item_change(composition_signature="u" * 64)),
    "pinned price": (
        request(),
        _item_change(pin=replace(_pin0(), pricing_snapshot_id="price-other")),
    ),
    "binding lineage": (
        request(),
        _item_change(pin=replace(_pin0(), source_binding_id="binding-other")),
    ),
    "facts lineage": (
        request(),
        _item_change(pin=replace(_pin0(), source_product_facts_revision_id="rev-other")),
    ),
    "membership revision": (
        request(),
        _item_change(pin=replace(_pin0(), membership_revision_id="membership-other")),
    ),
    "base readiness": (
        request(),
        _item_change(base=replace(READY_READINESS, dependency_fingerprint="1" * 64)),
    ),
    "pricing readiness": (
        request(),
        _item_change(pricing=replace(READY_READINESS, rule_version="p/2")),
    ),
    "selected artifact": (request(), _item_change(selection_revision_id="selection-other")),
    "artifact qa": (
        request(),
        _item_change(images=(replace(ITEMS[0].images[0], qa_result_id="qa-other"),)),
    ),
    "category mapping": (
        request(
            category=CategorySelection(
                "cat-1", "mapping-2", "taxonomy-1", CategoryConfirmation.OPERATOR_CONFIRMED
            )
        ),
        resolved(),
    ),
    "taxonomy revision": (request(), resolved(target=target(taxonomy_revision="taxonomy-2"))),
    "platform policy": (request(), resolved(target=target(policy_revision="policy-rev-2"))),
    "attribute/notice policy": (
        request(),
        resolved(metadata=replace(METADATA, metadata_revision="meta-2")),
    ),
    "templates": (
        request(),
        resolved(target=target(templates={"shipping": "ship-2", "returns": "ret-1"})),
    ),
    "pricing context": (
        request(),
        resolved(target=target(pricing_context=_context(fee_table_version="fee-2"))),
    ),
    "sanitizer profile": (
        request(),
        resolved(target=target(sanitizer_profile_version="sanitizer-2")),
    ),
    "asset profile": (
        request(),
        resolved(target=target(asset_policy=AssetPolicy(profile="asset-profile-2"))),
    ),
    "duplicate evidence": (
        request(duplicate_evidence=evidence(evidence_digest="f" * 64)),
        resolved(),
    ),
    "duplicate override": (
        request(),
        _item_change(overrides=(OverrideCoverage("o-1", "g1", None),)),
    ),
    "live registration": (request(), resolved(live_registrations=(LiveRegistration("reg-1"),))),
    "unknown conflict": (request(), resolved(conflicts=(ConflictState("intent-1", "UNKNOWN"),))),
    "detail composition": (
        request(detail=DetailComposition("detail-2", "invented body text")),
        resolved(),
    ),
    "detail body": (request(detail=DetailComposition("detail-1", "another body")), resolved()),
    "listing values": (request(listing=replace(LISTING, tags=frozenset({"tag-c"}))), resolved()),
    "listing identity": (request(), resolved(listing_identity="icbm-" + "0" * 32)),
}


@pytest.mark.parametrize("dependency", sorted(DEPENDENCY_CHANGES))
def test_each_named_dependency_changes_the_fingerprint(dependency: str) -> None:
    changed_request, changed_unit = DEPENDENCY_CHANGES[dependency]
    assert (
        candidate(changed_request, changed_unit).candidate_fingerprint
        != candidate().candidate_fingerprint
    )


def test_the_final_fingerprint_also_names_the_prepared_provider_assets() -> None:
    ready = candidate()
    one = final(prepared=prepared_for(ready, ref="provider-asset-a"))
    two = final(prepared=prepared_for(ready, ref="provider-asset-b"))
    assert one.candidate_fingerprint == two.candidate_fingerprint
    assert one.dependency_fingerprint != two.dependency_fingerprint
    assert one.dependencies["candidate_fingerprint"] == ready.candidate_fingerprint


def test_a_selected_subset_is_the_same_unit_in_any_request_order() -> None:
    # Kickoff test 18 (pure part): the unit follows Draft order, never request order.
    open_ids = ["i1", "i2", "i3"]
    for requested in (("i3", "i1"), ("i1", "i3")):
        chosen, problems = resolve_unit(ListingShape.SELECTED_OFFERS, open_ids, requested)
        assert (chosen, problems) == (("i1", "i3"), ())


def test_the_listing_identity_is_deterministic_and_never_from_a_name() -> None:
    key = [("g2", "t" * 64), ("g1", "s" * 64)]
    one = listing_identity(MARKET, ACCOUNT, DRAFT, key, 0)
    assert one == listing_identity(MARKET, ACCOUNT, DRAFT, list(reversed(key)), 0) == IDENTITY
    assert one != listing_identity(MARKET, ACCOUNT, DRAFT, key, 1)  # a later generation
    assert one != listing_identity(MARKET, OTHER_ACCOUNT, DRAFT, key, 0)
    assert one.startswith("icbm-") and len(one) == 37


# ---------------------------------------------------------------- statuses and precedence


def test_all_five_statuses_and_every_reason_are_returned() -> None:
    assert candidate().status is ReadinessStatus.READY
    review = candidate(request(category=None))
    assert (review.status, codes(review)) == (
        ReadinessStatus.REVIEW_REQUIRED,
        {"CATEGORY_NOT_SELECTED"},
    )
    stale = candidate(request(unit=UnitRequest(DRAFT, 2)))
    assert stale.status is ReadinessStatus.STALE
    duplicate = candidate(unit=resolved(live_registrations=(LiveRegistration("reg-1"),)))
    assert duplicate.status is ReadinessStatus.DUPLICATE
    everything = candidate(
        request(category=None, unit=UnitRequest(DRAFT, 2)),
        resolved(
            live_registrations=(LiveRegistration("reg-1"),),
            conflicts=(ConflictState("intent-1", "UNKNOWN"),),
        ),
    )
    # BLOCKED > DUPLICATE > STALE > REVIEW_REQUIRED > READY, and nothing is dropped.
    assert everything.status is ReadinessStatus.BLOCKED
    assert [r.status for r in everything.reasons] == [
        ReadinessStatus.BLOCKED,
        ReadinessStatus.DUPLICATE,
        ReadinessStatus.STALE,
        ReadinessStatus.REVIEW_REQUIRED,
    ]
    assert codes(everything) == {
        "UNRESOLVED_CREATE_CONFLICT",
        "LIVE_REGISTRATION_EXISTS",
        "DRAFT_REVISION_STALE",
        "CATEGORY_NOT_SELECTED",
    }


def test_every_emitted_code_is_inventoried_or_a_propagated_m4_reason() -> None:
    broken = candidate(
        request(
            category=CategorySelection(
                "cat-1", "m", "taxonomy-0", CategoryConfirmation.AI_SUGGESTION
            ),
            listing=ListingValues(name=FieldValue("x", Provenance.AI_SUGGESTION)),
            detail=None,
            duplicate_evidence=None,
        ),
        resolved(account=AccountState(AccountBinding.MISMATCHED, False)),
    )
    assert broken.reasons
    for reason in broken.reasons:
        assert reason.code in REASON_CODES or reason.code.startswith(
            (M4_BASE_PREFIX, M4_PRICING_PREFIX)
        ), reason


# ---------------------------------------------------------------- M4 truth (kickoff §6)


@pytest.mark.parametrize(
    "status",
    [ReadinessStatus.BLOCKED, ReadinessStatus.STALE, ReadinessStatus.REVIEW_REQUIRED],
)
def test_an_m4_status_propagates_with_its_own_status(status: ReadinessStatus) -> None:
    # Kickoff tests 6–8: deterministic mapping, never a re-decision.
    base = ReadinessInput(status, (Reason("SOURCE_X", status, "stock"),), "base/v", "b" * 64)
    pricing = ReadinessInput(status, (Reason("PRICING_Y", status),), "pricing/v", "c" * 64)
    unit = resolved(items=(replace(ITEMS[0], base=base, pricing=pricing), ITEMS[1]))
    result = candidate(unit=unit)
    assert result.status is status
    assert {(r.code, r.status, r.subject) for r in result.reasons} == {
        (f"{M4_BASE_PREFIX}SOURCE_X", status, "item:i1:stock"),
        (f"{M4_PRICING_PREFIX}PRICING_Y", status, "item:i1"),
    }
    assert result.upload_permitted is False


def test_the_pin_is_compared_with_the_m4_current_price_never_re_read() -> None:
    # Kickoff tests 4–5: the Draft pin decides; another current price is STALE, and a pin of
    # another Item, marketplace or account never reaches READY.
    superseded = resolved(items=(replace(ITEMS[0], current_price_id="price-newer"), ITEMS[1]))
    assert codes(candidate(unit=superseded)) == {"DRAFT_PRICE_PIN_SUPERSEDED"}
    other_context = replace(_pin0(), pricing_context_fingerprint="0" * 64)
    stale = resolved(items=(replace(ITEMS[0], pin=other_context), ITEMS[1]))
    assert codes(candidate(unit=stale)) == {"DRAFT_PRICE_PIN_CONTEXT_STALE"}
    wrong_contexts: list[dict[str, Any]] = [
        {"item_id": "i9"},
        {"marketplace_key": "market_b"},
        {"account_id": OTHER_ACCOUNT},
    ]
    for wrong in wrong_contexts:
        pin = replace(_pin0(), **wrong)
        result = candidate(unit=resolved(items=(replace(ITEMS[0], pin=pin), ITEMS[1])))
        assert (result.status, codes(result)) == (
            ReadinessStatus.BLOCKED,
            {"DRAFT_PRICE_PIN_CONTEXT_INVALID"},
        ), wrong
    for context in (_context(marketplace_key="market_b"), _context(account_id=OTHER_ACCOUNT)):
        result = candidate(unit=resolved(target=target(pricing_context=context)))
        assert "TARGET_PRICING_CONTEXT_INVALID" in codes(result)
        assert result.status is ReadinessStatus.BLOCKED


# ---------------------------------------------------------------- account (kickoff §Account)


def test_the_canonical_account_must_be_bound_and_connect_ready() -> None:
    cases = {
        AccountState(AccountBinding.UNKNOWN_ACCOUNT, True, AuthStatus.READY): (
            ReadinessStatus.BLOCKED,
            "ACCOUNT_UNKNOWN",
        ),
        AccountState(AccountBinding.NOT_BOUND, True, AuthStatus.READY): (
            ReadinessStatus.BLOCKED,
            "ACCOUNT_NOT_BOUND",
        ),
        # PR-C never classifies same or different account: a mismatch is review.
        AccountState(AccountBinding.MISMATCHED, True, AuthStatus.READY): (
            ReadinessStatus.REVIEW_REQUIRED,
            "ACCOUNT_BINDING_MISMATCH",
        ),
        AccountState(AccountBinding.BOUND, False): (
            ReadinessStatus.BLOCKED,
            "CONNECT_CAPABILITY_UNAVAILABLE",
        ),
        AccountState(AccountBinding.BOUND, True, AuthStatus.AUTH_MISMATCH): (
            ReadinessStatus.REVIEW_REQUIRED,
            "CONNECT_AUTH_MISMATCH",
        ),
        AccountState(AccountBinding.BOUND, True, AuthStatus.NOT_READY): (
            ReadinessStatus.REVIEW_REQUIRED,
            "CONNECT_AUTH_NOT_READY",
        ),
        AccountState(AccountBinding.BOUND, True, AuthStatus.READY, WriteScopeStatus.MISSING): (
            ReadinessStatus.BLOCKED,
            "CONNECT_WRITE_SCOPE_MISSING",
        ),
        AccountState(
            AccountBinding.BOUND,
            True,
            AuthStatus.READY,
            overlays=((WorkflowScope.PRODUCT_REGISTRATION, WorkflowState.PAUSED),),
        ): (ReadinessStatus.BLOCKED, "CONNECT_WORKFLOW_PAUSED"),
    }
    for account, (status, code) in cases.items():
        result = candidate(unit=resolved(account=account))
        assert (result.status, code in codes(result)) == (status, True), account
    wrong_target = candidate(unit=resolved(target=target(marketplace_account_id=OTHER_ACCOUNT)))
    assert "TARGET_SCOPE_MISMATCH" in codes(wrong_target)


# ---------------------------------------------------------------- conflict and duplicate (§10, §13)


def test_an_unresolved_unknown_is_never_released_by_an_override() -> None:
    # Kickoff tests 9–10.
    covered = tuple(
        replace(i, overrides=(OverrideCoverage("o-1", i.product_group_id, None),)) for i in ITEMS
    )
    for state in ("UNKNOWN", "SENT"):
        result = candidate(
            unit=resolved(items=covered, conflicts=(ConflictState("intent-1", state),))
        )
        assert (result.status, codes(result)) == (
            ReadinessStatus.BLOCKED,
            {"UNRESOLVED_CREATE_CONFLICT"},
        )


def test_duplicate_evidence_without_a_covering_override_is_duplicate() -> None:
    # Kickoff test 11.
    strong = request(
        duplicate_evidence=evidence(
            verdict=DuplicateVerdict.MATCH,
            matches=(DuplicateMatch(DuplicateKeyKind.SELLER_CODE, "provider-1"),),
        )
    )
    assert codes(candidate(strong)) == {"PROVIDER_DUPLICATE_FOUND"}
    assert candidate(strong).status is ReadinessStatus.DUPLICATE
    # An override of one group only does not cover a unit of two groups.
    partial = (replace(ITEMS[0], overrides=(OverrideCoverage("o-1", "g1", None),)), ITEMS[1])
    assert candidate(strong, resolved(items=partial)).status is ReadinessStatus.DUPLICATE
    # An override for another composition does not cover this Item.
    elsewhere = tuple(
        replace(i, overrides=(OverrideCoverage("o-1", i.product_group_id, "composition-x"),))
        for i in ITEMS
    )
    assert candidate(strong, resolved(items=elsewhere)).status is ReadinessStatus.DUPLICATE
    covered = tuple(
        replace(i, overrides=(OverrideCoverage("o-1", i.product_group_id, None),)) for i in ITEMS
    )
    assert candidate(strong, resolved(items=covered)).status is ReadinessStatus.READY
    live = resolved(live_registrations=(LiveRegistration("reg-1"),))
    assert codes(candidate(unit=live)) == {"LIVE_REGISTRATION_EXISTS"}
    weak = request(
        duplicate_evidence=evidence(
            verdict=DuplicateVerdict.MATCH,
            matches=(DuplicateMatch(DuplicateKeyKind.NORMALIZED_NAME, "provider-1"),),
        )
    )
    assert codes(candidate(weak)) == {"PROVIDER_DUPLICATE_WEAK_SIGNAL"}


COVERED = tuple(
    replace(i, overrides=(OverrideCoverage("o-1", i.product_group_id, None),)) for i in ITEMS
)
# PR #92 review 5256446628: provider references that carry signed, tokenized, secret or supplier
# material. None may reach a durable fingerprint, whatever override covers the unit.
UNSAFE_REFERENCES = {
    "signed url": "https://listing.example/p/1?X-Signature=abc123def456",
    "fragment token": "https://listing.example/p/1#access_token=abc",
    "userinfo": "https://user:secret@listing.example/p/1",
    "bearer": "Bearer abcdefghijklmnop",
    "json web token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij",
    "token parameter": "listing?access_token=abcdef",
    "plain http": "http://supplier.example/item/1",
    "protocol relative": "//supplier.example/item/1",
    # Review 5257966787: a scheme without ``//`` is still a URI, never an opaque reference.
    "scheme http": "http:supplier.example/item/1",
    "scheme ftp": "ftp:supplier.example/item/1",
    "scheme https malformed": "https:supplier.example/item/1",
}


@pytest.mark.parametrize("case", sorted(UNSAFE_REFERENCES))
def test_unsafe_duplicate_evidence_is_never_ready_even_under_a_covering_override(
    case: str,
) -> None:
    reference = UNSAFE_REFERENCES[case]
    unsafe = request(
        duplicate_evidence=evidence(
            verdict=DuplicateVerdict.MATCH,
            matches=(DuplicateMatch(DuplicateKeyKind.SELLER_CODE, reference),),
        )
    )
    first = candidate(unsafe, resolved(items=COVERED))
    assert (first.status, codes(first), first.upload_permitted) == (
        ReadinessStatus.BLOCKED,
        {"DUPLICATE_EVIDENCE_UNSAFE"},
        False,
    )
    last = final(unsafe, resolved(items=COVERED), prepared=prepared_for(first))
    assert last.status is ReadinessStatus.BLOCKED
    with pytest.raises(PayloadNotReadyError):
        build_payload(last)
    # The unsafe reference is not a fingerprint input at either stage.
    for result in (first, last):
        assert reference not in json.dumps(result.dependencies, ensure_ascii=False)
    # The same evidence with a safe reference is covered by the override and READY.
    safe = request(
        duplicate_evidence=evidence(
            verdict=DuplicateVerdict.MATCH,
            matches=(DuplicateMatch(DuplicateKeyKind.SELLER_CODE, "provider-listing-1"),),
        )
    )
    assert candidate(safe, resolved(items=COVERED)).status is ReadinessStatus.READY


def test_unsafe_evidence_identities_fail_closed() -> None:
    for unsafe in (
        evidence(evidence_digest="not-a-digest"),
        evidence(evidence_digest="A" * 64),
        evidence(lookup_contract_version="lookup https://lookup.example/v1"),
        evidence(lookup_contract_version="Bearer abcdefghijklmnop"),
        evidence(lookup_contract_version="lookup v1"),
        # Review 5257966787: a label is never URI-like either, ``//`` or not.
        evidence(lookup_contract_version="http:lookup.example/v1"),
        evidence(lookup_contract_version="ftp:lookup.example/v1"),
    ):
        result = candidate(request(duplicate_evidence=unsafe), resolved(items=COVERED))
        assert (result.status, codes(result)) == (
            ReadinessStatus.BLOCKED,
            {"DUPLICATE_EVIDENCE_UNSAFE"},
        ), unsafe
        assert unsafe.lookup_contract_version not in json.dumps(result.dependencies)
        assert unsafe.evidence_digest not in json.dumps(result.dependencies)


def test_only_sanitized_typed_evidence_identity_enters_the_fingerprint() -> None:
    # Option 2 of the review: the digest and typed fields, never a raw provider reference and
    # never a raw scope string.
    one = request(
        duplicate_evidence=evidence(
            verdict=DuplicateVerdict.MATCH,
            matches=(DuplicateMatch(DuplicateKeyKind.SELLER_CODE, "provider-listing-7f3a"),),
        )
    )
    ready = candidate(one, resolved(items=COVERED))
    assert ready.status is ReadinessStatus.READY
    text = json.dumps(ready.dependencies)
    assert "provider-listing-7f3a" not in text
    recorded = ready.dependencies["duplicate"]["evidence"]
    assert set(recorded) == {
        "unsafe",
        "lookup_contract_version",
        "evidence_digest",
        "scope_matches",
        "verdict",
        "keys_checked",
        "match_key_kinds",
    }
    assert (recorded["evidence_digest"], recorded["match_key_kinds"]) == ("e" * 64, ["SELLER_CODE"])
    # The evidence identity is its sanitized digest: another digest is another fingerprint.
    other_digest = replace(
        one, duplicate_evidence=replace(one.duplicate_evidence, evidence_digest="f" * 64)
    )  # type: ignore[type-var]
    assert candidate(other_digest, resolved(items=COVERED)).candidate_fingerprint != (
        ready.candidate_fingerprint
    )
    # A scope string is compared, never fingerprinted.
    foreign = request(duplicate_evidence=evidence(marketplace_account_id="Bearer abcdefghijklmn"))
    mismatched = candidate(foreign)
    assert "DUPLICATE_EVIDENCE_SCOPE_MISMATCH" in codes(mismatched)
    assert "Bearer" not in json.dumps(mismatched.dependencies)


def test_category_and_detail_identities_are_sanitized_before_the_fingerprint() -> None:
    hotlinked = CategorySelection(
        "https://supplier.example/category/1",
        "mapping-1",
        "taxonomy-1",
        CategoryConfirmation.OPERATOR_CONFIRMED,
    )
    assert sanitize.EXTERNAL_URL in codes(candidate(request(category=hotlinked)))
    assert candidate(request(category=hotlinked)).status is ReadinessStatus.BLOCKED
    secret_detail = DetailComposition("Bearer abcdefghijklmnop", "invented body text")
    assert sanitize.SECRET_MATERIAL in codes(candidate(request(detail=secret_detail)))
    # The builder refuses the same material even from a result that claims READY.
    ready = final()
    tampered = replace(ready, request=replace(ready.request, category=hotlinked))
    with pytest.raises(sanitize.PayloadSanitationError):
        build_payload(tampered)


def test_missing_or_inconclusive_provider_evidence_is_never_ready_when_required() -> None:
    cases = {
        None: "DUPLICATE_EVIDENCE_MISSING",
        evidence(verdict=DuplicateVerdict.INCONCLUSIVE): "DUPLICATE_EVIDENCE_INCONCLUSIVE",
        evidence(keys_checked=frozenset({DuplicateKeyKind.NORMALIZED_NAME})): (
            "DUPLICATE_EVIDENCE_INCOMPLETE"
        ),
        evidence(marketplace_account_id=OTHER_ACCOUNT): "DUPLICATE_EVIDENCE_SCOPE_MISMATCH",
        evidence(listing_identity="icbm-another-listing"): "DUPLICATE_EVIDENCE_SCOPE_MISMATCH",
        evidence(verdict=DuplicateVerdict.MATCH): "DUPLICATE_EVIDENCE_INCONCLUSIVE",
    }
    for given, code in cases.items():
        result = candidate(request(duplicate_evidence=given))
        assert (result.status, codes(result)) == (ReadinessStatus.REVIEW_REQUIRED, {code}), given
    # A target that requires no provider proof is not held to it.
    relaxed = resolved(target=target(duplicate_proof_required=False))
    assert candidate(request(duplicate_evidence=None), relaxed).status is ReadinessStatus.READY


# ---------------------------------------------------------------- the asset gate (B2)


@pytest.mark.parametrize(
    ("make", "status"),
    [
        (lambda: candidate(request(category=None)), ReadinessStatus.REVIEW_REQUIRED),
        (lambda: candidate(request(unit=UnitRequest(DRAFT, 2))), ReadinessStatus.STALE),
        (
            lambda: candidate(unit=resolved(live_registrations=(LiveRegistration("r"),))),
            ReadinessStatus.DUPLICATE,
        ),
        (
            lambda: candidate(unit=resolved(conflicts=(ConflictState("i", "UNKNOWN"),))),
            ReadinessStatus.BLOCKED,
        ),
    ],
)
def test_a_candidate_that_is_not_ready_never_permits_an_upload(make: Any, status: Any) -> None:
    # Kickoff test 12.
    result = make()
    assert (result.status, result.upload_permitted) == (status, False)


def test_the_final_preflight_needs_every_provider_asset_bound_to_this_candidate() -> None:
    # Kickoff tests 13–14.
    ready = candidate()
    assert codes(final(prepared=())) == {"PROVIDER_ASSET_IDENTITY_MISSING"}
    other = prepared_for(ready, fingerprint="0" * 64)
    mismatch = final(prepared=other)
    assert (mismatch.status, codes(mismatch)) == (
        ReadinessStatus.STALE,
        {"PREPARED_ASSET_CANDIDATE_MISMATCH"},
    )
    assert codes(final(prepared=prepared_for(ready, asset_profile="profile-x"))) == {
        "PREPARED_ASSET_PROFILE_MISMATCH"
    }
    unsafe = final(prepared=prepared_for(ready, ref="https://cdn.example/x.jpg?sig=abc"))
    assert (unsafe.status, codes(unsafe)) == (
        ReadinessStatus.BLOCKED,
        {"PREPARED_ASSET_REF_UNSAFE"},
    )
    extra = (
        *prepared_for(ready),
        PreparedAsset(
            ImageAssetKind.SOURCE_ASSET, "9" * 64, None, "asset-profile-1",
            ready.candidate_fingerprint, "provider-asset-extra",
        ),
    )  # fmt: skip
    assert codes(final(prepared=extra)) == {"PREPARED_ASSET_NOT_SELECTED"}
    with pytest.raises(ValueError, match="without any provider asset"):
        evaluate(request(), resolved(), PreflightStage.CANDIDATE, prepared_for(ready))


# PR #92 review 5257966787: ``safe_provider_reference`` also guards the prepared asset, so a
# scheme-without-``//`` reference must not ride into a durable fingerprint or payload either.
UNSAFE_ASSET_REFERENCES = {
    "scheme http": "http:cdn.example/a.jpg",
    "scheme ftp": "ftp:cdn.example/a.jpg",
    "scheme https malformed": "https:cdn.example/a.jpg",
    "signed url": "https://cdn.example/a.jpg?sig=abc",
    "protocol relative": "//cdn.example/a.jpg",
}


@pytest.mark.parametrize("case", sorted(UNSAFE_ASSET_REFERENCES))
def test_an_unsafe_prepared_asset_reference_is_never_final_ready_and_never_durable(
    case: str,
) -> None:
    reference = UNSAFE_ASSET_REFERENCES[case]
    ready = candidate()
    assert ready.status is ReadinessStatus.READY
    unsafe = final(prepared=prepared_for(ready, ref=reference))
    assert (unsafe.status, codes(unsafe)) == (
        ReadinessStatus.BLOCKED,
        {"PREPARED_ASSET_REF_UNSAFE"},
    )
    # The raw reference is no fingerprint input: only its absence is recorded.
    assert reference not in json.dumps(unsafe.dependencies, ensure_ascii=False)
    recorded = unsafe.dependencies["prepared_assets"]
    assert recorded and all(
        (asset["unsafe"], asset["provider_asset_ref"]) == (True, None) for asset in recorded
    )
    with pytest.raises(PayloadNotReadyError):
        build_payload(unsafe)
    # Nor does the builder let it through a result that claims READY.
    tampered = replace(final(), prepared_assets=prepared_for(ready, ref=reference))
    with pytest.raises(sanitize.PayloadSanitationError):
        build_payload(tampered)
    # A safe opaque reference and a plain https one still pass.
    for safe in (f"provider-asset-{case.replace(' ', '-')}", "https://cdn.example/a/b.jpg"):
        assert final(prepared=prepared_for(ready, ref=safe)).status is ReadinessStatus.READY


def test_prepared_assets_under_drifted_dependencies_are_stale() -> None:
    # Kickoff test 15: the upload was bound to the candidate; a dependency changed since.
    assets = prepared_for(candidate())
    drifted = final(unit=resolved(target=target(policy_revision="policy-rev-2")), prepared=assets)
    assert (drifted.status, codes(drifted)) == (
        ReadinessStatus.STALE,
        {"PREPARED_ASSET_CANDIDATE_MISMATCH"},
    )


def test_a_target_needing_no_provider_asset_has_the_final_preflight_only() -> None:
    no_upload = target(
        asset_policy=AssetPolicy(profile="asset-profile-1", provider_asset_identity_required=False)
    )
    first = candidate(unit=resolved(target=no_upload))
    assert (first.status, first.upload_permitted) == (ReadinessStatus.READY, False)
    only = evaluate(request(), resolved(target=no_upload), PreflightStage.FINAL, ())
    assert only.status is ReadinessStatus.READY
    payload = build_payload(only).payload
    assert all(
        asset["provider_asset_ref"] is None
        for entry in payload["items"]
        for asset in entry["publication_assets"]
    )


# ---------------------------------------------------------------- fields (ADR-0014 §4)


def test_a_missing_required_field_is_never_fabricated() -> None:
    # Kickoff test 19.
    sparse = replace(LISTING, attributes={}, notices={"origin": FieldValue("invented origin")})
    result = candidate(request(listing=sparse))
    assert {(r.code, r.subject, r.status) for r in result.reasons} == {
        ("ATTRIBUTE_REQUIRED_MISSING", "attribute:brand", ReadinessStatus.REVIEW_REQUIRED),
        ("NOTICE_REQUIRED_MISSING", "notice:manufacturer", ReadinessStatus.REVIEW_REQUIRED),
    }
    blocking = replace(
        METADATA, attributes=(FieldRule("brand", True, missing_status=ReadinessStatus.BLOCKED),)
    )
    assert candidate(request(listing=sparse), resolved(metadata=blocking)).status is (
        ReadinessStatus.BLOCKED
    )
    # An optional field stays absent in the payload: nothing is filled in for it.
    payload = build_payload(final()).payload
    assert set(payload["attributes"]) == {"brand"}
    assert set(payload["notice"]["fields"]) == {"manufacturer", "origin"}
    assert "color" not in payload["attributes"] and "care" not in payload["notice"]["fields"]


def test_detail_page_reference_needs_an_explicit_policy_permission() -> None:
    # Kickoff test 20: "상세페이지 참조" only where the reviewed rule allows it for that field.
    allowed = candidate()
    assert allowed.status is ReadinessStatus.READY
    notices = {**LISTING.notices, "manufacturer": FieldValue(detail_page_reference=True)}
    refused = candidate(request(listing=replace(LISTING, notices=notices)))
    assert codes(refused) == {"FIELD_DETAIL_REFERENCE_NOT_PERMITTED"}
    payload = build_payload(final()).payload
    assert payload["notice"]["fields"]["origin"] == {
        "detail_page_reference": True,
        "provenance": "OPERATOR_CONFIRMED",
    }


def test_an_ai_suggestion_never_satisfies_a_field_or_confirms_a_category() -> None:
    # Kickoff §9 / ADR-0014 §18.
    ai = replace(LISTING, attributes={"brand": FieldValue("guess", Provenance.AI_SUGGESTION)})
    assert codes(candidate(request(listing=ai))) == {"FIELD_AI_SUGGESTION_UNCONFIRMED"}
    ranked = CategorySelection(
        "cat-1", "mapping-1", "taxonomy-1", CategoryConfirmation.AI_SUGGESTION
    )
    assert codes(candidate(request(category=ranked))) == {"CATEGORY_NOT_CONFIRMED"}


def test_category_metadata_drives_the_rules_and_fails_closed() -> None:
    assert codes(candidate(unit=resolved(metadata=None))) == {"CATEGORY_METADATA_MISSING"}
    assert codes(candidate(unit=resolved(metadata=replace(METADATA, reviewed=False)))) == {
        "CATEGORY_METADATA_UNREVIEWED"
    }
    assert candidate(unit=resolved(metadata=replace(METADATA, leaf=False))).status is (
        ReadinessStatus.BLOCKED
    )
    old = CategorySelection(
        "cat-1", "mapping-1", "taxonomy-0", CategoryConfirmation.OPERATOR_CONFIRMED
    )
    assert codes(candidate(request(category=old))) == {"CATEGORY_TAXONOMY_STALE"}
    no_template = resolved(target=target(templates={"shipping": "ship-1"}))
    assert codes(candidate(unit=no_template)) == {"POLICY_TEMPLATE_MISSING"}
    undeclared = replace(LISTING, attributes={**LISTING.attributes, "invented": FieldValue("x")})
    assert codes(candidate(request(listing=undeclared))) == {"FIELD_UNDECLARED"}


# ---------------------------------------------------------------- options and units (§2, R3)


def test_option_compatibility_is_decided_by_metadata_never_forced() -> None:
    single = replace(METADATA, options=OptionPolicy(options_supported=False))
    assert codes(candidate(unit=resolved(metadata=single))) == {"OPTIONS_NOT_SUPPORTED"}
    small = replace(METADATA, options=OptionPolicy(options_supported=True, max_options=1))
    assert codes(candidate(unit=resolved(metadata=small))) == {"OPTION_COUNT_EXCEEDED"}
    same = replace(LISTING, options={"i1": {"size": "S"}, "i2": {"size": "S"}})
    assert codes(candidate(request(listing=same))) == {"OPTION_VALUES_NOT_DISTINCT"}
    mixed = replace(LISTING, options={"i1": {"size": "S"}, "i2": {"color": "red"}})
    assert codes(candidate(request(listing=mixed))) == {"OPTION_DIMENSIONS_INCONSISTENT"}
    missing = replace(LISTING, options={"i1": {"size": "S"}})
    assert codes(candidate(request(listing=missing))) == {"OPTION_VALUE_MISSING"}


def test_unit_shapes_follow_r3() -> None:
    # Kickoff tests 16–17 (pure part).
    open_ids = ["i1", "i2", "i3"]
    assert resolve_unit(ListingShape.SINGLE_LISTING_WITH_OPTIONS, open_ids, None)[0] == (
        "i1",
        "i2",
        "i3",
    )
    _, subset = resolve_unit(ListingShape.SINGLE_LISTING_WITH_OPTIONS, open_ids, ("i1", "i2"))
    assert {r.code for r in subset} == {"UNIT_SINGLE_NOT_COVERING"}
    _, two = resolve_unit(ListingShape.SEPARATE_LISTINGS, open_ids, ("i1", "i2"))
    assert {r.code for r in two} == {"UNIT_SEPARATE_NOT_ONE_ITEM"}
    assert resolve_unit(ListingShape.SEPARATE_LISTINGS, open_ids, ("i2",)) == (("i2",), ())
    _, closed = resolve_unit(ListingShape.SELECTED_OFFERS, open_ids, ("i1", "i9"))
    assert {r.code for r in closed} == {"UNIT_ITEM_NOT_OPEN"}
    wrong = request(unit=UnitRequest(DRAFT, 3, ("i1",)))
    assert codes(candidate(wrong)) == {"UNIT_SINGLE_NOT_COVERING"}


# ---------------------------------------------------------------- advisory, AI and sanitation


def test_the_resale_advisory_never_changes_a_price_a_status_or_a_fingerprint() -> None:
    # Kickoff test 22: 공급처 판매가 정책 참고 is display data only.
    advised = request(advisories=(ResaleAdvisory("1개 19,900 / 2개 37,900", "revision-1"),))
    plain, shown = final(), final(advised)
    assert (plain.status, plain.dependency_fingerprint) == (
        shown.status,
        shown.dependency_fingerprint,
    )
    assert build_payload(plain).payload == build_payload(shown).payload
    assert shown.advisories and not plain.advisories
    assert candidate(request(advisories=())).status is ReadinessStatus.READY


def test_secret_material_and_external_urls_never_reach_the_payload() -> None:
    # Kickoff test 23 and M5-19: refused at preflight, and by the builder itself.
    cases = [
        replace(LISTING, attributes={**LISTING.attributes, "access_token": FieldValue("x")}),
        replace(LISTING, name=FieldValue("Bearer abcdefghijklmnop")),
        replace(LISTING, attributes={"brand": FieldValue("see https://supplier.example/a.jpg")}),
    ]
    for listing in cases:
        result = candidate(request(listing=listing))
        assert result.status is ReadinessStatus.BLOCKED
        assert codes(result) & {sanitize.SECRET_MATERIAL, sanitize.EXTERNAL_URL}, listing
    hotlink = DetailComposition("detail-1", '<img src="//supplier.example/x.jpg">')
    assert sanitize.EXTERNAL_URL in codes(candidate(request(detail=hotlink)))
    ready = final()
    tampered = replace(
        ready,
        request=replace(
            ready.request,
            listing=replace(LISTING, name=FieldValue("session=abc Bearer abcdefghi1")),
        ),
    )
    with pytest.raises(sanitize.PayloadSanitationError):
        build_payload(tampered)
    assert sanitize.safe_provider_reference("https://shop-phinf.example/a/b.jpg")
    assert sanitize.safe_provider_reference("provider-asset-1/a_b.jpg")
    for unsafe in (
        "https://x.example/a.jpg?token=1",
        "https://u:p@x.example/a",
        "a b",
        "http://x.example/a.jpg",
        "//x.example/a.jpg",
        # Review 5257966787: a scheme is a scheme with or without ``//``.
        "http:x.example/a.jpg",
        "ftp:x.example/a.jpg",
        "https:x.example/a.jpg",
        "HTTP:x.example/a.jpg",
        "urn:provider:asset:1",
    ):
        assert not sanitize.safe_provider_reference(unsafe), unsafe
        assert not sanitize.safe_label(unsafe), unsafe
    assert sanitize.safe_label("lookup-contract/v1") and not sanitize.safe_label("a:b")


# ---------------------------------------------------------------- the payload (§6, §7)


def test_the_payload_is_the_exact_evaluated_preparation() -> None:
    ready = final()
    built = build_payload(ready)
    payload = built.payload
    assert payload["builder_version"] == PAYLOAD_BUILDER_VERSION
    assert (payload["marketplace_account_id"], payload["listing_identity"]) == (ACCOUNT, IDENTITY)
    assert [entry["item_id"] for entry in payload["items"]] == ["i1", "i2"]
    for entry, resolved_item in zip(payload["items"], ITEMS, strict=True):
        pin = resolved_item.pin
        assert pin is not None
        assert (entry["pricing_snapshot_id"], entry["sale_price_krw"]) == (
            pin.pricing_snapshot_id,
            pin.final_sale_price_krw,
        )
        assert entry["registration_item_key"] == registration_item_key(
            IDENTITY, resolved_item.product_group_id, resolved_item.composition_signature
        )
        assert [a["provider_asset_ref"] for a in entry["publication_assets"]] == [
            f"provider-asset-{image.sha256[:8]}" for image in resolved_item.images
        ]
    assert [item.registration_item_key for item in built.items] == [
        entry["registration_item_key"] for entry in payload["items"]
    ]
    assert built.policy_revisions["policy_revision"] == "policy-rev-1"
    with pytest.raises(PayloadNotReadyError):
        build_payload(candidate())
    with pytest.raises(PayloadNotReadyError):
        build_payload(final(request(category=None)))
