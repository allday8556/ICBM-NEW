"""M4 and REGISTER review producers, and authoritative review counts (Gate 2 G2-C, ADR-0016).

What this proves, over the real application and a real migrated database:
- the reviewed mappings: M4 base readiness's REVIEW_REQUIRED reasons and REGISTER's UNKNOWN,
  MISMATCH and PAUSED states are indexed by reference, each as one item of one kind, and nothing
  else is (§6); an M4 reason with no reviewed kind fails the derivation closed;
- counts come from durable OPEN rows. ``NOT_WIRED`` (a producer that can emit the kind does not
  exist or never completed a full pass) and ``NOT_CURRENT`` (every producer wired, one not
  current) are distinct, and neither carries a count, so neither can read as zero (§7);
- STOCK is never authoritative on COLLECT's coverage alone;
- a watermark covers only the owner truth it was fenced on: an owner that moved since is not
  current at once, and whole-owner churn makes progress but never publishes CURRENT;
- G2-19 for each new producer: a missed condition is recreated exactly once by the startup pass
  after a restart, and by the periodic pass in a running process, with no owner write between.

No provider is contacted and nothing is sent: every marketplace, account and value is invented.
"""

import contextlib
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.connect.marketplace.capability import RemoteOutcome
from app.container import Container, build_container
from app.core.errors import ErrorClass
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.db.database import create_sqlite_engine
from app.db.migrate import alembic_config, current_revision, upgrade_to_head
from app.products.model import MemberStatus, ReadinessStatus, Reason
from app.register.model import OPERATOR_RESUMABLE, ScopePauseReason
from app.register.store import ItemSnapshotSpec, SnapshotSpec
from app.review.collect_producer import COLLECT_PRODUCER
from app.review.counts import (
    EMITTERS,
    OPERATE_STOCK_PRODUCER,
    REVIEW_PRODUCER_NO_FULL_PASS,
    REVIEW_PRODUCER_NOT_IMPLEMENTED,
    ReviewCounts,
)
from app.review.coverage import (
    REVIEW_COVERAGE_NO_PASS_THIS_RUN,
    REVIEW_INDEX_FAILURE_UNRECOVERED,
    REVIEW_OWNER_MOVED_DURING_PASS,
    REVIEW_OWNER_MOVED_SINCE_PASS,
)
from app.review.model import CountState, ReviewDisposition, ReviewKind, ReviewState
from app.review.products_producer import (
    EXCLUDED,
    MAPPING,
    PRODUCTS_PRODUCER,
    REVIEW_CONDITION_UNMAPPED,
)
from app.review.reconciler import CHURN_RETRIES
from app.review.register_producer import REGISTER_PRODUCER
from app.screens.contracts import ScreenState
from tests.collect_submit_support import served
from tests.conftest import make_config
from tests.integration.test_g2b_collect_review import (
    UnclearShop,
    collect_unclear,
    hold_the_running_recovery,
    wait_for,
)
from tests.product_support import Collections, product, raw, review, sold_out
from tests.register_support import MARKET, OPERATOR, draft, establish, ready_item, select_and_pass
from tests.support import FakeClock

pytestmark = pytest.mark.integration

CID = "cid-g2c"
ENDPOINT = "product_registration"
REPO = Path(__file__).resolve().parents[2]
# The class each brake is recorded with (ADR-0014 §26): the schema pairs no other.
CAUSE_CLASS = {
    ScopePauseReason.AUTH: ErrorClass.AUTH,
    ScopePauseReason.POLICY: ErrorClass.POLICY_BLOCKED,
    ScopePauseReason.FAILURE_BUDGET: None,
}


@contextlib.contextmanager
def running(config: AppConfig, clock: FakeClock) -> Iterator[Container]:
    """One process on the data directory, composed but not serving: no lifespan runs."""
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config, ownership=lease, clock=clock, secret_store=MemorySecretStore()
        )
        try:
            yield built
        finally:
            built.db.dispose()


def open_of(config: AppConfig, producer: str) -> list[tuple[str, str, str, str]]:
    with contextlib.closing(raw(config)) as connection:
        return [
            (r[0], r[1], r[2], r[3])
            for r in connection.execute(
                "SELECT kind, subject, reason_code, source_identity FROM review_items"
                " WHERE state = 'OPEN' AND producer = ? ORDER BY kind, subject, reason_code",
                (producer,),
            )
        ]


def coverage_of(container: Container, producer: str) -> Any:
    (found,) = [c for c in container.review_reconciler.coverage() if c.producer == producer]
    return found


def counts(container: Container) -> dict[ReviewKind, Any]:
    return ReviewCounts(container.review_items, container.review_reconciler).open_counts()


# ---------------------------------------------------------------- REGISTER owner fixtures


def _freeze(container: Container, draft_id: str, item_id: str) -> str:
    store = container.registrations
    found = store.draft(draft_id)
    assert found is not None
    spec = SnapshotSpec(
        draft_id=draft_id,
        draft_revision=found.draft_revision,
        listing_identity=f"icbm-{uuid.uuid4().hex}",
        preflight_rule_version="preflight-test-1",
        preflight_fingerprint="a" * 64,
        category_mapping_revision="category-test-1",
        taxonomy_revision="taxonomy-test-1",
        policy_revisions={"shipping_template": "shipping-test-1"},
        detail_composition_revision="detail-test-1",
        sanitizer_profile_version="sanitizer-test-1",
        payload={"name": "invented listing name", "items": 1},
        items=[
            ItemSnapshotSpec(
                item_id=item_id,
                source_snapshot={"binding": "copied at registration"},
                publication_assets=[{"artifact_sha256": "b" * 64, "provider_asset": "asset-1"}],
                outbound_values={"option_value": "invented option"},
            )
        ],
    )
    with store.transaction() as unit:
        return unit.freeze_snapshot(
            spec, created_by=OPERATOR, correlation_id=CID
        ).registration_snapshot_id


def sent_intent(
    container: Container, config: AppConfig, account: str, outcome: RemoteOutcome
) -> tuple[str, str, str]:
    """An Intent whose one attempt finished with ``outcome``: (intent, attempt, draft)."""
    item = ready_item(
        container, Collections.of(container, config), source_product_id=uuid.uuid4().hex[:8]
    )
    draft_id = draft(container.registrations, account, [item])
    snapshot = _freeze(container, draft_id, item.item_id)
    with container.registrations.transaction() as unit:
        batch = unit.create_batch(MARKET, account, created_by=OPERATOR, correlation_id=CID)
        intent = unit.create_intent(
            batch, snapshot, created_by=OPERATOR, correlation_id=CID
        ).intent_id
        attempt = unit.start_attempt(
            intent,
            sanitized_request={"request": "sanitized"},
            sanitizer_profile_version="sanitizer-test-1",
            correlation_id=CID,
        ).attempt_id
        unit.finish_attempt(
            attempt,
            remote_outcome=outcome,
            marketplace_product_id="mp-invented-1"
            if outcome is RemoteOutcome.APPLIED_PROVEN
            else None,
            correlation_id=CID,
        )
    return intent, attempt, draft_id


def pause(container: Container, account: str, reason: ScopePauseReason) -> None:
    with container.registrations.transaction() as unit:
        unit.pause_scope(
            MARKET,
            account,
            ENDPOINT,
            reason=reason,
            policy_version="policy-test-1",
            error_class=CAUSE_CLASS[reason],
            actor=OPERATOR,
            correlation_id=CID,
        )


def resume(container: Container, account: str) -> None:
    with container.registrations.transaction() as unit:
        unit.resume_scope(
            MARKET,
            account,
            ENDPOINT,
            actor=OPERATOR,
            reason="operator-checked",
            correlation_id=CID,
            allowed_reasons=OPERATOR_RESUMABLE,
        )


# ---------------------------------------------------------------- the M4 mapping (§6)


def m4_item(container: Container, config: AppConfig, product_id: str, **fields: Any) -> Any:
    run_id, revision = Collections.of(container, config).collect(
        product(**fields), source_product_id=product_id
    )
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None and result.product_group_id is not None, result
    return result, revision


def test_the_m4_mapping_indexes_base_readiness_review_reasons_and_nothing_else(
    container: Container, config: AppConfig
) -> None:
    # An unclear stock: M4 projects it as SOURCE_CORE_FIELD_REVIEW_REQUIRED, which is COLLECT's
    # condition and is not indexed a second time. The missing image selection is M4's own.
    result, revision = m4_item(container, config, "901", stock=review("#stock"))
    readiness = container.product_readiness.base_readiness(result.item_id)
    assert "SOURCE_CORE_FIELD_REVIEW_REQUIRED" in {r.code for r in readiness.reasons}
    container.review_reconciler.full_passes()
    assert open_of(config, PRODUCTS_PRODUCER) == [
        (
            "COLLECT_EVIDENCE",
            "images",
            "IMAGE_SELECTION_MISSING",
            readiness.dependency_fingerprint,
        )
    ]
    (item,) = container.review_items.items(producer=PRODUCTS_PRODUCER)
    assert dict(item.scope) == {
        "item_id": result.item_id,
        "product_group_id": result.product_group_id,
    }
    # A membership decision still open is SOURCE_CHANGE work of the same Item.
    other, _ = m4_item(container, config, "902")
    uid = container.product_store.source_product("kmretail", "902").source_product_uid
    container.product_store.add_candidate(result.product_group_id, uid, decided_by=OPERATOR)
    assert other.product_group_id != result.product_group_id
    container.review_reconciler.full_passes()
    of_a = {
        (i.kind.value, i.subject, i.reason_code)
        for i in container.review_items.items(
            producer=PRODUCTS_PRODUCER, scope={"item_id": result.item_id}, state=ReviewState.OPEN
        )
    }
    assert of_a == {
        ("COLLECT_EVIDENCE", "images", "IMAGE_SELECTION_MISSING"),
        ("SOURCE_CHANGE", "membership:candidates", "GROUP_MEMBER_CANDIDATE_PENDING"),
    }
    # Once the owner no longer derives a condition, reconciliation alone closes its item; the
    # item whose fingerprint moved is superseded, never rewritten.
    select_and_pass(container, result.item_id, revision)
    container.review_reconciler.full_passes()
    after = {
        (i.kind.value, i.subject, i.reason_code)
        for i in container.review_items.items(
            producer=PRODUCTS_PRODUCER, scope={"item_id": result.item_id}, state=ReviewState.OPEN
        )
    }
    assert ("COLLECT_EVIDENCE", "images", "IMAGE_SELECTION_MISSING") not in after
    assert ("SOURCE_CHANGE", "membership:candidates", "GROUP_MEMBER_CANDIDATE_PENDING") in after
    closed = container.review_items.items(
        producer=PRODUCTS_PRODUCER, scope={"item_id": result.item_id}, state=ReviewState.RESOLVED
    )
    assert "IMAGE_SELECTION_MISSING" in {i.reason_code for i in closed}


def test_a_retired_group_is_history_not_review_work(
    container: Container, config: AppConfig
) -> None:
    result, _ = m4_item(container, config, "905")
    container.review_reconciler.full_passes()
    assert len(open_of(config, PRODUCTS_PRODUCER)) == 1
    # The one transition the schema allows a group (ADR-0013 §4), written as its owner would.
    with contextlib.closing(raw(config)) as connection:
        connection.execute(
            "UPDATE product_groups SET status = 'RETIRED', retired_at = '2026-09-24 00:00:00'"
            " WHERE product_group_id = ?",
            (result.product_group_id,),
        )
        connection.commit()
    assert container.review_items.producer(PRODUCTS_PRODUCER).scopes() == ()
    container.review_reconciler.full_passes()
    assert open_of(config, PRODUCTS_PRODUCER) == []
    (closed,) = container.review_items.items(producer=PRODUCTS_PRODUCER)
    assert closed.state is ReviewState.RESOLVED


def test_a_blocked_or_stale_m4_reason_is_the_owner_verdict_not_review_work(
    container: Container, config: AppConfig
) -> None:
    # A confirmed SOLD OUT is BLOCKED: the owner's own verdict, which no resolution could move.
    result, _ = m4_item(container, config, "906", stock=sold_out())
    readiness = container.product_readiness.base_readiness(result.item_id)
    assert readiness.status is ReadinessStatus.BLOCKED
    assert "SOURCE_STOCK_SOLD_OUT" in {r.code for r in readiness.reasons}
    passes = container.review_reconciler.full_passes()
    assert all(p.complete for p in passes)
    assert {r for _, _, r, _ in open_of(config, PRODUCTS_PRODUCER)} == {"IMAGE_SELECTION_MISSING"}


def test_every_m4_review_reason_has_a_reviewed_kind_or_is_excluded() -> None:
    """A source scan of M4: every reason it can derive as REVIEW_REQUIRED is mapped or excluded,
    so none is silently left out of a count."""
    literal = re.compile(r"Reason\(\s*([A-Z_]+),\s*ReadinessStatus\.REVIEW_REQUIRED")
    found: set[str] = set()
    for path in (REPO / "app" / "products").glob("*.py"):
        found |= set(literal.findall(path.read_text("utf-8")))
    import app.products.images as images
    import app.products.pricing_service as pricing_service
    import app.products.readiness as readiness

    constants = {**vars(images), **vars(pricing_service), **vars(readiness)}
    codes = {constants[name] for name in found}
    # images.py derives this one through a variable: a QA verdict that is not PASS or FAIL.
    codes.add(images.IMAGE_QA_REVIEW_REQUIRED)
    assert codes, "the scan found nothing"
    assert codes <= set(MAPPING) | EXCLUDED, codes - set(MAPPING) - EXCLUDED


def test_an_unmapped_m4_reason_fails_the_pass_closed(
    container: Container, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = m4_item(container, config, "911")
    base = container.product_readiness.base_readiness

    def with_a_new_reason(item_id: str) -> Any:
        found = base(item_id)
        extra = Reason("M4_REASON_NOBODY_REVIEWED", ReadinessStatus.REVIEW_REQUIRED)
        return type(found)(**{**found.__dict__, "reasons": (*found.reasons, extra)})

    monkeypatch.setattr(container.product_readiness, "base_readiness", with_a_new_reason)
    passed = container.review_reconciler.full_pass(PRODUCTS_PRODUCER)
    assert passed.failed == (REVIEW_CONDITION_UNMAPPED,)
    assert open_of(config, PRODUCTS_PRODUCER) == []  # the scope rolled back as one unit
    assert coverage_of(container, PRODUCTS_PRODUCER).current is False
    assert result.item_id


# ---------------------------------------------------------------- the REGISTER mapping (§6)


def test_the_register_mapping_indexes_unknown_mismatch_and_paused_scopes(
    container: Container, config: AppConfig
) -> None:
    account = establish(container, config, MARKET, "uid-g2c-map")
    unknown, attempt, draft_id = sent_intent(container, config, account, RemoteOutcome.UNKNOWN)
    applied, _, _ = sent_intent(container, config, account, RemoteOutcome.APPLIED_PROVEN)
    with container.registrations.transaction() as unit:
        unit.record_mismatch(
            applied,
            comparison_contract_version="c-1",
            normalizer_version="n-1",
            sanitized_comparison={"verdict": "MISMATCH", "field": "name"},
            actor=OPERATOR,
            correlation_id=CID,
        )
    pause(container, account, ScopePauseReason.FAILURE_BUDGET)
    container.review_reconciler.full_passes()
    items = {
        (i.subject, i.reason_code): i
        for i in container.review_items.items(producer=REGISTER_PRODUCER)
    }
    assert set(items) == {
        ("intent", "REGISTER_INTENT_UNKNOWN"),
        ("readback", "REGISTER_READBACK_MISMATCH"),
        (f"scope:{ENDPOINT}", "REGISTER_SCOPE_PAUSED_FAILURE_BUDGET"),
    }
    assert {i.kind for i in items.values()} == {ReviewKind.REGISTRATION_ERROR}
    lost = items[("intent", "REGISTER_INTENT_UNKNOWN")]
    assert dict(lost.scope) == {
        "draft_id": draft_id,
        "intent_id": unknown,
        "marketplace_account_id": account,
        "marketplace_key": MARKET,
    }
    assert lost.source_identity == attempt
    paused = items[(f"scope:{ENDPOINT}", "REGISTER_SCOPE_PAUSED_FAILURE_BUDGET")]
    assert (dict(paused.scope), paused.source_identity) == (
        {"marketplace_account_id": account, "marketplace_key": MARKET},
        "generation-0",
    )
    # A human resolution records history and changes no REGISTER fact: the Intent stays UNKNOWN,
    # and its item stays OPEN while the owner still derives it (§5; ADR-0014 §10).
    resolved = container.review_items.resolve(
        lost.review_item_id,
        expected_scope=lost.scope,
        expected_generation=lost.generation,
        disposition=ReviewDisposition.NO_ACTION_TAKEN,
        note=None,
        evidence=None,
        actor=OPERATOR,
        correlation_id=CID,
    )
    assert resolved.outcome.value == "CONDITION_PERSISTS"
    assert container.registrations.intent(unknown).state.value == "UNKNOWN"  # type: ignore[union-attr]
    # The owner's resume clears the pause, and only reconciliation closes its item.
    resume(container, account)
    container.review_reconciler.full_passes()
    assert container.review_items.item(paused.review_item_id).state is ReviewState.RESOLVED


# ---------------------------------------------------------------- counts (§7)


def test_counts_come_from_durable_rows_and_not_wired_is_never_zero(
    container: Container, config: AppConfig
) -> None:
    Collections.of(container, config).collect(
        product(stock=review("#stock")), source_product_id="921"
    )
    container.review_reconciler.full_passes()
    found = counts(container)
    stock = found[ReviewKind.STOCK]
    # COLLECT's STOCK item is durable and known, and COLLECT is wired and current; but OPERATE's
    # stock workflow can emit STOCK too and has no producer, so there is no count.
    assert (stock.state, stock.open, stock.open_known) == (CountState.NOT_WIRED, None, 1)
    assert [(e.producer, e.wired, e.current, e.reason) for e in stock.emitters] == [
        (COLLECT_PRODUCER, True, True, None),
        (OPERATE_STOCK_PRODUCER, False, False, REVIEW_PRODUCER_NOT_IMPLEMENTED),
    ]
    for kind in (ReviewKind.COMPLIANCE, ReviewKind.FULFILLMENT, ReviewKind.SOURCE_CHANGE):
        assert (found[kind].state, found[kind].open) == (CountState.NOT_WIRED, None)
    assert found[ReviewKind.COLLECT_EVIDENCE].state is CountState.NOT_WIRED
    registration = found[ReviewKind.REGISTRATION_ERROR]
    assert (registration.state, registration.open) == (CountState.CURRENT, 0)
    # The screens rest no verdict on a count that is not authoritative.
    soldout = container.screens.soldout()
    assert (soldout.meta.state, soldout.meta.empty_reason) == (ScreenState.READY, None)
    assert (soldout.stock_review.state, soldout.stock_review.open) == (CountState.NOT_WIRED, None)
    assert container.screens.dashboard().meta.state is ScreenState.READY


def test_stock_is_never_authoritative_on_collect_coverage_alone(container: Container) -> None:
    container.review_reconciler.full_passes()
    stock = counts(container)[ReviewKind.STOCK]
    assert coverage_of(container, COLLECT_PRODUCER).current is True
    assert (stock.state, stock.open, stock.open_known) == (CountState.NOT_WIRED, None, 0)
    assert stock.authoritative_zero is False
    assert EMITTERS[ReviewKind.STOCK] == (COLLECT_PRODUCER, OPERATE_STOCK_PRODUCER)
    assert container.screens.soldout().meta.empty_reason is None


def test_not_wired_and_not_current_are_distinct(config: AppConfig, clock: FakeClock) -> None:
    with running(config, clock) as first:
        # Before its first full reconciliation ever, a producer is not wired (G2-14).
        registration = counts(first)[ReviewKind.REGISTRATION_ERROR]
        assert (registration.state, registration.emitters[0].reason) == (
            CountState.NOT_WIRED,
            REVIEW_PRODUCER_NO_FULL_PASS,
        )
        first.review_reconciler.full_passes()
        assert counts(first)[ReviewKind.REGISTRATION_ERROR].state is CountState.CURRENT
        first.review_reconciler._record_failure(REGISTER_PRODUCER, "REVIEW_INDEX_FAILED", CID)
        failed = counts(first)[ReviewKind.REGISTRATION_ERROR]
        assert (failed.state, failed.open, failed.emitters[0].reason) == (
            CountState.NOT_CURRENT,
            None,
            REVIEW_INDEX_FAILURE_UNRECOVERED,
        )
        first.review_reconciler.full_passes()
        assert counts(first)[ReviewKind.REGISTRATION_ERROR].open == 0
    with running(config, clock) as second:
        # Wired once, but not current in a new process run until its own pass completes.
        fresh = counts(second)[ReviewKind.REGISTRATION_ERROR]
        assert (fresh.state, fresh.open, fresh.emitters[0].reason) == (
            CountState.NOT_CURRENT,
            None,
            REVIEW_COVERAGE_NO_PASS_THIS_RUN,
        )


def test_a_count_is_current_only_if_coverage_held_on_both_sides_of_it(
    container: Container, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = establish(container, config, MARKET, "uid-g2c-sides")
    container.review_reconciler.full_passes()
    assert counts(container)[ReviewKind.REGISTRATION_ERROR].open == 0
    store = container.review_items
    rows = store.open_counts

    def owner_moves_while_counting() -> Any:
        read = rows()
        pause(container, account, ScopePauseReason.POLICY)  # after the rows were read
        return read

    monkeypatch.setattr(store, "open_counts", owner_moves_while_counting)
    moved = counts(container)[ReviewKind.REGISTRATION_ERROR]
    assert (moved.state, moved.open, moved.open_known) == (CountState.NOT_CURRENT, None, 0)
    assert moved.emitters[0].reason == REVIEW_OWNER_MOVED_SINCE_PASS


# ---------------------------------------------------------------- the owner fence under churn


def test_an_owner_that_moved_since_the_watermark_is_not_current_at_once(
    container: Container, config: AppConfig
) -> None:
    account = establish(container, config, MARKET, "uid-g2c-since")
    reconciler = container.review_reconciler
    reconciler.full_passes()
    assert coverage_of(container, REGISTER_PRODUCER).current is True
    reconciler._wake.clear()
    # An owner write with no fast path: nothing indexes it until a full pass.
    pause(container, account, ScopePauseReason.POLICY)
    cover = coverage_of(container, REGISTER_PRODUCER)
    assert (cover.current, cover.reason) == (False, REVIEW_OWNER_MOVED_SINCE_PASS)
    assert reconciler._wake.is_set()  # the read asked for a pass; it did not run one itself
    assert open_of(config, REGISTER_PRODUCER) == []
    reconciler.full_passes()
    registration = counts(container)[ReviewKind.REGISTRATION_ERROR]
    assert (registration.state, registration.open) == (CountState.CURRENT, 1)
    # M4 the same way: a collection materializes a Product, and M4's watermark no longer covers it.
    m4_item(container, config, "931")
    assert coverage_of(container, PRODUCTS_PRODUCER).reason == REVIEW_OWNER_MOVED_SINCE_PASS
    # The COLLECT fast path asks for a full pass too: its owner, and M4's with it, just moved.
    reconciler._wake.clear()
    reconciler.index_scope(
        COLLECT_PRODUCER, {"supplier_key": "kmretail", "source_product_id": "931"}
    )
    assert reconciler._wake.is_set()


def test_a_requested_pass_runs_promptly_whatever_the_periodic_interval(config: AppConfig) -> None:
    """Bounded reconciliation under movement: the default interval is minutes, yet an owner that
    moved is covered again as soon as a reader has seen it move."""
    assert config.review_reconcile_interval_s >= 60
    with served(config, UnclearShop()) as api:
        container: Container = api.app.state.container  # type: ignore[attr-defined]
        account = establish(container, config, MARKET, "uid-g2c-prompt")
        pause(container, account, ScopePauseReason.POLICY)
        assert coverage_of(container, REGISTER_PRODUCER).current is False  # and asks for a pass
        wait_for(lambda: coverage_of(container, REGISTER_PRODUCER).current is True)
        assert ("REGISTRATION_ERROR", f"scope:{ENDPOINT}", "REGISTER_SCOPE_PAUSED_POLICY") in {
            (k, s, r) for k, s, r, _ in open_of(config, REGISTER_PRODUCER)
        }


def test_the_collect_review_list_carries_only_collect_coverage(config: AppConfig) -> None:
    """Another producer's coverage says nothing about whether a COLLECT source's list is current."""
    with served(config, UnclearShop()) as api:
        collect_unclear(api, "4242")
        listed = api.get(
            "/api/v1/review/items",
            params={"supplier_key": "fakeshop", "source_product_id": "4242"},
            headers={"X-ICBM-Client": "pytest"},
        ).json()
    assert [c["producer"] for c in listed["coverage"]] == [COLLECT_PRODUCER]


def test_whole_owner_churn_makes_progress_but_never_publishes_current(
    container: Container, config: AppConfig
) -> None:
    account = establish(container, config, MARKET, "uid-g2c-churn")
    reconciler = container.review_reconciler
    # Wired first: one stable pass. Churn that begins before any completed pass leaves the kind
    # NOT_WIRED instead (G2-14), which proves nothing about the fence.
    reconciler.full_passes()
    assert counts(container)[ReviewKind.REGISTRATION_ERROR].state is CountState.CURRENT
    sent_intent(container, config, account, RemoteOutcome.UNKNOWN)
    store = container.review_items
    original = store.reconcile
    causes = iter([ScopePauseReason.POLICY, ScopePauseReason.FAILURE_BUDGET] * 10)

    def churning(producer: str, **kwargs: Any) -> Any:
        result = original(producer, **kwargs)
        if producer == REGISTER_PRODUCER:
            pause(container, account, next(causes))  # the owner moves after every scope
        return result

    store.reconcile = churning  # type: ignore[method-assign]
    passes = [p for p in reconciler.full_passes() if p.producer == REGISTER_PRODUCER]
    store.reconcile = original  # type: ignore[method-assign]
    # Bounded: the pass and its immediate retries, then it waits for the next requested pass.
    assert CHURN_RETRIES == 2 and len(passes) == 1 + CHURN_RETRIES
    assert all(REVIEW_OWNER_MOVED_DURING_PASS in p.failed for p in passes)
    # Progress: each attempt indexed its scope in its own unit.
    assert ("REGISTRATION_ERROR", "intent", "REGISTER_INTENT_UNKNOWN") in {
        (k, s, r) for k, s, r, _ in open_of(config, REGISTER_PRODUCER)
    }
    # Safety: never CURRENT, never a count.
    registration = counts(container)[ReviewKind.REGISTRATION_ERROR]
    assert (registration.state, registration.open) == (CountState.NOT_CURRENT, None)
    assert registration.open_known >= 1
    # Once the owner is still, one stable pass publishes it.
    reconciler.full_passes()
    settled = counts(container)[ReviewKind.REGISTRATION_ERROR]
    assert settled.state is CountState.CURRENT
    assert settled.open == len(open_of(config, REGISTER_PRODUCER))


def test_the_owner_tokens_never_return_to_an_earlier_value(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    register = container.review_items.producer(REGISTER_PRODUCER)
    products = container.review_items.producer(PRODUCTS_PRODUCER)
    account = establish(container, config, MARKET, "uid-g2c-token")
    seen = [register.truth_token()]
    # The clock is frozen: a cause replaced and then restored leaves the same row values, and the
    # token still moves every time.
    for cause in (ScopePauseReason.AUTH, ScopePauseReason.FAILURE_BUDGET, ScopePauseReason.AUTH):
        pause(container, account, cause)
        seen.append(register.truth_token())
    assert len(set(seen)) == len(seen)
    result, revision = m4_item(container, config, "941")
    tokens = [products.truth_token()]
    select_and_pass(container, result.item_id, revision)
    tokens.append(products.truth_token())
    m4_item(container, config, "942")
    tokens.append(products.truth_token())
    m4_item(container, config, "943")
    tokens.append(products.truth_token())
    uid = container.product_store.source_product("kmretail", "942").source_product_uid
    container.product_store.add_candidate(result.product_group_id, uid, decided_by=OPERATOR)
    member = container.product_store.add_candidate(
        result.product_group_id,
        container.product_store.source_product("kmretail", "943").source_product_uid,
        decided_by=OPERATOR,
    )
    tokens.append(products.truth_token())
    # A status change alone adds no row: the token still moves.
    container.product_store.change_member_status(
        member, MemberStatus.REJECTED, reason="not the same product", decided_by=OPERATOR,
        correlation_id=CID,
    )  # fmt: skip
    tokens.append(products.truth_token())
    assert len(set(tokens)) == len(tokens)
    assert clock  # frozen throughout


# ---------------------------------------------------------------- G2-19 for each new producer


def _commit_m4(api: TestClient, container: Container, config: AppConfig) -> None:
    m4_item(container, config, "5151")  # a Product materialized; nothing indexes it here


def _commit_register(api: TestClient, container: Container, config: AppConfig) -> None:
    account = establish(container, config, MARKET, f"uid-{uuid.uuid4().hex[:6]}")
    sent_intent(container, config, account, RemoteOutcome.UNKNOWN)


PRODUCERS: dict[str, tuple[str, Callable[..., None], tuple[str, str, str]]] = {
    "m4": (
        PRODUCTS_PRODUCER,
        _commit_m4,
        ("COLLECT_EVIDENCE", "images", "IMAGE_SELECTION_MISSING"),
    ),
    "register": (
        REGISTER_PRODUCER,
        _commit_register,
        ("REGISTRATION_ERROR", "intent", "REGISTER_INTENT_UNKNOWN"),
    ),
}


def _keys(config: AppConfig, producer: str) -> list[tuple[str, str, str]]:
    return [(k, s, r) for k, s, r, _ in open_of(config, producer)]


@pytest.mark.parametrize("which", sorted(PRODUCERS))
def test_a_restart_recreates_a_missed_condition_exactly_once(config: AppConfig, which: str) -> None:
    """Owner commit → no index of it in this process → restart with no new owner write → the
    startup full pass recreates the missing OPEN item exactly once."""
    producer, commit, expected = PRODUCERS[which]
    with served(config, UnclearShop()) as api:
        container: Container = api.app.state.container  # type: ignore[attr-defined]
        hold_the_running_recovery(container)
        commit(api, container, config)
        assert expected not in _keys(config, producer)
        assert coverage_of(container, producer).current is False
    with served(config, UnclearShop()) as restarted:
        container = restarted.app.state.container  # type: ignore[attr-defined]
        recovered = open_of(config, producer)
        assert _keys(config, producer).count(expected) == 1
        cover = coverage_of(container, producer)
        assert (cover.current, cover.reason) == (True, None)
    with served(config, UnclearShop()):
        assert open_of(config, producer) == recovered


@pytest.mark.parametrize("which", sorted(PRODUCERS))
def test_a_running_process_recreates_a_missed_condition_by_its_periodic_pass(
    data_dir: Path, which: str
) -> None:
    """Owner commit → no index of it → the process keeps running with no new owner write → the
    next periodic full pass recreates the item exactly once; until then it is not current."""
    producer, commit, expected = PRODUCERS[which]
    config = make_config(data_dir, review_reconcile_interval_s=0.3, review_coverage_max_age_s=30.0)
    with served(config, UnclearShop()) as api:
        container: Container = api.app.state.container  # type: ignore[attr-defined]
        gate = threading.Event()
        periodic = container.review_reconciler.full_passes

        def gated_periodic() -> Any:
            gate.wait(10)
            return periodic()

        container.review_reconciler.full_passes = gated_periodic  # type: ignore[method-assign]
        commit(api, container, config)
        assert expected not in _keys(config, producer)
        assert coverage_of(container, producer).current is False
        gate.set()  # the periodic loop, and nothing else, may run now
        wait_for(lambda: coverage_of(container, producer).current is True)
        assert _keys(config, producer).count(expected) == 1


# ---------------------------------------------------------------- the migration (0023)


def test_0023_requires_a_token_with_every_watermark_and_its_downgrade_fails_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)
    command.downgrade(alembic_config(url), "0022_g2_review_coverage")
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO review_coverage (producer, process_run_id, pass_started_at,"
            " watermark_at, full_passes, failures_recorded, updated_at) VALUES"
            " ('collect.facts', 'run-1', '2026-09-24 00:00:00', '2026-09-24 00:00:01', 1, 0,"
            " '2026-09-24 00:00:01')"
        )
        connection.commit()
    command.upgrade(alembic_config(url), "head")
    with contextlib.closing(sqlite3.connect(database)) as connection:
        # A watermark from before 0023 keeps no token: it is not current until a new pass.
        assert connection.execute("SELECT truth_digest FROM review_coverage").fetchone() == (None,)
        for message, statement in {
            "published with the owner truth digest": (
                "INSERT INTO review_coverage (producer, process_run_id, pass_started_at,"
                " watermark_at, full_passes, failures_recorded, updated_at) VALUES"
                " ('other.producer', 'run-1', '2026-09-24 00:00:00', '2026-09-24 00:00:01', 1,"
                " 0, '2026-09-24 00:00:01')"
            ),
            "renewed watermark carries a fresh owner truth digest": (
                "UPDATE review_coverage SET watermark_at = '2026-09-24 00:00:02'"
            ),
            "fenced on": "UPDATE review_coverage SET watermark_at = '2026-09-24 00:00:02',"
            " truth_digest = 'not-a-token'",
        }.items():
            with pytest.raises(sqlite3.IntegrityError, match=message):
                connection.execute(statement)
        connection.execute(
            "UPDATE review_coverage SET watermark_at = '2026-09-24 00:00:02',"
            f" truth_digest = '{'c' * 64}'"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0022_g2_review_coverage")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0023_g2_review_coverage_fence"
    finally:
        engine.dispose()
