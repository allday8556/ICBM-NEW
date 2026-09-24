"""REGISTER preparation review items, and each owner scope's review path (Gate 2 G2-C, B1–B2).

Review ``5810256789``. What this proves, over the real application and a real migrated database:
- B2: every current durable preparation's **candidate** preflight — derived by the existing
  preflight owner, exactly as Registration Management derives it — is indexed by reference, one
  REGISTRATION_ERROR item per REVIEW_REQUIRED reason, anchored on the durable preparation
  revision. True projections (``M4_BASE.*``, M4 procurement re-emitted under ``M4_PRICING.``) are
  not indexed twice. An unmapped reason fails the pass. REGISTRATION_ERROR is therefore never an
  authoritative zero while a current preparation's candidate is REVIEW_REQUIRED. The producer
  carries the same fence and the same G2-19 recovery proofs;
- a derivation's nested write is refused and rolls its unit back, even when the caller swallowed
  the refusal (the CONNECT capability read inside the preflight catches every AppError);
- B1: each existing owner screen reads its own scope's items — an M4 Product, a REGISTER account —
  with exactly its producers' coverage; a stale scope or generation is refused; resolution keeps
  the owner re-derivation, so a still-derived condition stays open.

No provider is contacted and nothing is sent.
"""

import contextlib
import re
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.core.errors import AppError
from app.db.database import DatabaseWriteReentryError
from app.products.model import ReadinessStatus, Reason
from app.register.model import ListingShape
from app.register.preparation import REASON_CODES
from app.review.coverage import ReviewCoverageStore
from app.review.model import CountState, ReviewCondition, ReviewKind, ReviewState
from app.review.owner import ReviewItemStore
from app.review.preflight_producer import (
    M4_PRICING_REVIEWED,
    M4_PROCUREMENT,
    PREFLIGHT_PRODUCER,
    REVIEWED,
    indexed,
)
from app.review.products_producer import PRODUCTS_PRODUCER, REVIEW_CONDITION_UNMAPPED
from app.review.register_producer import REGISTER_PRODUCER
from app.review.scopes import PRODUCER_SCOPE_KEYS
from tests.collect_submit_support import served
from tests.conftest import make_config
from tests.gate1_support import (
    CATEGORY,
    CLIENT,
    MARKET,
    OPERATOR,
    TAXONOMY,
    record_reviewed_metadata,
    save_policy,
)
from tests.integration.test_g2b_collect_review import (
    UnclearShop,
    hold_the_running_recovery,
    wait_for,
)
from tests.integration.test_g2c_review_counts import counts, coverage_of, open_of
from tests.product_support import Collections, product
from tests.register_support import establish

pytestmark = pytest.mark.integration

REVIEW = "/api/v1/review/items"
PREPARATIONS = "/api/v1/register/preparations"
REPO = Path(__file__).resolve().parents[2]
UNOWNED = ("REGISTRATION_ERROR", "authoring", "AUTHORING_REVISIONS_UNOWNED")


def authored_inputs(body: str = "상세 본문") -> dict[str, Any]:
    return {
        "category": {
            "category_id": CATEGORY,
            "mapping_revision": None,
            "taxonomy_revision": TAXONOMY,
            "confirmation": "OPERATOR_CONFIRMED",
        },
        "name": {"value": "합성 상품", "provenance": "OPERATOR_CONFIRMED"},
        "tags": [],
        "attributes": {"brand": {"value": "합성 브랜드"}},
        "notices": {
            "manufacturer": {"value": "합성 제조사"},
            "origin": {"detail_page_reference": True},
        },
        "options": {},
        "detail_composition_revision": None,
        "detail_body": body,
        "detail_sections": ["BODY"],
    }


def prepared(api: TestClient, container: Container, config: AppConfig) -> dict[str, str]:
    """One durable preparation of one priced M4 Item, under a durable G1-A policy and reviewed
    G1-B metadata, authored through the application: its account, Draft, Item and preparation."""
    account = establish(container, config, MARKET, f"provider-{uuid.uuid4().hex[:8]}")
    save_policy(api, account)
    record_reviewed_metadata(api)
    run_id, _ = Collections.of(container, config).collect(product(), source_product_id="1234")
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None
    policy = container.registration_preflight.target_policy(MARKET, account)
    assert policy is not None
    pin = container.pricing.price(result.item_id, policy.pricing_context).snapshot
    assert pin is not None
    with container.registrations.transaction() as unit:
        draft = unit.create_draft(
            MARKET, account, ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR, correlation_id="cid-g2c",
        )  # fmt: skip
        unit.add_draft_item(
            draft.draft_id, result.item_id, pin.pricing_snapshot_id,
            added_by=OPERATOR, correlation_id="cid-g2c",
        )  # fmt: skip
    response = api.post(
        PREPARATIONS,
        json={
            "draft_id": draft.draft_id,
            "item_ids": [result.item_id],
            "actor": OPERATOR,
            "inputs": authored_inputs(),
        },
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    return {
        "account": account,
        "draft_id": draft.draft_id,
        "item_id": result.item_id,
        "product_group_id": str(result.product_group_id),
        "preparation_id": response.json()["preparation_id"],
    }


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with served(config, UnclearShop()) as client:
        yield client


@pytest.fixture
def app_container(api: TestClient) -> Container:
    found: Container = api.app.state.container  # type: ignore[attr-defined]
    hold_the_running_recovery(found)  # each test decides when a full pass runs
    return found


def passes(container: Container) -> Any:
    """One full pass of every producer, run here; the process's own loop is held."""
    reconciler = container.review_reconciler
    return type(reconciler).full_passes(reconciler)


def keys(config: AppConfig, producer: str) -> set[tuple[str, str, str]]:
    return {(k, s, r) for k, s, r, _ in open_of(config, producer)}


# ---------------------------------------------------------------- B2: the preparation producer


def test_a_current_preparation_candidate_is_indexed_by_reference(
    api: TestClient, app_container: Container, config: AppConfig
) -> None:
    made = prepared(api, app_container, config)
    container = app_container
    passes(container)
    # What the owner derives now, through the one path Registration Management uses.
    candidate = container.registration_preparations.evaluate(made["preparation_id"])
    review = {r for r in candidate.reasons if r.status is ReadinessStatus.REVIEW_REQUIRED}
    assert any(r.code.startswith("M4_BASE.") for r in review)  # present, and not indexed here
    expected = {
        ("REGISTRATION_ERROR", r.subject or "preflight", r.code) for r in review if indexed(r.code)
    }
    assert UNOWNED in expected
    assert ("REGISTRATION_ERROR", "duplicate", "DUPLICATE_EVIDENCE_MISSING") in expected
    assert keys(config, PREFLIGHT_PRODUCER) == expected
    # Stated independently of the mapping: no verbatim M4 re-emission is indexed a second time.
    assert not [r for _, _, r in keys(config, PREFLIGHT_PRODUCER) if r.startswith("M4_BASE.")]
    assert not [
        r
        for _, _, r in keys(config, PREFLIGHT_PRODUCER)
        if r.startswith("M4_PRICING.") and r.split(".", 1)[1] in M4_PROCUREMENT
    ]
    items = container.review_items.items(producer=PREFLIGHT_PRODUCER)
    record = container.registrations.preparation(made["preparation_id"])
    assert record is not None
    assert {i.source_identity for i in items} == {record.current.preparation_revision_id}
    assert {tuple(sorted(i.scope.items())) for i in items} == {
        (
            ("draft_id", made["draft_id"]),
            ("marketplace_account_id", made["account"]),
            ("marketplace_key", MARKET),
            ("preparation_id", made["preparation_id"]),
        )
    }
    # The M4 base condition behind the projection is indexed once, by its own producer.
    assert ("COLLECT_EVIDENCE", "images", "IMAGE_SELECTION_MISSING") in keys(
        config, PRODUCTS_PRODUCER
    )
    # And REGISTRATION_ERROR is never an authoritative zero while the candidate needs review.
    registration = counts(container)[ReviewKind.REGISTRATION_ERROR]
    assert registration.state is CountState.CURRENT
    assert registration.open == len(expected) > 0
    assert [e.producer for e in registration.emitters] == [REGISTER_PRODUCER, PREFLIGHT_PRODUCER]


def test_a_revision_supersedes_and_a_frozen_unit_leaves_the_current_set(
    api: TestClient, app_container: Container, config: AppConfig
) -> None:
    made = prepared(api, app_container, config)
    container = app_container
    passes(container)
    first = container.review_items.items(producer=PREFLIGHT_PRODUCER, state=ReviewState.OPEN)
    revised = api.post(
        f"{PREPARATIONS}/{made['preparation_id']}",
        json={
            "item_ids": [made["item_id"]],
            "actor": OPERATOR,
            "inputs": authored_inputs(body="고친 본문"),
        },
        headers=CLIENT,
    )
    assert revised.status_code == 200, revised.text
    passes(container)
    record = container.registrations.preparation(made["preparation_id"])
    assert record is not None and len(record.revisions) == 2
    now = container.review_items.items(producer=PREFLIGHT_PRODUCER, state=ReviewState.OPEN)
    assert {i.source_identity for i in now} == {record.current.preparation_revision_id}
    assert {container.review_items.item(i.review_item_id).state for i in first} == {
        ReviewState.SUPERSEDED
    }
    # A Snapshot of the Draft's current revision holding the same Items: no longer work to author.
    from tests.integration.test_g2c_review_counts import _freeze

    _freeze(container, made["draft_id"], made["item_id"])
    assert container.registrations.current_preparations() == ()
    passes(container)
    assert keys(config, PREFLIGHT_PRODUCER) == set()


def test_a_refused_candidate_is_indexed_as_the_owner_states_it(
    api: TestClient, app_container: Container, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared(api, app_container, config)
    preflight = app_container.registration_preflight
    monkeypatch.setattr(preflight._policies, "target", lambda *_: None)
    passes(app_container)
    assert keys(config, PREFLIGHT_PRODUCER) == {
        ("REGISTRATION_ERROR", "preflight", "REGISTER_TARGET_POLICY_MISSING")
    }


def test_an_unmapped_preflight_reason_fails_the_pass(
    api: TestClient, app_container: Container, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared(api, app_container, config)
    container = app_container
    passes(container)
    service = container.registration_preparations
    evaluate = service.evaluate

    def with_a_new_reason(preparation_id: str, **kwargs: Any) -> Any:
        found = evaluate(preparation_id, **kwargs)
        extra = Reason("REGISTER_REASON_NOBODY_REVIEWED", ReadinessStatus.REVIEW_REQUIRED, "x")
        return type(found)(**{**found.__dict__, "reasons": (*found.reasons, extra)})

    monkeypatch.setattr(service, "evaluate", with_a_new_reason)
    before = open_of(config, PREFLIGHT_PRODUCER)
    passed = container.review_reconciler.full_pass(PREFLIGHT_PRODUCER)
    assert passed.failed == (REVIEW_CONDITION_UNMAPPED,)
    assert open_of(config, PREFLIGHT_PRODUCER) == before
    registration = counts(container)[ReviewKind.REGISTRATION_ERROR]
    assert (registration.state, registration.open) == (CountState.NOT_CURRENT, None)


def test_every_preflight_review_reason_is_reviewed() -> None:
    """A source scan of the preflight and of M4 pricing: every reason either can state as
    REVIEW_REQUIRED is indexed here or is a projection indexed elsewhere, so none is silently
    left out of a count."""
    source = (REPO / "app" / "register" / "preparation.py").read_text("utf-8")
    import app.register.preparation as preparation

    literal = {
        getattr(preparation, name) for name in re.findall(r"Reason\(\s*([A-Z_]+),\s*_R\b", source)
    }
    # Stated through a variable: a policy's missing status, a QA verdict, a duplicate signal.
    dynamic = {
        preparation.ATTRIBUTE_REQUIRED_MISSING,
        preparation.NOTICE_REQUIRED_MISSING,
        preparation.FIELD_DETAIL_REFERENCE_NOT_PERMITTED,
        preparation.PUBLICATION_ASSET_QA_NOT_PASSED,
        preparation.PROVIDER_DUPLICATE_FOUND,
        preparation.PROVIDER_DUPLICATE_WEAK_SIGNAL,
    }
    assert literal and literal | dynamic <= REVIEWED
    assert REVIEWED <= REASON_CODES
    pricing = (REPO / "app" / "products" / "pricing.py").read_text("utf-8")
    import app.products.pricing as m4_pricing

    reviewed = {getattr(m4_pricing, name) for name in re.findall(r"_review\(\s*([A-Z_]+)", pricing)}
    assert reviewed and reviewed <= M4_PRICING_REVIEWED | M4_PROCUREMENT


def test_a_swallowed_nested_write_rolls_the_review_unit_back(
    api: TestClient, app_container: Container, config: AppConfig
) -> None:
    """A derivation that tries to write is refused; a caller that swallows the refusal as an
    ordinary AppError (the preflight's CONNECT read does) cannot save the unit: it rolls back."""
    container = app_container

    class Swallowing:
        name = "test.swallowing"

        def scopes(self) -> tuple[dict[str, str], ...]:
            return ({"product_group_id": "g-1"},)

        def truth_token(self) -> str:
            return "t"

        def derive(self, scope: Any) -> tuple[ReviewCondition, ...]:
            with contextlib.suppress(AppError), container.db.write():
                pass
            return (
                ReviewCondition(
                    kind=ReviewKind.REGISTRATION_ERROR,
                    producer="test.swallowing",
                    scope={"product_group_id": "g-1"},
                    subject="x",
                    reason_code="X",
                    source_identity="s-1",
                ),
            )

    store = ReviewItemStore(
        container.db, container.clock, container.audit, producers=[Swallowing()]
    )
    with pytest.raises(DatabaseWriteReentryError):
        store.reconcile("test.swallowing", scope={"product_group_id": "g-1"}, correlation_id="c")
    assert store.items(producer="test.swallowing") == ()
    assert isinstance(ReviewCoverageStore(container.db, container.clock, container.audit), object)


def test_the_preflight_token_moves_with_what_the_candidate_reads(
    api: TestClient, app_container: Container, config: AppConfig
) -> None:
    container = app_container
    producer = container.review_items.producer(PREFLIGHT_PRODUCER)
    made = prepared(api, container, config)
    tokens = [producer.truth_token()]
    revised = api.post(
        f"{PREPARATIONS}/{made['preparation_id']}",
        json={
            "item_ids": [made["item_id"]],
            "actor": OPERATOR,
            "inputs": authored_inputs(body="두번째"),
        },
        headers=CLIENT,
    )
    assert revised.status_code == 200, revised.text
    tokens.append(producer.truth_token())
    save_policy(api, made["account"], account_scoped=True)  # a new target policy revision
    tokens.append(producer.truth_token())
    other_run, _ = Collections.of(container, config).collect(product(), source_product_id="5678")
    container.materializer.materialize_run(other_run)
    tokens.append(producer.truth_token())
    uid = container.product_store.source_product("kmretail", "5678").source_product_uid
    container.product_store.add_candidate(made["product_group_id"], uid, decided_by=OPERATOR)
    tokens.append(producer.truth_token())  # M4 membership, which is not audited
    assert len(set(tokens)) == len(tokens)


# ---------------------------------------------------------------- G2-19, the preparation producer


def test_a_restart_recreates_a_missed_preparation_condition_exactly_once(config: AppConfig) -> None:
    with served(config, UnclearShop()) as api:
        container: Container = api.app.state.container  # type: ignore[attr-defined]
        hold_the_running_recovery(container)
        prepared(api, container, config)
        assert UNOWNED not in keys(config, PREFLIGHT_PRODUCER)
        assert coverage_of(container, PREFLIGHT_PRODUCER).current is False
    with served(config, UnclearShop()) as restarted:
        container = restarted.app.state.container  # type: ignore[attr-defined]
        recovered = open_of(config, PREFLIGHT_PRODUCER)
        assert [(k, s, r) for k, s, r, _ in recovered].count(UNOWNED) == 1
        assert coverage_of(container, PREFLIGHT_PRODUCER).current is True
    with served(config, UnclearShop()):
        assert open_of(config, PREFLIGHT_PRODUCER) == recovered


def test_a_running_process_recreates_a_missed_preparation_condition(data_dir: Path) -> None:
    config = make_config(data_dir, review_reconcile_interval_s=0.3, review_coverage_max_age_s=30.0)
    with served(config, UnclearShop()) as api:
        container: Container = api.app.state.container  # type: ignore[attr-defined]
        gate = threading.Event()
        periodic = container.review_reconciler.full_passes

        def gated_periodic() -> Any:
            gate.wait(10)
            return periodic()

        container.review_reconciler.full_passes = gated_periodic  # type: ignore[method-assign]
        prepared(api, container, config)
        assert UNOWNED not in keys(config, PREFLIGHT_PRODUCER)
        assert coverage_of(container, PREFLIGHT_PRODUCER).current is False
        gate.set()
        wait_for(lambda: coverage_of(container, PREFLIGHT_PRODUCER).current is True)
        assert [(k, s, r) for k, s, r, _ in open_of(config, PREFLIGHT_PRODUCER)].count(UNOWNED) == 1


# ---------------------------------------------------------------- B1: each owner scope's path


def test_each_owner_scope_reads_its_own_items_with_its_own_coverage(
    api: TestClient, app_container: Container, config: AppConfig
) -> None:
    made = prepared(api, app_container, config)
    passes(app_container)
    product = api.get(REVIEW, params={"product_group_id": made["product_group_id"]}, headers=CLIENT)
    assert product.status_code == 200, product.text
    body = product.json()
    assert {i["producer"] for i in body["items"]} == {PRODUCTS_PRODUCER}
    assert [c["producer"] for c in body["coverage"]] == [PRODUCTS_PRODUCER]
    account = api.get(
        REVIEW,
        params={"marketplace_key": MARKET, "marketplace_account_id": made["account"]},
        headers=CLIENT,
    ).json()
    assert {i["producer"] for i in account["items"]} == {PREFLIGHT_PRODUCER}
    assert [c["producer"] for c in account["coverage"]] == [REGISTER_PRODUCER, PREFLIGHT_PRODUCER]
    assert all(i["scope"]["marketplace_account_id"] == made["account"] for i in account["items"])
    # Another account's scope never lists these items (§8).
    other = api.get(
        REVIEW,
        params={"marketplace_key": MARKET, "marketplace_account_id": "other"},
        headers=CLIENT,
    ).json()
    assert other["items"] == []
    # A combination no producer's items carry is refused, not answered as empty.
    mixed = api.get(
        REVIEW, params={"supplier_key": "kmretail", "product_group_id": "g"}, headers=CLIENT
    )
    assert (mixed.status_code, mixed.json()["error"]["code"]) == (422, "REVIEW_SCOPE_UNSUPPORTED")
    assert set(PRODUCER_SCOPE_KEYS) == set(app_container.review_items.producers)


def test_a_stale_scope_or_generation_is_refused_and_a_derived_condition_stays_open(
    api: TestClient, app_container: Container, config: AppConfig
) -> None:
    made = prepared(api, app_container, config)
    passes(app_container)
    (item,) = [
        i
        for i in app_container.review_items.items(
            producer=PREFLIGHT_PRODUCER, state=ReviewState.OPEN
        )
        if i.reason_code == "AUTHORING_REVISIONS_UNOWNED"
    ]
    url = f"{REVIEW}/{item.review_item_id}/resolve"
    wrong = dict(item.scope) | {"marketplace_account_id": "another-account"}
    for scope, generation, code in (
        (wrong, item.generation, "REVIEW_ITEM_SCOPE_MISMATCH"),
        (dict(item.scope), item.generation + 1, "REVIEW_ITEM_MOVED"),
    ):
        refused = api.post(
            url,
            json={
                "expected_scope": scope,
                "expected_generation": generation,
                "disposition": "NO_ACTION_TAKEN",
                "actor": OPERATOR,
            },
            headers=CLIENT,
        )
        assert (refused.status_code, refused.json()["error"]["code"]) == (409, code)
    assert app_container.review_items.history(item.review_item_id)[-1].event.value == "OPENED"
    resolved = api.post(
        url,
        json={
            "expected_scope": dict(item.scope),
            "expected_generation": item.generation,
            "disposition": "FOLLOW_UP_REQUIRED",
            "actor": OPERATOR,
        },
        headers=CLIENT,
    )
    assert resolved.json()["outcome"] == "CONDITION_PERSISTS"
    assert resolved.json()["item"]["state"] == "OPEN"
    # The owner still derives it: nothing about the preparation moved.
    assert "AUTHORING_REVISIONS_UNOWNED" in {
        r.code
        for r in app_container.registration_preparations.evaluate(made["preparation_id"]).reasons
    }
