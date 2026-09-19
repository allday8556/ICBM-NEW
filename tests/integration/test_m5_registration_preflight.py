"""M5 PR-C: the registration preflight and the Snapshot builder on real owners (Issue #89 kickoff
5742880432; ADR-0014 §2–§7, §10, §13).

A migrated database with the real M4 readiness, pricing and image owners and the PR-B store. No
supplier, marketplace or AI provider is contacted: the CONNECT capability is a fixed read model,
the category metadata and the account's registration policy are invented offline sources, and
every provider lookup result and provider asset reference is invented.
"""

import contextlib
import json
import subprocess
import sys
from dataclasses import replace

import pytest

from app.config import AppConfig
from app.connect.marketplace.capability import AuthStatus, WriteStatus
from app.container import Container
from app.core.errors import InputValidationError, PolicyBlockedError
from app.products.model import ReadinessStatus
from app.register.builder import RegistrationSnapshotBuilder
from app.register.model import (
    IntentState,
    ListingShape,
    RegistrationConflictError,
    registration_item_key,
    sanitized_digest,
)
from app.register.payload import build_payload
from app.register.policy import (
    AssetPolicy,
    DuplicateKeyKind,
    StaticRegistrationMetadata,
    StaticRegistrationPolicy,
)
from app.register.preflight import RegistrationPreflightService
from app.register.preparation import (
    DuplicateMatch,
    DuplicateVerdict,
    FieldValue,
    PreflightRequest,
    PreflightResult,
    ResaleAdvisory,
    UnitRequest,
)
from app.register.store import RegistrationStore
from tests.product_support import Collections, context, count, product, raw, sold_out
from tests.register_support import (
    CID,
    MARKET,
    OPERATOR,
    Preparation,
    ReadyItem,
    bind,
    draft,
    establish,
    listing,
    metadata,
    no_match,
    preparation,
    prepared,
    ready_final,
    ready_item,
    ready_tiered,
    request,
    target,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


@pytest.fixture
def prep(container: Container, account: str) -> Preparation:
    return preparation(container, account)


@pytest.fixture
def store(container: Container) -> RegistrationStore:
    return container.registrations


def _builder(container: Container, prep: Preparation) -> RegistrationSnapshotBuilder:
    return RegistrationSnapshotBuilder(
        preflight=prep.service, registrations=container.registrations
    )


def _codes(result: PreflightResult) -> set[str]:
    return set(result.codes)


def _table_counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name <> 'alembic_version'"
            )
        ]
    return {table: count(config, table) for table in tables}


def _stale(builder: RegistrationSnapshotBuilder, ready: PreflightResult) -> None:
    with pytest.raises(RegistrationConflictError) as refused:
        builder.freeze(ready, created_by=OPERATOR, correlation_id=CID)
    assert refused.value.code == "REGISTER_PREFLIGHT_STALE"


def _single(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    source_product_id: str = "1234",
) -> tuple[ReadyItem, str, PreflightRequest]:
    item = ready_item(container, sources, source_product_id)
    draft_id = draft(store, account, [item])
    return item, draft_id, request(store, draft_id, account, [item])


# ---------------------------------------------------------------- the whole path


def test_an_operator_confirmed_unit_is_frozen_exactly_as_it_was_evaluated(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff tests 1, 12, 13 and 21: no AI provider is configured anywhere in this path.
    item, _draft_id, req = _single(container, sources, store, account)
    before = _table_counts(config)
    first = prep.service.candidate(req)
    assert (first.status, _codes(first), first.upload_permitted) == (
        ReadinessStatus.REVIEW_REQUIRED,
        {"DUPLICATE_EVIDENCE_MISSING"},
        False,
    )
    req = replace(req, duplicate_evidence=no_match(first))
    candidate = prep.service.candidate(req)
    assert (candidate.status, candidate.upload_permitted) == (ReadinessStatus.READY, True)
    unready_final = prep.service.final(req)
    assert _codes(unready_final) == {"PROVIDER_ASSET_IDENTITY_MISSING"}
    final = prep.service.final(req, prepared(candidate))
    assert final.status is ReadinessStatus.READY
    # A preflight is an evaluation: it wrote nothing anywhere.
    assert _table_counts(config) == before
    snapshot = _builder(container, prep).freeze(final, created_by=OPERATOR, correlation_id=CID)
    built = build_payload(final)
    assert (snapshot.preflight_fingerprint, snapshot.payload_hash) == (
        final.dependency_fingerprint,
        built.payload_digest,
    )
    assert snapshot.payload_hash == sanitized_digest(built.payload)
    assert (snapshot.marketplace_account_id, snapshot.listing_identity) == (
        account,
        final.resolved.listing_identity,
    )
    (frozen,) = snapshot.items
    assert (frozen.item_id, frozen.pricing_snapshot_id) == (item.item_id, item.pricing_snapshot_id)
    assert frozen.registration_item_key == built.items[0].registration_item_key
    with contextlib.closing(raw(config)) as connection:
        payload_json, assets_json, policy_json = connection.execute(
            "SELECT s.payload_json, i.publication_assets_json, s.policy_revisions_json"
            " FROM registration_snapshots s JOIN registration_item_snapshots i"
            " ON i.registration_snapshot_id = s.registration_snapshot_id"
            " WHERE s.registration_snapshot_id = ?",
            (snapshot.registration_snapshot_id,),
        ).fetchone()
    payload = json.loads(payload_json)
    assert payload == built.payload
    # Nothing is fabricated: the optional attribute the operator left out stays out.
    assert set(payload["attributes"]) == {"brand"}
    assert payload["notice"]["fields"]["origin"]["detail_page_reference"] is True
    assert [a["provider_asset_ref"] for a in json.loads(assets_json)] == [
        p.provider_asset_ref for p in prepared(candidate)
    ]
    assert json.loads(policy_json)["policy_revision"] == "policy-test-1"
    # The same preparation again is the same Snapshot, never a second one.
    again = _builder(container, prep).freeze(final, created_by=OPERATOR, correlation_id=CID)
    assert again == snapshot
    assert count(config, "registration_snapshots") == 1


def test_the_same_inputs_give_the_same_fingerprint_across_evaluations_and_a_restart(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff tests 1 and 22: the resale advisory is display data only.
    _item, _draft_id, req = _single(container, sources, store, account)
    req, final = ready_final(prep, req)
    again = prep.service.final(req, final.prepared_assets)
    fresh = preparation(container, account).service.final(req, final.prepared_assets)
    assert final.dependency_fingerprint == again.dependency_fingerprint
    assert final.dependency_fingerprint == fresh.dependency_fingerprint
    assert build_payload(final).payload_digest == build_payload(fresh).payload_digest
    advised = replace(req, advisories=(ResaleAdvisory("1개 19,900 / 2개 37,900", "rev-1"),))
    shown = prep.service.final(advised, final.prepared_assets)
    assert (shown.status, shown.dependency_fingerprint) == (
        ReadinessStatus.READY,
        final.dependency_fingerprint,
    )
    assert build_payload(shown).payload == build_payload(final).payload


# ---------------------------------------------------------------- Draft and price pin (§2)


def test_a_draft_revision_change_stales_the_preparation(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff test 3.
    _item, draft_id, req = _single(container, sources, store, account)
    req, final = ready_final(prep, req)
    with store.transaction() as unit:
        unit.change_listing_shape(
            draft_id, ListingShape.SELECTED_OFFERS, changed_by=OPERATOR, correlation_id=CID
        )
        unit.change_listing_shape(
            draft_id,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            changed_by=OPERATOR,
            correlation_id=CID,
        )
    moved = prep.service.final(req, final.prepared_assets)
    assert "DRAFT_REVISION_STALE" in _codes(moved)
    assert moved.dependency_fingerprint != final.dependency_fingerprint
    _stale(_builder(container, prep), final)


def test_a_new_price_pin_is_a_new_revision_and_a_new_fingerprint(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff tests 4–5: a pin is never silently re-read; a changed pin is a new Draft revision.
    item, draft_id, req = _single(container, sources, store, account)
    req, final = ready_final(prep, req)
    newer_context = context(fee_table_version="fee-test-2")
    newer = container.pricing.price(item.item_id, newer_context).snapshot
    assert newer is not None
    prep.policies.put(target(account, pricing_context=newer_context))
    moved_target = prep.service.final(req, final.prepared_assets)
    assert "DRAFT_PRICE_PIN_CONTEXT_STALE" in _codes(moved_target)
    assert moved_target.status is ReadinessStatus.STALE
    _stale(_builder(container, prep), final)
    with store.transaction() as unit:
        repinned = unit.change_draft_item_price(
            draft_id, item.item_id, newer.pricing_snapshot_id, changed_by=OPERATOR,
            correlation_id=CID,
        )  # fmt: skip
    current = replace(req, unit=UnitRequest(draft_id, repinned.draft_revision))
    current, refreshed = ready_final(prep, current)
    assert refreshed.dependency_fingerprint != final.dependency_fingerprint
    frozen = _builder(container, prep).freeze(refreshed, created_by=OPERATOR, correlation_id=CID)
    assert [i.pricing_snapshot_id for i in frozen.items] == [newer.pricing_snapshot_id]


def test_an_m4_repricing_under_the_same_context_supersedes_the_pin(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # A newer source revision moves the M4 current price; the Draft pin is never followed.
    item, _draft_id, req = _single(container, sources, store, account)
    req, final = ready_final(prep, req)
    run_id, _revision = sources.collect(product(price=12000), source_product_id="1234")
    container.materializer.materialize_run(run_id)
    moved = container.pricing.price(item.item_id, context()).snapshot
    assert moved is not None and moved.pricing_snapshot_id != item.pricing_snapshot_id
    result = prep.service.final(req, final.prepared_assets)
    assert "DRAFT_PRICE_PIN_SUPERSEDED" in _codes(result)
    assert result.status is ReadinessStatus.STALE
    _stale(_builder(container, prep), final)


def test_a_price_or_account_of_another_context_never_reaches_ready(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff test 5.
    _item, _draft_id, req = _single(container, sources, store, account)
    prep.policies.put(target(account, pricing_context=context(account_id=f"mpa-{'0' * 32}")))
    wrong = prep.service.candidate(req)
    assert wrong.status is ReadinessStatus.BLOCKED
    assert "TARGET_PRICING_CONTEXT_INVALID" in _codes(wrong)
    prep.policies.put(target(account))
    foreign = replace(req, unit=replace(req.unit, item_ids=("not-an-open-item",)))
    assert {"UNIT_ITEM_NOT_OPEN", "UNIT_SINGLE_NOT_COVERING"} <= _codes(
        prep.service.candidate(foreign)
    )
    # No policy for the account means no evaluation at all (fail-closed).
    empty = RegistrationPreflightService(
        registrations=container.registrations,
        readiness=container.product_readiness,
        pricing=container.pricing,
        images=container.images,
        capability=prep.capability,
        metadata=prep.metadata,
        policies=StaticRegistrationPolicy(),
    )
    with pytest.raises(PolicyBlockedError, match="policy"):
        empty.candidate(req)
    # After a rebinding the account is not bound to its identity: never READY (review).
    bind(config, MARKET, "uid-market-a-2")
    rebound = prep.service.candidate(req)
    assert "ACCOUNT_BINDING_MISMATCH" in _codes(rebound)
    assert rebound.status is not ReadinessStatus.READY


# ---------------------------------------------------------------- M4 truth (kickoff §6)


def test_m4_blocked_stale_and_review_propagate(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff tests 6–8, on real M4 state.
    blocked = ready_item(container, sources, "sold-out-1", fields=product(stock=sold_out()))
    blocked_draft = draft(store, account, [blocked])
    result = prep.service.candidate(request(store, blocked_draft, account, [blocked]))
    assert result.status is ReadinessStatus.BLOCKED
    assert "M4_BASE.SOURCE_STOCK_SOLD_OUT" in _codes(result)
    _item, _draft_id, req = _single(container, sources, store, account, "stale-1")
    container.images.qa_rule_version = "image-qa/test-2"
    stale = prep.service.candidate(req)
    assert stale.status is ReadinessStatus.STALE
    assert "M4_BASE.IMAGE_QA_STALE" in _codes(stale)
    run_id, _revision = sources.collect(product(), source_product_id="review-1")
    fresh_item = container.materializer.materialize_run(run_id).item_id
    assert fresh_item is not None
    price = container.pricing.price(fresh_item, context()).snapshot
    assert price is not None
    unselected = ReadyItem(fresh_item, "", price.pricing_snapshot_id, _revision)
    review_draft = draft(store, account, [unselected])
    review = prep.service.candidate(request(store, review_draft, account, [unselected]))
    assert "M4_BASE.IMAGE_SELECTION_MISSING" in _codes(review)
    assert ReadinessStatus.REVIEW_REQUIRED in {r.status for r in review.reasons}


# ---------------------------------------------------------------- conflict and duplicate (§10, §13)


def _send(container: Container, prep: Preparation, final: PreflightResult) -> tuple[str, str]:
    snapshot = _builder(container, prep).freeze(final, created_by=OPERATOR, correlation_id=CID)
    with container.registrations.transaction() as unit:
        batch = unit.create_batch(
            MARKET, snapshot.marketplace_account_id, created_by=OPERATOR, correlation_id=CID
        )
        intent = unit.create_intent(
            batch, snapshot.registration_snapshot_id, created_by=OPERATOR, correlation_id=CID
        ).intent_id
        attempt = unit.start_attempt(
            intent,
            sanitized_request={"request": "sanitized"},
            sanitizer_profile_version="sanitizer-test-1",
            correlation_id=CID,
        ).attempt_id
    return intent, attempt


def test_an_unresolved_unknown_prevents_ready_and_no_override_releases_it(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff tests 9–10.
    from app.connect.marketplace.capability import RemoteOutcome

    item, _draft_id, req = _single(container, sources, store, account)
    _req, final = ready_final(prep, req)
    intent, attempt = _send(container, prep, final)
    with store.transaction() as unit:
        unit.finish_attempt(attempt, remote_outcome=RemoteOutcome.UNKNOWN, correlation_id=CID)
    assert store.intent(intent).state is IntentState.UNKNOWN  # type: ignore[union-attr]
    second_draft = draft(store, account, [item])
    second = request(store, second_draft, account, [item])
    blocked = prep.service.candidate(second)
    assert blocked.status is ReadinessStatus.BLOCKED
    assert f"intent:{intent}" in {r.subject for r in blocked.reasons}
    with store.transaction() as unit:
        unit.record_duplicate_override(
            MARKET, account, item.group, reason="intentional", approved_by=OPERATOR,
            correlation_id=CID,
        )  # fmt: skip
    still = prep.service.candidate(second)
    assert (still.status, "UNRESOLVED_CREATE_CONFLICT" in _codes(still)) == (
        ReadinessStatus.BLOCKED,
        True,
    )
    assert still.upload_permitted is False


def test_a_live_registration_is_duplicate_until_an_override_covers_it(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff test 11.
    from app.connect.marketplace.capability import RemoteOutcome

    item, _draft_id, req = _single(container, sources, store, account)
    _req, final = ready_final(prep, req)
    intent, attempt = _send(container, prep, final)
    with store.transaction() as unit:
        unit.finish_attempt(
            attempt, remote_outcome=RemoteOutcome.APPLIED_PROVEN, marketplace_product_id="mp-1",
            correlation_id=CID,
        )  # fmt: skip
    snapshot = store.snapshot(store.intent(intent).registration_snapshot_id)  # type: ignore[union-attr]
    assert snapshot is not None
    with store.transaction() as unit:
        registration = unit.confirm_registration(
            intent, comparison_contract_version="c-1", normalizer_version="n-1",
            sanitized_readback={"read_back": "sanitized"}, published_state="SALE",
            option_ids={i.registration_item_key: None for i in snapshot.items},
            created_by=OPERATOR, correlation_id=CID,
        )  # fmt: skip
    second_draft = draft(store, account, [item])
    second = request(store, second_draft, account, [item])
    first = prep.service.candidate(second)
    second = replace(second, duplicate_evidence=no_match(first))
    duplicate = prep.service.candidate(second)
    assert (duplicate.status, _codes(duplicate)) == (
        ReadinessStatus.DUPLICATE,
        {"LIVE_REGISTRATION_EXISTS"},
    )
    assert f"registration:{registration.registration_id}" in {r.subject for r in duplicate.reasons}
    strong = replace(
        second,
        duplicate_evidence=replace(
            no_match(first),
            verdict=DuplicateVerdict.MATCH,
            matches=(DuplicateMatch(DuplicateKeyKind.SELLER_CODE, "provider-listing-1"),),
        ),
    )
    assert "PROVIDER_DUPLICATE_FOUND" in _codes(prep.service.candidate(strong))
    with store.transaction() as unit:
        unit.record_duplicate_override(
            MARKET, account, item.group, reason="intentional second listing",
            approved_by=OPERATOR, correlation_id=CID,
        )  # fmt: skip
    assert prep.service.candidate(second).status is ReadinessStatus.READY
    assert prep.service.candidate(strong).status is ReadinessStatus.READY


# ---------------------------------------------------------------- the asset gate (B2)


def test_prepared_assets_bind_to_their_candidate_and_drift_is_stale(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff tests 12–15.
    _item, _draft_id, req = _single(container, sources, store, account)
    first = prep.service.candidate(req)
    assert (first.status, first.upload_permitted) == (ReadinessStatus.REVIEW_REQUIRED, False)
    req = replace(req, duplicate_evidence=no_match(first))
    candidate = prep.service.candidate(req)
    assert candidate.upload_permitted
    foreign = prepared(candidate, fingerprint="0" * 64)
    assert _codes(prep.service.final(req, foreign)) == {"PREPARED_ASSET_CANDIDATE_MISMATCH"}
    assets = prepared(candidate)
    prep.policies.put(target(account, policy_revision="policy-test-2"))
    drifted = prep.service.final(req, assets)
    assert (drifted.status, _codes(drifted)) == (
        ReadinessStatus.STALE,
        {"PREPARED_ASSET_CANDIDATE_MISMATCH"},
    )
    # Back on the new candidate, fresh assets for it make the final READY again.
    renewed = prep.service.candidate(req)
    assert prep.service.final(req, prepared(renewed)).status is ReadinessStatus.READY


# ---------------------------------------------------------------- units and shapes (R3)


def test_a_single_listing_uses_every_open_item(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff test 16.
    tiers = ready_tiered(container, sources, "tiered-single")
    every = [tiers[1], tiers[2], tiers[3]]
    draft_id = draft(store, account, every)
    req = request(store, draft_id, account, every)
    subset = replace(req, unit=replace(req.unit, item_ids=(tiers[1].item_id, tiers[2].item_id)))
    refused = prep.service.candidate(subset)
    assert (refused.status, "UNIT_SINGLE_NOT_COVERING" in _codes(refused)) == (
        ReadinessStatus.BLOCKED,
        True,
    )
    req, final = ready_final(prep, req)
    snapshot = _builder(container, prep).freeze(final, created_by=OPERATOR, correlation_id=CID)
    assert [i.item_id for i in snapshot.items] == [i.item_id for i in every]
    assert {i.registration_item_key for i in snapshot.items} == {
        registration_item_key(snapshot.listing_identity, i.group_id_at_registration,
                              i.composition_signature)
        for i in snapshot.items
    }  # fmt: skip


def test_separate_listings_prepare_one_item_per_unit(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff test 17.
    tiers = ready_tiered(container, sources, "tiered-separate")
    both = [tiers[1], tiers[2]]
    draft_id = draft(store, account, both, ListingShape.SEPARATE_LISTINGS)
    two = request(store, draft_id, account, both)
    assert "UNIT_SEPARATE_NOT_ONE_ITEM" in _codes(prep.service.candidate(two))
    identities = []
    for chosen in both:
        one = request(
            store,
            draft_id,
            account,
            [chosen],
            unit=UnitRequest(draft_id, two.unit.expected_draft_revision, (chosen.item_id,)),
        )
        _req, final = ready_final(prep, one)
        snapshot = _builder(container, prep).freeze(final, created_by=OPERATOR, correlation_id=CID)
        assert [i.item_id for i in snapshot.items] == [chosen.item_id]
        identities.append(snapshot.listing_identity)
    assert len(set(identities)) == 2


def test_a_selected_subset_is_deterministic(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff test 18.
    tiers = ready_tiered(container, sources, "tiered-selected")
    draft_id = draft(store, account, [tiers[1], tiers[2], tiers[3]], ListingShape.SELECTED_OFFERS)
    chosen = [tiers[1], tiers[3]]
    results = []
    for order in ((tiers[3].item_id, tiers[1].item_id), (tiers[1].item_id, tiers[3].item_id)):
        req = request(store, draft_id, account, chosen, unit=UnitRequest(draft_id, 4, order))
        results.append(ready_final(prep, req)[1])
    assert results[0].dependency_fingerprint == results[1].dependency_fingerprint
    assert results[0].resolved.unit_item_ids == (tiers[1].item_id, tiers[3].item_id)
    snapshot = _builder(container, prep).freeze(results[1], created_by=OPERATOR, correlation_id=CID)
    assert [i.item_id for i in snapshot.items] == [tiers[1].item_id, tiers[3].item_id]


def test_the_listing_identity_changes_only_once_an_intent_names_the_unit(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    _item, _draft_id, req = _single(container, sources, store, account)
    req, final = ready_final(prep, req)
    identity = final.resolved.listing_identity
    snapshot = _builder(container, prep).freeze(final, created_by=OPERATOR, correlation_id=CID)
    assert prep.service.candidate(req).resolved.listing_identity == identity
    with store.transaction() as unit:
        batch = unit.create_batch(MARKET, account, created_by=OPERATOR, correlation_id=CID)
        unit.create_intent(
            batch, snapshot.registration_snapshot_id, created_by=OPERATOR, correlation_id=CID
        )
    after = prep.service.candidate(req).resolved
    assert (after.identity_generation, after.listing_identity != identity) == (1, True)


# ---------------------------------------------------------------- freshness before a freeze


def test_every_dependency_drift_refuses_the_freeze(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # First kickoff §14: a category, taxonomy, policy, image/QA, account, auth or conflict change
    # after the final READY refuses the freeze; nothing is frozen from the older evaluation.
    _item, _draft_id, req = _single(container, sources, store, account)
    _req, final = ready_final(prep, req)
    builder = _builder(container, prep)
    drifts = [
        (lambda: prep.metadata.put(metadata(metadata_revision="metadata-test-2")),
         lambda: prep.metadata.put(metadata())),
        (lambda: prep.policies.put(target(account, taxonomy_revision="taxonomy-test-2")),
         lambda: prep.policies.put(target(account))),
        (lambda: prep.policies.put(target(account, templates={"shipping": "s", "returns": "r"})),
         lambda: prep.policies.put(target(account))),
        (lambda: setattr(container.images, "qa_rule_version", "image-qa/test-9"),
         lambda: setattr(container.images, "qa_rule_version", "image-qa/v1")),
        (lambda: setattr(prep.capability, "auth", AuthStatus.NOT_READY),
         lambda: setattr(prep.capability, "auth", AuthStatus.READY)),
        (lambda: bind(config, MARKET, "uid-market-a-9"),
         lambda: bind(config, MARKET, "uid-market-a-1")),
    ]  # fmt: skip
    for change, restore in drifts:
        change()
        _stale(builder, final)
        restore()
    assert count(config, "registration_snapshots") == 0
    # Operator inputs are part of the preparation: new evidence or a new detail composition is a
    # new evaluation with another fingerprint, never the frozen older one.
    newer = replace(final.request, detail=replace(final.request.detail, composition_revision="d2"))  # type: ignore[type-var]
    assert (
        prep.service.final(newer, final.prepared_assets).dependency_fingerprint
        != final.dependency_fingerprint
    )
    assert builder.freeze(final, created_by=OPERATOR, correlation_id=CID)
    with pytest.raises(InputValidationError, match="final READY"):
        builder.freeze(
            prep.service.candidate(final.request), created_by=OPERATOR, correlation_id=CID
        )


def test_without_provider_assets_the_fingerprint_alone_guards_the_freeze(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # A target that needs no provider-issued asset has the final preflight only, so no prepared
    # asset binds the candidate: a drift that stays READY is refused by its fingerprint alone.
    no_assets = AssetPolicy(profile="asset-profile-test-1", provider_asset_identity_required=False)
    prep.policies.put(target(account, asset_policy=no_assets))
    _item, _draft_id, req = _single(container, sources, store, account)
    first = prep.service.candidate(req)
    req = replace(req, duplicate_evidence=no_match(first))
    candidate = prep.service.candidate(req)
    assert (candidate.status, candidate.upload_permitted) == (ReadinessStatus.READY, False)
    final = prep.service.final(req)
    assert final.status is ReadinessStatus.READY
    prep.metadata.put(metadata(metadata_revision="metadata-test-2"))
    drifted = prep.service.final(req)
    assert drifted.status is ReadinessStatus.READY
    assert drifted.dependency_fingerprint != final.dependency_fingerprint
    _stale(_builder(container, prep), final)
    assert count(config, "registration_snapshots") == 0
    frozen = _builder(container, prep).freeze(drifted, created_by=OPERATOR, correlation_id=CID)
    assert all(
        asset["provider_asset_ref"] is None
        for asset in json.loads(_one_item_assets(config, frozen.registration_snapshot_id))
    )


def _one_item_assets(config: AppConfig, snapshot_id: str) -> str:
    with contextlib.closing(raw(config)) as connection:
        (assets,) = connection.execute(
            "SELECT publication_assets_json FROM registration_item_snapshots"
            " WHERE registration_snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    return str(assets)


def test_secret_material_never_reaches_a_snapshot(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Kickoff test 23.
    item, _draft_id, req = _single(container, sources, store, account)
    secret = replace(
        req,
        listing=listing([item], attributes={"brand": FieldValue("x"), "api_key": FieldValue("y")}),
    )
    result = prep.service.candidate(secret)
    assert result.status is ReadinessStatus.BLOCKED
    assert "PAYLOAD_SECRET_MATERIAL" in _codes(result)
    assert count(config, "registration_snapshots") == 0


# ---------------------------------------------------------------- boundary (kickoff 24–25)


def test_the_production_wiring_fails_closed_and_write_stays_unverified(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
) -> None:
    # Kickoff test 25, and the wiring: no account has a registration policy yet, and the real
    # capability owner knows no invented marketplace.
    _item, _draft_id, req = _single(container, sources, store, account)
    with pytest.raises(PolicyBlockedError):
        container.registration_preflight.candidate(req)
    real_capability = RegistrationPreflightService(
        registrations=container.registrations,
        readiness=container.product_readiness,
        pricing=container.pricing,
        images=container.images,
        capability=container.marketplace_capability,
        metadata=StaticRegistrationMetadata((metadata(),)),
        policies=StaticRegistrationPolicy((target(account),)),
    )
    unavailable = real_capability.candidate(req)
    assert "CONNECT_CAPABILITY_UNAVAILABLE" in _codes(unavailable)
    assert unavailable.status is ReadinessStatus.BLOCKED
    view = container.marketplace_capability.capability("smartstore")
    assert view.write.status is WriteStatus.UNVERIFIED


def test_no_provider_ai_or_http_module_is_reachable_from_the_preparation() -> None:
    # Kickoff test 24: importing every PR-C module loads no transport, caller or AI client.
    code = (
        "import sys; import app.register.builder, app.register.preflight, app.register.payload;"
        " print('\\n'.join(sorted(sys.modules)))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    forbidden = (
        "httpx",
        "requests",
        "urllib3",
        "aiohttp",
        "playwright",
        "anthropic",
        "openai",
        "integrations.marketplaces",
        "integrations.suppliers.transport",
        "app.connect.sessions",
        "app.connect.credentials",
        "app.connect.smartstore.service",
        "app.connect.smartstore.credentials",
        "app.connect.marketplace.service",
        "app.connect.service",
        "app.ai",
    )
    assert [m for m in loaded if m.startswith(forbidden)] == []
