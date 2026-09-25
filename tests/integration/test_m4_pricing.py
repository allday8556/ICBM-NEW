"""M4 PR-D: pricing snapshots, the current-snapshot pointer, and derived readiness (Issue #80
kickoff 5737440897; ADR-0013 §7–§8), on a migrated database through the real services.

No supplier, marketplace or AI provider is contacted, and no campaign root is opened. Every
pricing context is an invented placeholder: no marketplace fee is known to the repository.
"""

import contextlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command

from app.audit.models import AuditEventType
from app.audit.service import AuditLog
from app.collect.facts import FieldFact
from app.collect.revisions import ProductFactsRevisionStore
from app.config import AppConfig
from app.container import Container
from app.db.database import Database, create_sqlite_engine
from app.db.migrate import alembic_config, current_revision, head_revision, upgrade_to_head
from app.products.images import IMAGE_SELECTION_MISSING
from app.products.materialization import ProductMaterializer
from app.products.model import MoveReason, ReadinessStatus
from app.products.pricing import PriceBasis, PriceGuard, PricingMoveReason
from app.products.pricing_service import PricingOutcome, PricingResult, ProductPricingService
from app.products.readiness import Readiness
from app.products.store import ProductFoundationStore
from tests.product_support import (
    PRODUCT,
    SUPPLIER,
    Collections,
    conditional,
    context,
    count,
    fixed,
    minimum,
    product,
    raw,
    review,
    sold_out,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration

PRICING_TABLES = ("pricing_snapshots", "current_pricing_snapshot_moves")
PRICING_AUDIT = (
    AuditEventType.PRODUCT_PRICING_SNAPSHOT_RECORDED.value,
    AuditEventType.PRODUCT_CURRENT_PRICING_SNAPSHOT_MOVED.value,
)


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


def _item(
    container: Container,
    sources: Collections,
    fields: dict[str, FieldFact] | None = None,
    source_product_id: str = PRODUCT,
) -> str:
    """Collect a RECORDED revision and materialize it; return its default Item."""
    run_id, _revision = sources.collect(fields, source_product_id=source_product_id)
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None, result
    return result.item_id


def _recorded(result: PricingResult) -> PricingResult:
    assert result.outcome is PricingOutcome.RECORDED, result
    assert result.snapshot is not None and result.move is not None
    return result


def _pricing_counts(config: AppConfig) -> dict[str, int]:
    counts = {table: count(config, table) for table in PRICING_TABLES}
    for event_type in PRICING_AUDIT:
        counts[event_type] = count(config, "audit_events", "event_type = ?", event_type)
    return counts


def _codes(readiness: Readiness) -> list[tuple[str, str | None]]:
    return [(reason.code, reason.subject) for reason in readiness.reasons]


def _snapshot_row(config: AppConfig, snapshot_id: str) -> dict[str, Any]:
    with contextlib.closing(raw(config)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM pricing_snapshots WHERE pricing_snapshot_id = ?", (snapshot_id,)
        ).fetchone()
    return dict(row)


def _insert(config: AppConfig, table: str, row: dict[str, Any]) -> None:
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with contextlib.closing(raw(config)) as connection:
        connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))
        connection.commit()


# ---------------------------------------------------------------- 1–10 pricing source truth


def test_a_base_product_is_priced_under_its_explicit_context(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.1: one confirmed price, FREE shipping, no minimum → TARGET_MARGIN.
    item = _item(container, sources)
    ctx = context()
    result = _recorded(container.pricing.price(item, ctx))
    snapshot = result.snapshot
    assert snapshot is not None
    assert snapshot.price_basis is PriceBasis.TARGET_MARGIN
    assert (snapshot.final_sale_price_krw, snapshot.target_margin_price_krw) == (18184, 18184)
    assert (snapshot.purchase_cost_krw, snapshot.supplier_shipping_krw) == (10000, 0)
    assert snapshot.minimum_sale_price_krw is None and snapshot.price_guard is PriceGuard.OK
    assert snapshot.pricing_context_fingerprint == ctx.fingerprint
    assert (snapshot.marketplace_key, snapshot.account_id) == ("market_a", None)
    row = _snapshot_row(config, snapshot.pricing_snapshot_id)
    assert json.loads(row["pricing_context_json"]) == ctx.canonical()
    assert (row["target_margin_numerator"], row["target_margin_denominator"]) == (7, 20)
    assert (row["minimum_margin_numerator"], row["minimum_margin_denominator"]) == (1, 10)
    assert (row["cost_rounding"], row["price_rounding"]) == ("CEIL_KRW_1", "CEIL_KRW_1")
    assert result.move is not None and result.move.reason is PricingMoveReason.INITIAL
    readiness = container.product_readiness.pricing_readiness(item, ctx)
    assert readiness.status is ReadinessStatus.READY and readiness.reasons == ()


def test_fixed_supplier_shipping_is_included_exactly(
    container: Container, sources: Collections
) -> None:
    # Kickoff §11.2.
    item = _item(container, sources, product(shipping=fixed(3000)))
    snapshot = _recorded(container.pricing.price(item, context())).snapshot
    assert snapshot is not None and snapshot.supplier_shipping_krw == 3000
    assert snapshot.expected_net_profit_krw == (
        snapshot.final_sale_price_krw - 10000 - 3000 - snapshot.platform_fee_krw
    )


@pytest.mark.parametrize("stated", [15000, 20000], ids=["below target", "above target"])
def test_a_minimum_sale_price_is_the_final_price_exactly(
    container: Container, sources: Collections, stated: int
) -> None:
    # Kickoff §11.3–4: never a maximum of the minimum and the target (18,184 KRW here).
    item = _item(container, sources, product(minimum_sale_price=minimum(stated)))
    snapshot = _recorded(container.pricing.price(item, context())).snapshot
    assert snapshot is not None
    assert snapshot.price_basis is PriceBasis.MINIMUM_SALE_PRICE
    assert (snapshot.final_sale_price_krw, snapshot.target_margin_price_krw) == (stated, 18184)


@pytest.mark.parametrize(
    ("stated", "fee", "guard", "reasons"),
    [
        (10500, "0.1", PriceGuard.LOSS, ("PRICE_BELOW_MIN_MARGIN", "PRICE_LOSS")),
        (11500, "0.05", PriceGuard.BELOW_MIN_MARGIN, ("PRICE_BELOW_MIN_MARGIN",)),
    ],
    ids=["loss", "below minimum margin"],
)
def test_a_minimum_sale_price_can_block_pricing_with_every_guard_kept(
    container: Container,
    sources: Collections,
    stated: int,
    fee: str,
    guard: PriceGuard,
    reasons: tuple[str, ...],
) -> None:
    # Kickoff §11.5–6 and §11.35: both guards apply under MINIMUM_SALE_PRICE, every reason kept.
    item = _item(container, sources, product(minimum_sale_price=minimum(stated)))
    ctx = context(fee_rate=fee)
    snapshot = _recorded(container.pricing.price(item, ctx)).snapshot
    assert snapshot is not None and snapshot.price_guard is guard
    readiness = container.product_readiness.pricing_readiness(item, ctx)
    assert readiness.status is ReadinessStatus.BLOCKED
    assert tuple(code for code, _subject in _codes(readiness)) == reasons


UNRESOLVED = {
    "several prices": (product(price=(10000, 12000)), "PRICING_PURCHASE_PRICE_AMBIGUOUS"),
    "conditional shipping": (product(shipping=conditional()), "PRICING_SHIPPING_CONDITIONAL"),
    "shipping under review": (product(shipping=review(".delivery")), "PRICING_SHIPPING_UNRESOLVED"),
    "minimum under review": (
        product(minimum_sale_price=review(".minimum-price")),
        "PRICING_MINIMUM_SALE_PRICE_UNRESOLVED",
    ),
}


@pytest.mark.parametrize(("fields", "code"), UNRESOLVED.values(), ids=UNRESOLVED.keys())
def test_source_inputs_that_are_not_unambiguous_are_never_priced(
    container: Container,
    config: AppConfig,
    sources: Collections,
    fields: dict[str, FieldFact],
    code: str,
) -> None:
    # Kickoff §11.7–9 and §11.34: REVIEW_REQUIRED with the concrete reason, and no snapshot.
    item = _item(container, sources, fields)
    result = container.pricing.price(item, context())
    assert result.outcome is PricingOutcome.NOT_PRICED and result.snapshot is None
    assert [reason.code for reason in result.reasons] == [code]
    assert not any(_pricing_counts(config).values())
    readiness = container.product_readiness.pricing_readiness(item, context())
    assert readiness.status is ReadinessStatus.REVIEW_REQUIRED
    assert [c for c, _ in _codes(readiness)] == [code]


# ---------------------------------------------------------------- 11–15 context


def test_two_marketplace_contexts_price_one_item_independently(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.11: no universal price.
    item = _item(container, sources)
    a = _recorded(container.pricing.price(item, context(marketplace_key="market_a")))
    b = _recorded(
        container.pricing.price(item, context(marketplace_key="market_b", fee_rate="0.2"))
    )
    assert a.snapshot is not None and b.snapshot is not None
    assert a.snapshot.pricing_snapshot_id != b.snapshot.pricing_snapshot_id
    assert a.snapshot.final_sale_price_krw != b.snapshot.final_sale_price_krw
    assert a.move is not None and b.move is not None
    assert (a.move.sequence, b.move.sequence) == (1, 1)
    assert count(config, "current_pricing_snapshot_moves") == 2


def test_account_scoped_contexts_stay_distinct(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.12.
    item = _item(container, sources)
    for account in (None, "acct-1", "acct-2"):
        _recorded(container.pricing.price(item, context(account_id=account)))
    assert count(config, "pricing_snapshots") == 3
    assert count(config, "pricing_snapshots", "account_id IS NULL") == 1


def test_one_version_label_with_other_effective_inputs_never_aliases(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.13–14: the same labels with another fee is another context. It is not priced by
    # the earlier snapshot, and pricing it leaves the earlier context's history as it was.
    item = _item(container, sources)
    first, reused = context(fee_rate="0.1"), context(fee_rate="0.12")
    assert (first.fee_table_version, first.pricing_policy_version) == (
        reused.fee_table_version,
        reused.pricing_policy_version,
    )
    _recorded(container.pricing.price(item, first))
    stale = container.product_readiness.pricing_readiness(item, reused)
    assert stale.status is ReadinessStatus.STALE
    assert _codes(stale) == [("PRICING_SNAPSHOT_MISSING", None)]
    assert container.pricing.current_pricing_snapshot(item, reused) is None
    _recorded(container.pricing.price(item, reused))
    assert count(config, "pricing_snapshots") == 2
    assert container.product_readiness.pricing_readiness(item, first).status is (
        ReadinessStatus.READY
    )


def test_a_changed_fee_table_version_is_stale_until_priced(
    container: Container, sources: Collections
) -> None:
    item = _item(container, sources)
    old, new = context(), context(fee_table_version="fee-test-2")
    _recorded(container.pricing.price(item, old))
    before = container.pricing.evaluate(item, old).dependency_fingerprint
    after = container.pricing.evaluate(item, new).dependency_fingerprint
    assert before != after
    assert container.product_readiness.pricing_readiness(item, new).status is ReadinessStatus.STALE


def test_no_platform_fee_is_stored_without_its_context(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.15: the database refuses a snapshot missing its marketplace or versions.
    item = _item(container, sources)
    snapshot = _recorded(container.pricing.price(item, context())).snapshot
    assert snapshot is not None
    row = _snapshot_row(config, snapshot.pricing_snapshot_id)
    for column in ("marketplace_key", "fee_table_version", "pricing_policy_version"):
        forged = {**row, "pricing_snapshot_id": str(uuid.uuid4()), column: ""}
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(config, "pricing_snapshots", forged)


# ---------------------------------------------------------------- 16–21 immutability and currency


def test_a_snapshot_and_its_pointer_history_are_append_only(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.16–17.
    item = _item(container, sources)
    _recorded(container.pricing.price(item, context()))
    with contextlib.closing(raw(config)) as connection:
        for table in PRICING_TABLES:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(f"UPDATE {table} SET item_id = item_id")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(f"DELETE FROM {table}")


def test_pricing_the_same_dependencies_again_writes_nothing(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.18.
    item = _item(container, sources)
    first = _recorded(container.pricing.price(item, context()))
    before = _pricing_counts(config)
    again = container.pricing.price(item, context())
    assert again.outcome is PricingOutcome.UNCHANGED and again.move is None
    assert again.snapshot == first.snapshot
    assert _pricing_counts(config) == before


def test_a_new_source_revision_makes_the_snapshot_historical(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.19–20: a new revision rebinds the Item; the old snapshot is superseded, never
    # edited, and repricing appends a new snapshot and a REPRICED move from it.
    item = _item(container, sources, product(price=10000))
    first = _recorded(container.pricing.price(item, context()))
    assert first.snapshot is not None
    old_row = _snapshot_row(config, first.snapshot.pricing_snapshot_id)
    same_item = _item(container, sources, product(price=9000))
    assert same_item == item
    stale = container.product_readiness.pricing_readiness(item, context())
    assert _codes(stale) == [("PRICING_SNAPSHOT_SUPERSEDED", None)]
    assert stale.status is ReadinessStatus.STALE
    second = _recorded(container.pricing.price(item, context()))
    assert second.snapshot is not None and second.move is not None
    assert second.move.reason is PricingMoveReason.REPRICED and second.move.sequence == 2
    assert second.move.previous_pricing_snapshot_id == first.snapshot.pricing_snapshot_id
    assert second.snapshot.purchase_cost_krw == 9000
    assert second.snapshot.source_binding_id != first.snapshot.source_binding_id
    assert second.snapshot.dependency_fingerprint != first.snapshot.dependency_fingerprint
    assert _snapshot_row(config, first.snapshot.pricing_snapshot_id) == old_row
    assert container.pricing.current_pricing_snapshot(item, context()) == second.snapshot


def test_a_membership_change_makes_the_snapshot_historical(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    item = _item(container, sources)
    first = _recorded(container.pricing.price(item, context()))
    assert first.snapshot is not None
    sources.collect(source_product_id="5678")
    store = container.product_store
    other = store.source_product(SUPPLIER, "5678").source_product_uid
    change = store.confirm_new_member(
        first.snapshot.product_group_id, other, reason="TEST", decided_by="t", correlation_id="c"
    )
    assert change.revision is not None and change.revision.revision_no == 2
    assert container.product_readiness.pricing_readiness(item, context()).status is (
        ReadinessStatus.STALE
    )
    second = _recorded(container.pricing.price(item, context()))
    assert second.snapshot is not None
    assert second.snapshot.membership_revision_id == change.revision.membership_revision_id


def test_stale_procurement_is_never_priced(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # The pointer moved but the binding still claims the old revision: nothing is priced, and the
    # database refuses a snapshot that claims otherwise.
    item = _item(container, sources)
    priced = _recorded(container.pricing.price(item, context()))
    assert priced.snapshot is not None
    _run, newer = sources.collect(product(price=9000))
    store = container.product_store
    uid = store.source_product(SUPPLIER, PRODUCT).source_product_uid
    store.record_move(
        uid,
        newer.revision_id,
        reason=MoveReason.EXPLICIT_DECISION,
        decided_by="test",
        correlation_id="c",
    )
    result = container.pricing.price(item, context(fee_rate="0.2"))
    assert result.outcome is PricingOutcome.NOT_PRICED
    assert [(r.code, r.status) for r in result.reasons] == [
        ("BINDING_PROVENANCE_STALE", ReadinessStatus.STALE)
    ]
    row = _snapshot_row(config, priced.snapshot.pricing_snapshot_id)
    forged = {**row, "pricing_snapshot_id": str(uuid.uuid4())}
    with pytest.raises(sqlite3.IntegrityError, match="current source revision"):
        _insert(config, "pricing_snapshots", forged)


def test_the_pointer_chain_is_enforced_by_the_database(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    item = _item(container, sources, product(price=10000))
    first = _recorded(container.pricing.price(item, context()))
    _item(container, sources, product(price=9000))
    second = _recorded(container.pricing.price(item, context()))
    other = _recorded(container.pricing.price(item, context(marketplace_key="market_b")))
    assert first.snapshot and second.snapshot and other.snapshot
    fingerprint = context().fingerprint

    def move(sequence: int, snapshot: str, previous: str | None, fp: str = fingerprint) -> None:
        _insert(
            config,
            "current_pricing_snapshot_moves",
            {
                "move_id": str(uuid.uuid4()),
                "item_id": item,
                "pricing_context_fingerprint": fp,
                "sequence": sequence,
                "pricing_snapshot_id": snapshot,
                "previous_pricing_snapshot_id": previous,
                "reason": "REPRICED",
                "rule_version": "t",
                "decided_by": "t",
                "correlation_id": "c",
                "moved_at": "2026-09-19 00:00:00",
            },
        )

    s1, s2 = first.snapshot.pricing_snapshot_id, second.snapshot.pricing_snapshot_id
    with pytest.raises(sqlite3.IntegrityError, match="never returns"):
        move(3, s1, s2)
    with pytest.raises(sqlite3.IntegrityError, match="in order"):
        move(5, s1, s2)
    with pytest.raises(sqlite3.IntegrityError, match="starts from the current"):
        move(3, s1, s1)
    with pytest.raises(sqlite3.IntegrityError, match="this exact context"):
        move(3, other.snapshot.pricing_snapshot_id, s2)


@pytest.mark.parametrize("failing", PRICING_AUDIT, ids=["snapshot audit", "move audit"])
def test_an_audit_failure_rolls_the_snapshot_and_its_move_back(
    container: Container,
    config: AppConfig,
    sources: Collections,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    # Kickoff §11.21.
    item = _item(container, sources)
    append = AuditLog.append

    def refuse(self: AuditLog, entry: Any, **kwargs: Any) -> Any:
        if entry.event_type == failing:
            raise RuntimeError("audit refused")
        return append(self, entry, **kwargs)

    monkeypatch.setattr(AuditLog, "append", refuse)
    with pytest.raises(RuntimeError, match="audit refused"):
        container.pricing.price(item, context())
    assert not any(_pricing_counts(config).values())
    monkeypatch.undo()
    _recorded(container.pricing.price(item, context()))


def test_pricing_is_audited_with_identifiers_and_amounts_only(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    item = _item(container, sources)
    result = _recorded(container.pricing.price(item, context(), correlation_id="cid-price"))
    assert result.snapshot is not None and result.move is not None
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute(
            "SELECT event_type, target_ref, reason_code, correlation_id,"
            " coalesce(before_json, ''), coalesce(after_json, ''), details_json"
            " FROM audit_events WHERE event_type IN (?, ?) ORDER BY seq",
            PRICING_AUDIT,
        ).fetchall()
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        (PRICING_AUDIT[0], result.snapshot.pricing_snapshot_id, "OK", "cid-price"),
        (PRICING_AUDIT[1], item, "INITIAL", "cid-price"),
    ]
    recorded = json.loads(rows[0][6])
    assert recorded["final_sale_price_krw"] == 18184
    assert recorded["pricing_context_fingerprint"] == context().fingerprint
    text = " ".join(" ".join(str(part) for part in row[4:]) for row in rows)
    for forbidden in ("https://", "observed", "shop.example", ".price", "label"):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------- 26–32 base readiness


def test_a_complete_item_is_not_base_ready_without_an_image_selection(
    container: Container, sources: Collections
) -> None:
    # PR-D kickoff §11.31, now PR-E: without an operator image selection, base readiness says so.
    item = _item(container, sources)
    readiness = container.product_readiness.base_readiness(item)
    assert readiness.status is ReadinessStatus.REVIEW_REQUIRED
    assert _codes(readiness) == [(IMAGE_SELECTION_MISSING, "images")]
    assert readiness.pricing_context_fingerprint is None


def test_a_retired_group_blocks(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §11.26.
    item = _item(container, sources)
    with contextlib.closing(raw(config)) as connection:
        connection.execute(
            "UPDATE product_groups SET status = 'RETIRED', retired_at = '2026-09-19 00:00:00'"
        )
        connection.commit()
    assert container.product_readiness.base_readiness(item).status is ReadinessStatus.BLOCKED
    pricing = container.product_readiness.pricing_readiness(item, context())
    assert pricing.status is ReadinessStatus.BLOCKED
    assert ("PRODUCT_GROUP_RETIRED", None) in _codes(pricing)


def test_sold_out_blocks_and_every_reason_is_kept(
    container: Container, sources: Collections
) -> None:
    # Kickoff §11.27, §11.32: one precedence-selected status, all reasons. The brand under review
    # is a COVERAGE field and contributes none (PR #84 review 5253693574).
    fields = product(stock=sold_out(), original_name=review(".name"), brand=review(".brand"))
    item = _item(container, sources, fields)
    readiness = container.product_readiness.base_readiness(item)
    assert readiness.status is ReadinessStatus.BLOCKED
    assert _codes(readiness) == [
        ("SOURCE_STOCK_SOLD_OUT", "stock"),
        (IMAGE_SELECTION_MISSING, "images"),
        ("SOURCE_CORE_FIELD_REVIEW_REQUIRED", "original_name"),
    ]


def test_every_core_field_under_review_is_named(container: Container, sources: Collections) -> None:
    # Kickoff §11.28.
    fields = product(original_name=review(".name"), stock=review(".stock"))
    item = _item(container, sources, fields)
    readiness = container.product_readiness.base_readiness(item)
    assert readiness.status is ReadinessStatus.REVIEW_REQUIRED
    assert _codes(readiness) == [
        (IMAGE_SELECTION_MISSING, "images"),
        ("SOURCE_CORE_FIELD_REVIEW_REQUIRED", "original_name"),
        ("SOURCE_CORE_FIELD_REVIEW_REQUIRED", "stock"),
    ]


# PR #84 review 5253693574: base readiness reads CORE facts only. A COVERAGE field under review is
# never a universal base gate; the pricing inputs among them belong to pricing readiness, and the
# rest to M5's per-target preflight.
COVERAGE_UNDER_REVIEW = ("brand", "manufacturer", "origin", "notice", "detail_description")


@pytest.mark.parametrize("key", COVERAGE_UNDER_REVIEW)
def test_a_coverage_field_under_review_is_not_a_base_gate(
    container: Container, sources: Collections, key: str
) -> None:
    item = _item(container, sources, product(**{key: review(f".{key}")}))
    readiness = container.product_readiness.base_readiness(item)
    assert readiness.status is ReadinessStatus.REVIEW_REQUIRED
    assert _codes(readiness) == [(IMAGE_SELECTION_MISSING, "images")]


@pytest.mark.parametrize(
    ("key", "fact", "code"),
    [
        ("shipping", review(".delivery"), "PRICING_SHIPPING_UNRESOLVED"),
        (
            "minimum_sale_price",
            review(".minimum-price"),
            "PRICING_MINIMUM_SALE_PRICE_UNRESOLVED",
        ),
    ],
    ids=["shipping", "minimum sale price"],
)
def test_pricing_inputs_under_review_belong_to_pricing_readiness_only(
    container: Container, sources: Collections, key: str, fact: FieldFact, code: str
) -> None:
    item = _item(container, sources, product(**{key: fact}))
    base = container.product_readiness.base_readiness(item)
    assert _codes(base) == [(IMAGE_SELECTION_MISSING, "images")]
    pricing = container.product_readiness.pricing_readiness(item, context())
    assert pricing.status is ReadinessStatus.REVIEW_REQUIRED
    assert _codes(pricing) == [(code, key)]


def test_an_item_without_a_valid_binding_needs_review(
    container: Container, sources: Collections
) -> None:
    # Kickoff §11.29: the newer revision no longer proves the base product, so the binding closed.
    item = _item(container, sources)
    run_id, _revision = sources.collect(product(options=review(".options")))
    container.materializer.materialize_run(run_id)
    readiness = container.product_readiness.base_readiness(item)
    assert readiness.status is ReadinessStatus.REVIEW_REQUIRED
    assert ("BINDING_MISSING", None) in _codes(readiness)
    pricing = container.product_readiness.pricing_readiness(item, context())
    assert pricing.status is ReadinessStatus.REVIEW_REQUIRED
    assert _codes(pricing) == [("BINDING_MISSING", None)]


def test_a_binding_on_an_obsolete_revision_is_stale(
    container: Container, sources: Collections
) -> None:
    # Kickoff §11.30.
    item = _item(container, sources)
    _run, newer = sources.collect(product(price=9000))
    store = container.product_store
    uid = store.source_product(SUPPLIER, PRODUCT).source_product_uid
    store.record_move(
        uid,
        newer.revision_id,
        reason=MoveReason.EXPLICIT_DECISION,
        decided_by="t",
        correlation_id="c",
    )
    readiness = container.product_readiness.base_readiness(item)
    assert readiness.status is ReadinessStatus.STALE
    assert ("BINDING_PROVENANCE_STALE", None) in _codes(readiness)


# ---------------------------------------------------------------- 33–39 pricing readiness


def test_an_otherwise_priceable_item_without_a_snapshot_is_stale(
    container: Container, sources: Collections
) -> None:
    # Kickoff §11.33.
    item = _item(container, sources)
    readiness = container.product_readiness.pricing_readiness(item, context())
    assert readiness.status is ReadinessStatus.STALE
    assert _codes(readiness) == [("PRICING_SNAPSHOT_MISSING", None)]


def test_the_pricing_context_is_part_of_the_readiness_fingerprint(
    container: Container, sources: Collections
) -> None:
    # Kickoff §11.37.
    item = _item(container, sources)
    a = container.product_readiness.pricing_readiness(item, context(marketplace_key="market_a"))
    b = container.product_readiness.pricing_readiness(item, context(marketplace_key="market_b"))
    assert a.dependency_fingerprint != b.dependency_fingerprint
    assert (a.pricing_context_fingerprint, b.pricing_context_fingerprint) == (
        context(marketplace_key="market_a").fingerprint,
        context(marketplace_key="market_b").fingerprint,
    )
    base = container.product_readiness.base_readiness(item)
    assert base.dependency_fingerprint not in (a.dependency_fingerprint, b.dependency_fingerprint)


def test_readiness_is_never_universal_across_contexts(
    container: Container, sources: Collections
) -> None:
    # Kickoff §11.38: one Item, three contexts, three results. 12,000 KRW with no fee is 16.7%;
    # with a 10% fee it is 6.7%, below the minimum margin; the third context was never priced.
    item = _item(container, sources, product(minimum_sale_price=minimum(12000)))
    ready, blocked, unpriced = (
        context(marketplace_key="market_a", fee_rate="0"),
        context(marketplace_key="market_b", fee_rate="0.1"),
        context(marketplace_key="market_c"),
    )
    _recorded(container.pricing.price(item, ready))
    _recorded(container.pricing.price(item, blocked))
    statuses = [
        container.product_readiness.pricing_readiness(item, ctx).status
        for ctx in (ready, blocked, unpriced)
    ]
    assert statuses == [ReadinessStatus.READY, ReadinessStatus.BLOCKED, ReadinessStatus.STALE]


def test_no_product_is_a_registration_candidate(container: Container, sources: Collections) -> None:
    # Kickoff §11.39, §H: pricing READY is not an M5 candidate; there is no preflight yet.
    item = _item(container, sources)
    _recorded(container.pricing.price(item, context()))
    assert container.product_readiness.pricing_readiness(item, context()).status is (
        ReadinessStatus.READY
    )
    register = container.screens.register()
    assert register.registration_candidates_total == 0
    assert container.products.product_count() == 1


# ---------------------------------------------------------------- 40–43 migration


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


M4_TABLES = (
    "source_products",
    "current_source_revision_moves",
    "product_groups",
    "group_members",
    "group_membership_revisions",
    "group_change_events",
    "listing_compositions",
    "product_items",
    "source_bindings",
    "audit_events",
)


# Columns later revisions append: 0015 (PR-Q) on ``source_bindings`` and 0025 (Adaptive P3)
# on ``collection_runs``, nullable and never backfilled.
LATER_COLUMNS = frozenset(
    {
        "quantity_offer_id",
        "shadow_decision",
        "shadow_switch_entry_id",
        "shadow_bundle_key",
        "first_product_read_at",
        "settled_by_recovery",
    }
)


def _columns(connection: sqlite3.Connection, table: str) -> str:
    """Every column but those later revisions appended, so rows compare across revisions on what
    each of them holds."""
    names = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    return ", ".join(name for name in names if name not in LATER_COLUMNS)


def _rows(database: Path) -> dict[str, list[tuple[object, ...]]]:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        return {
            table: connection.execute(
                f"SELECT {_columns(connection, table)} FROM {table} ORDER BY 1, 2"
            ).fetchall()
            for table in M4_TABLES
        }


def _materialized_at_0012(tmp_path: Path) -> Path:
    """Two BASE_PRODUCT products at revision 0012. The services write the head schema (migration
    0015 appended a binding column), so they run at head, and the database then steps down through
    the fail-closed downgrades, which keep every row."""
    database = tmp_path / "icbm.db"
    url = _url(database)
    upgrade_to_head(url)
    db = Database(url)
    try:
        clock = FakeClock()
        materializer = ProductMaterializer(
            db=db,
            store=ProductFoundationStore(db, clock),
            revisions=ProductFactsRevisionStore(db, clock),
            audit=AuditLog(db, clock),
        )
        collections = Collections(db, tmp_path / "source-assets")
        for source_product_id in ("1234", "5678"):
            run_id, _revision = collections.collect(source_product_id=source_product_id)
            assert materializer.materialize_run(run_id).item_id is not None
    finally:
        db.dispose()
    command.downgrade(alembic_config(url), "0012_m4_product_foundation")
    return database


def test_0012_to_0013_keeps_every_m4_row_and_guesses_no_price(tmp_path: Path) -> None:
    # Kickoff §11.40–42.
    database = _materialized_at_0012(tmp_path)
    before = _rows(database)
    assert len(before["product_items"]) == 2 and len(before["source_bindings"]) == 2
    upgrade_to_head(_url(database))
    engine = create_sqlite_engine(_url(database))
    try:
        assert current_revision(engine) == head_revision()
    finally:
        engine.dispose()
    assert _rows(database) == before
    with contextlib.closing(sqlite3.connect(database)) as connection:
        for table in PRICING_TABLES:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_downgrade_refuses_to_destroy_pricing_history(tmp_path: Path) -> None:
    # Kickoff §11.43.
    database = _materialized_at_0012(tmp_path)
    url = _url(database)
    upgrade_to_head(url)
    before = _rows(database)
    command.downgrade(alembic_config(url), "0012_m4_product_foundation")
    assert _rows(database) == before
    upgrade_to_head(url)
    db = Database(url)
    try:
        clock = FakeClock()
        store = ProductFoundationStore(db, clock)
        pricing = ProductPricingService(
            store=store,
            revisions=ProductFactsRevisionStore(db, clock),
            audit=AuditLog(db, clock),
            clock=clock,
        )
        with contextlib.closing(sqlite3.connect(database)) as connection:
            item = connection.execute("SELECT item_id FROM product_items LIMIT 1").fetchone()[0]
        _recorded(pricing.price(item, context()))
    finally:
        db.dispose()
    with pytest.raises(RuntimeError, match="pricing history is never silently destroyed"):
        command.downgrade(alembic_config(url), "0012_m4_product_foundation")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0013_m4_pricing_snapshots"
    finally:
        engine.dispose()


def test_the_pricing_checks_match_the_orm(config: AppConfig) -> None:
    from sqlalchemy import CheckConstraint

    from app.db.metadata import metadata

    with contextlib.closing(raw(config)) as connection:
        for table in PRICING_TABLES:
            ddl = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()[0]
            checks = [
                c for c in metadata.tables[table].constraints if isinstance(c, CheckConstraint)
            ]
            assert checks, table
            for check in checks:
                assert f"CHECK ({check.sqltext})" in ddl, f"{table}: {check.name}"
