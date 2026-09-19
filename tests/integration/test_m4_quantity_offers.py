"""M4 PR-Q: product-level QuantityOffers, quantity Items, SOURCE_OFFER bindings and exact offer
pricing (Issue #80 kickoff 5738854211; rulings 5737762202 and 5738760913; ADR-0013 §2, §5–§8).

Every rule is proven through the real services on a migrated database. For each rule the
services would never break, the database refuses the raw write that would break it. No supplier,
marketplace or AI provider is contacted, and no campaign root is opened. Every price, label and
pricing context is invented.
"""

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import CheckConstraint

from app.api.routes.products import product as product_route
from app.audit.models import AuditEventType
from app.audit.service import AuditLog
from app.collect.facts import (
    FieldFact,
    ImageRole,
    OptionAxis,
    OptionConfiguration,
    OptionsValue,
    QuantityTier,
    QuantityTiersValue,
)
from app.collect.revisions import ProductFactsRevisionStore, StoredRevision
from app.config import AppConfig
from app.container import Container
from app.core.errors import InputValidationError, NotFoundError
from app.db.database import Database, create_sqlite_engine
from app.db.metadata import metadata
from app.db.migrate import alembic_config, current_revision, head_revision, upgrade_to_head
from app.products import pricing as pricing_rules
from app.products.image_model import (
    ImageAssetKind,
    QaVerdict,
    SelectedOutput,
    SourceDecision,
    SourceDecisionKind,
)
from app.products.image_store import DerivedImageStore
from app.products.images import IMAGE_SELECTION_MISSING, ProductImageService
from app.products.materialization import (
    BaseProductEvidence,
    Materialization,
    MaterializationStatus,
    ProductMaterializer,
)
from app.products.model import (
    DEFAULT_SINGLE_UNIT_SIGNATURE,
    BindingKind,
    CompositionSpec,
    MoveReason,
    ReadinessStatus,
    Reason,
    composition_signature,
)
from app.products.pricing import (
    MINIMUM_SALE_PRICE_NOT_OFFER_BOUND,
    TARGET_MARGIN_UNREACHABLE,
    PriceBasis,
    PriceGuard,
    PricingMoveReason,
)
from app.products.pricing_service import (
    BINDING_PROVENANCE_STALE,
    PricingOutcome,
    PricingResult,
    ProductPricingService,
)
from app.products.quantity import QuantityOfferEvidence
from app.products.readiness import (
    BASE_READINESS_RULE_VERSION,
    PRICING_READINESS_RULE_VERSION,
    PRICING_SNAPSHOT_MISSING,
    PRICING_SNAPSHOT_SUPERSEDED,
    Readiness,
)
from app.products.store import ProductFoundationStore
from app.register.service import RegisterService
from app.screens.contracts import EmptyReason
from tests.collect_support import confirmed
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
    unknown_shipping,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration

AT = "2026-09-19 00:00:00"
# The canonical example of rulings 5737762202 and 5738760913.
CANONICAL = ((1, 19900), (2, 37900), (3, 53900))
OFFER_FIELDS = json.dumps(["quantity_tiers", "shipping", "minimum_sale_price", "options"])
QUANTITY_TABLES = ("quantity_offers", "listing_compositions", "product_items", "source_bindings")


def tier_fact(*pairs: tuple[int, int]) -> FieldFact:
    """Confirmed tiers in source order. The labels are display text: nothing reads them."""
    return confirmed(
        QuantityTiersValue(
            tiers=tuple(
                QuantityTier(quantity=quantity, total_price_krw=total, label=f"{quantity}개 묶음")
                for quantity, total in pairs
            )
        ),
        ".tiers",
    )


def tiered(*pairs: tuple[int, int], **overrides: Any) -> dict[str, FieldFact]:
    """A product with no options and confirmed tiers, FREE shipping and no minimum sale price
    unless stated otherwise. Its one generic price is 10,000 KRW and never an offer."""
    return product(quantity_tiers=tier_fact(*(pairs or CANONICAL)), **overrides)


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@dataclass(frozen=True)
class Tiered:
    """One materialized tiered product, by quantity."""

    group: str
    member: str
    uid: str
    revision: str
    items: dict[int, str]
    offers: dict[int, str]
    bindings: dict[int, str]


def _materialize(
    container: Container,
    sources: Collections,
    fields: dict[str, FieldFact],
    source_product_id: str = PRODUCT,
) -> tuple[Materialization, StoredRevision]:
    run_id, revision = sources.collect(fields, source_product_id=source_product_id)
    result = container.materializer.materialize_run(run_id)
    assert result.status is MaterializationStatus.MATERIALIZED, result
    return result, revision


def _tiered(
    container: Container,
    sources: Collections,
    fields: dict[str, FieldFact] | None = None,
    source_product_id: str = PRODUCT,
) -> Tiered:
    result, revision = _materialize(
        container, sources, tiered() if fields is None else fields, source_product_id
    )
    readback = container.products.product(str(result.product_group_id))
    items = {item.composition.quantity: item for item in readback.items}
    bound = {q: item.current_binding for q, item in items.items()}
    assert all(binding is not None for binding in bound.values()), bound
    return Tiered(
        group=readback.product_group_id,
        member=readback.members[0].member_id,
        uid=readback.members[0].source_product_uid,
        revision=revision.revision_id,
        items={q: item.item_id for q, item in items.items()},
        offers={q: str(b.quantity_offer_id) for q, b in bound.items() if b is not None},
        bindings={q: b.binding_id for q, b in bound.items() if b is not None},
    )


def _items(container: Container, group: str | None) -> dict[int, Any]:
    return {
        item.composition.quantity: item for item in container.products.product(str(group)).items
    }


def _offers(config: AppConfig) -> list[tuple[Any, ...]]:
    with contextlib.closing(raw(config)) as connection:
        return connection.execute(
            "SELECT tier_ordinal, quantity, total_price_krw, currency, source_revision_id,"
            " source_product_uid FROM quantity_offers ORDER BY rowid"
        ).fetchall()


def _table_counts(config: AppConfig) -> dict[str, int]:
    return {table: count(config, table) for table in metadata.tables}


def _insert(config: AppConfig, table: str, row: dict[str, Any]) -> None:
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with contextlib.closing(raw(config)) as connection:
        connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))
        connection.commit()


def _bind(
    config: AppConfig,
    *,
    item: str,
    member: str,
    offer: str | None,
    revision: str,
    quantity: int,
    kind: str = "SOURCE_OFFER",
    fields: str = OFFER_FIELDS,
) -> None:
    _insert(
        config,
        "source_bindings",
        {
            "binding_id": str(uuid.uuid4()),
            "item_id": item,
            "group_member_id": member,
            "binding_kind": kind,
            "fulfillment_quantity": quantity,
            "provenance_revision_id": revision,
            "provenance_fields": fields,
            "decided_by": "t",
            "correlation_id": "c",
            "valid_from": AT,
            "valid_to": None,
            "quantity_offer_id": offer,
        },
    )


def _offer_row(uid: str, revision: str, ordinal: int, quantity: int, total: int) -> dict[str, Any]:
    return {
        "quantity_offer_id": str(uuid.uuid4()),
        "source_product_uid": uid,
        "source_revision_id": revision,
        "tier_ordinal": ordinal,
        "quantity": quantity,
        "total_price_krw": total,
        "currency": "KRW",
        "created_at": AT,
    }


def _close(container: Container, binding: str) -> None:
    with container.product_store.transaction() as unit:
        unit.close_binding(binding)


def _recorded(result: PricingResult) -> PricingResult:
    assert result.outcome is PricingOutcome.RECORDED, result
    assert result.snapshot is not None and result.move is not None
    return result


def _codes(readiness: Readiness) -> list[tuple[str, str | None]]:
    return [(reason.code, reason.subject) for reason in readiness.reasons]


def _snapshot_row(config: AppConfig, snapshot_id: str) -> dict[str, Any]:
    with contextlib.closing(raw(config)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM pricing_snapshots WHERE pricing_snapshot_id = ?", (snapshot_id,)
        ).fetchone()
    return dict(row)


def _audit(config: AppConfig, event_type: AuditEventType) -> list[dict[str, Any]]:
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute(
            "SELECT details_json, before_json, after_json FROM audit_events"
            " WHERE event_type = ? ORDER BY seq",
            (event_type.value,),
        ).fetchall()
    return [{"details": json.loads(row[0]), "raw": " ".join(str(p) for p in row)} for row in rows]


# ---------------------------------------------------------------- 1–10 offers and Items


def test_confirmed_tiers_become_one_exact_offer_each(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 1, 2, 7: three confirmed tiers, three offers, each the source's own total.
    result, revision = _materialize(container, sources, tiered())
    assert result.quantity_offers is QuantityOfferEvidence.PRODUCT_LEVEL
    assert result.base_product is BaseProductEvidence.NOT_PROVEN
    assert len(result.quantity_offer_ids) == len(result.quantity_offers_created) == 3
    uid = container.product_store.source_product(SUPPLIER, PRODUCT).source_product_uid
    assert _offers(config) == [
        (0, 1, 19900, "KRW", revision.revision_id, uid),
        (1, 2, 37900, "KRW", revision.revision_id, uid),
        (2, 3, 53900, "KRW", revision.revision_id, uid),
    ]
    # Never 19,900 × quantity, and never a total divided into a unit price.
    totals = {row[2] for row in _offers(config)}
    assert totals.isdisjoint({39800, 59700, 18950}) and totals == {19900, 37900, 53900}


def test_no_source_sku_is_fabricated_and_the_generic_prices_are_not_relied_on(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 3; ruling 5738760913 §2: no SourceSKU table, column or row, and no binding
    # that relies on the generic prices field.
    run_id, _revision = sources.collect(tiered())
    before = _table_counts(config)
    container.materializer.materialize_run(run_id)
    after = _table_counts(config)
    assert {table for table in after if after[table] != before[table]} == {
        "source_products",
        "current_source_revision_moves",
        "product_groups",
        "group_members",
        "group_membership_revisions",
        "listing_compositions",
        "product_items",
        "quantity_offers",
        "source_bindings",
        "audit_events",
    }
    assert not [table for table in metadata.tables if "sku" in table]
    with contextlib.closing(raw(config)) as connection:
        bindings = connection.execute(
            "SELECT binding_kind, quantity_offer_id, provenance_fields FROM source_bindings"
        ).fetchall()
    assert len(bindings) == 3
    for kind, offer, fields in bindings:
        assert kind == "SOURCE_OFFER" and offer is not None
        assert json.loads(fields) == json.loads(OFFER_FIELDS) and "prices" not in fields


def test_each_offer_quantity_is_its_own_item_and_q1_is_the_default_single_unit(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 4–6 and §D: quantities stay structural; display text is not identity.
    result, _revision = _materialize(container, sources, tiered())
    items = _items(container, result.product_group_id)
    assert sorted(items) == [1, 2, 3]
    assert items[1].composition.composition_signature == DEFAULT_SINGLE_UNIT_SIGNATURE
    for quantity, item in items.items():
        c = item.composition
        assert (c.unit_amount, c.unit_code, c.pack_count, c.units_per_pack, c.total_amount) == (
            None,
            None,
            None,
            None,
            None,
        )
        assert c.composition_signature == composition_signature(CompositionSpec(quantity=quantity))
    assert len({item.item_id for item in items.values()}) == 3
    assert result.quantity_item_ids == tuple(items[q].item_id for q in (1, 2, 3))
    assert result.quantity_items_created == result.quantity_item_ids


def test_q1_reuses_the_existing_default_item(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 5: a product first sold as a base product keeps its default Item for q1.
    base, _first = _materialize(container, sources, product())
    assert base.item_id is not None and base.binding_opened is not None
    moved, revision = _materialize(container, sources, tiered())
    items = _items(container, moved.product_group_id)
    assert items[1].item_id == base.item_id and base.item_id not in moved.quantity_items_created
    assert moved.bindings_closed == (base.binding_opened,)
    binding = items[1].current_binding
    assert binding.binding_kind is BindingKind.SOURCE_OFFER
    assert binding.provenance_revision_id == revision.revision_id
    assert count(config, "product_items") == 3


GENERIC_PRICES = {
    "one generic price": (10000,),
    "several, lowest first": (100, 19900, 99999),
    "several, highest first": (99999, 37900, 1),
    "the tier totals in reverse": (53900, 37900, 19900),
}


@pytest.mark.parametrize("price", GENERIC_PRICES.values(), ids=GENERIC_PRICES.keys())
def test_generic_prices_never_decide_an_offer_or_its_cost(
    container: Container, config: AppConfig, sources: Collections, price: tuple[int, ...]
) -> None:
    # Kickoff §J 8–9: no min, first, last, order or label of the generic prices decides anything.
    t = _tiered(container, sources, tiered(price=price))
    assert [row[1:3] for row in _offers(config)] == [(1, 19900), (2, 37900), (3, 53900)]
    for quantity, total in CANONICAL:
        snapshot = _recorded(container.pricing.price(t.items[quantity], context())).snapshot
        assert snapshot is not None and snapshot.purchase_cost_krw == total


def test_replaying_the_same_revision_writes_nothing_new(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 10: no duplicate offer, Item, binding or audit event.
    run_id, _revision = sources.collect(tiered())
    first = container.materializer.materialize_run(run_id)
    before = _table_counts(config)
    for again in (
        container.materializer.materialize_run(run_id),
        container.materializer.materialize_source(SUPPLIER, PRODUCT),
    ):
        assert again.status is MaterializationStatus.UNCHANGED
        assert again.quantity_offer_ids == first.quantity_offer_ids
        assert again.quantity_item_ids == first.quantity_item_ids
        assert (
            again.quantity_offers_created,
            again.quantity_items_created,
            again.offer_bindings_opened,
        ) == ((), (), ())
    assert _table_counts(config) == before


def test_explicit_reconciliation_completes_an_already_current_revision(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 11, §F: a tiered revision made current before PR-Q shipped has a group and a
    # member but no offer. An explicit call creates what it lacks, and moves nothing.
    _run_id, revision = sources.collect(tiered())
    store = container.product_store
    uid = store.source_product(SUPPLIER, PRODUCT).source_product_uid
    store.record_move(
        uid,
        revision.revision_id,
        reason=MoveReason.INITIAL,
        decided_by="products.materializer",
        correlation_id="cid-pre-q",
        rule_version="current-source-rule/v1",
    )
    group = store.create_group(decided_by="products.materializer")
    store.confirm_new_member(
        group, uid, reason="MATERIALIZED", decided_by="products.materializer", correlation_id="c"
    )
    assert all(count(config, table) == 0 for table in QUANTITY_TABLES)
    result = container.materializer.materialize_source(SUPPLIER, PRODUCT)
    assert result.status is MaterializationStatus.MATERIALIZED
    assert result.move is None and result.product_group_id == group
    assert len(result.quantity_offers_created) == len(result.offer_bindings_opened) == 3
    assert count(config, "current_source_revision_moves") == 1
    assert count(config, "source_bindings", "valid_to IS NULL") == 3


# ---------------------------------------------------------------- 12–17 revision changes


def test_new_totals_for_the_same_quantities_keep_the_items_with_new_offers_and_bindings(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 12: same Items, new offers, new bindings; history stays immutable.
    first, old = _materialize(container, sources, tiered())
    history = _offers(config)
    second, new = _materialize(container, sources, tiered((1, 18900), (2, 36900), (3, 52900)))
    assert second.quantity_item_ids == first.quantity_item_ids
    assert second.quantity_items_created == ()
    assert set(second.quantity_offer_ids).isdisjoint(first.quantity_offer_ids)
    assert sorted(second.bindings_closed) == sorted(first.offer_bindings_opened)
    assert len(second.offer_bindings_opened) == 3
    assert _offers(config)[:3] == history
    assert [row[1:3] for row in _offers(config)[3:]] == [(1, 18900), (2, 36900), (3, 52900)]
    for item in _items(container, second.product_group_id).values():
        binding = item.current_binding
        assert binding.quantity_offer_id in second.quantity_offer_ids
        assert binding.provenance_revision_id == new.revision_id != old.revision_id


def test_an_added_quantity_creates_only_its_new_item(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 13.
    _materialize(container, sources, tiered())
    added, _revision = _materialize(container, sources, tiered(*CANONICAL, (4, 69900)))
    items = _items(container, added.product_group_id)
    assert sorted(items) == [1, 2, 3, 4]
    assert added.quantity_items_created == (items[4].item_id,)
    assert items[4].current_binding.fulfillment_quantity == 4


def test_a_removed_quantity_closes_its_binding_and_keeps_its_item(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 14: the q3 Item stays as history, unbound; nothing guesses a replacement.
    first, _old = _materialize(container, sources, tiered())
    removed, _new = _materialize(container, sources, tiered((1, 19900), (2, 37900)))
    items = _items(container, removed.product_group_id)
    assert sorted(items) == [1, 2, 3] and items[3].current_binding is None
    assert sorted(removed.bindings_closed) == sorted(first.offer_bindings_opened)
    assert len(removed.offer_bindings_opened) == 2
    assert count(config, "source_bindings", "valid_to IS NULL") == 2


def test_tiers_under_review_create_no_offer_and_no_binding(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 15: no guessed offer.
    result, _revision = _materialize(container, sources, product(quantity_tiers=review(".tiers")))
    assert result.quantity_offers is QuantityOfferEvidence.NOT_PROVEN
    assert all(count(config, table) == 0 for table in QUANTITY_TABLES)


def test_tiers_moving_under_review_close_every_offer_binding_and_open_none(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §F: close obsolete SOURCE_OFFER bindings after the move; no replacement.
    first, _old = _materialize(container, sources, tiered())
    moved, _new = _materialize(container, sources, product(quantity_tiers=review(".tiers")))
    assert sorted(moved.bindings_closed) == sorted(first.offer_bindings_opened)
    assert moved.offer_bindings_opened == () and moved.binding_opened is None
    assert count(config, "source_bindings", "valid_to IS NULL") == 0
    assert (count(config, "quantity_offers"), count(config, "product_items")) == (3, 3)


OPTIONS_NOT_ABSENT = {
    "options stated with no axis": lambda: confirmed(OptionsValue(axes=()), ".options"),
    "options with configurations": lambda: confirmed(
        OptionsValue(
            axes=(OptionAxis(name="용량", values=("1개", "2개")),),
            configurations=(
                OptionConfiguration(selections=("1개",)),
                OptionConfiguration(selections=("2개",)),
            ),
        ),
        ".options",
    ),
    "options under review": lambda: review(".options"),
}


@pytest.mark.parametrize("options", OPTIONS_NOT_ABSENT.values(), ids=OPTIONS_NOT_ABSENT.keys())
def test_tiers_are_never_read_as_offers_unless_options_are_absent(
    container: Container,
    config: AppConfig,
    sources: Collections,
    options: Callable[[], FieldFact],
) -> None:
    # Kickoff §J 16: options CONFIRMED or REVIEW_REQUIRED never map to product-level tiers, and
    # the database refuses such an offer too.
    result, revision = _materialize(container, sources, tiered(options=options()))
    assert result.quantity_offers is QuantityOfferEvidence.NOT_PROVEN
    assert all(count(config, table) == 0 for table in QUANTITY_TABLES)
    uid = container.product_store.source_product(SUPPLIER, PRODUCT).source_product_uid
    with pytest.raises(sqlite3.IntegrityError, match="revision stating no options"):
        _insert(config, "quantity_offers", _offer_row(uid, revision.revision_id, 1, 2, 37900))


def test_a_later_absent_revision_returns_q1_to_base_product(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 17: q1 may return to BASE_PRODUCT; q2/q3 stay historical and unbound.
    first, _old = _materialize(container, sources, tiered())
    later, revision = _materialize(container, sources, product())
    assert later.base_product is BaseProductEvidence.PROVEN
    assert later.quantity_offers is QuantityOfferEvidence.NONE_STATED
    assert sorted(later.bindings_closed) == sorted(first.offer_bindings_opened)
    items = _items(container, later.product_group_id)
    binding = items[1].current_binding
    assert binding.binding_kind is BindingKind.BASE_PRODUCT and binding.quantity_offer_id is None
    assert (binding.fulfillment_quantity, binding.provenance_revision_id) == (
        1,
        revision.revision_id,
    )
    assert items[2].current_binding is None and items[3].current_binding is None
    assert count(config, "quantity_offers") == 3


# ---------------------------------------------------------------- 18–25 invariants


def test_a_source_offer_without_its_exact_offer_is_refused(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 18.
    t = _tiered(container, sources)
    _close(container, t.bindings[2])
    with pytest.raises(sqlite3.IntegrityError, match="SOURCE_OFFER names its exact offer"):
        _bind(config, item=t.items[2], member=t.member, offer=None, revision=t.revision, quantity=2)
    with pytest.raises(sqlite3.IntegrityError):
        _bind(
            config,
            item=t.items[2],
            member=t.member,
            offer=str(uuid.uuid4()),
            revision=t.revision,
            quantity=2,
        )
    with pytest.raises(NotFoundError), container.product_store.transaction() as unit:
        unit.bind_source_offer(
            t.items[2], t.member, str(uuid.uuid4()), decided_by="t", correlation_id="c"
        )


def test_a_source_offer_binds_only_an_offer_of_its_members_own_source_product(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 19: wrong source, wrong member.
    a = _tiered(container, sources, source_product_id="1234")
    b = _tiered(container, sources, source_product_id="5678")
    _close(container, a.bindings[2])
    with pytest.raises(
        sqlite3.IntegrityError, match="an offer of the source product of its member"
    ):
        _bind(
            config,
            item=a.items[2],
            member=a.member,
            offer=b.offers[2],
            revision=a.revision,
            quantity=2,
        )
    with pytest.raises(sqlite3.IntegrityError, match="CONFIRMED in the group of the Item"):
        _bind(
            config,
            item=a.items[2],
            member=b.member,
            offer=b.offers[2],
            revision=b.revision,
            quantity=2,
        )
    with (
        pytest.raises(InputValidationError, match="own source product"),
        container.product_store.transaction() as unit,
    ):
        unit.bind_source_offer(
            a.items[2], a.member, b.offers[2], decided_by="t", correlation_id="c"
        )


def test_a_source_offer_is_bound_from_its_offers_revision_which_is_current(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 20: offer revision = provenance = the member's current source revision.
    old = _tiered(container, sources)
    new = _tiered(container, sources, tiered((1, 18900), (2, 36900), (3, 52900)))
    _close(container, new.bindings[2])
    with pytest.raises(sqlite3.IntegrityError, match="provenance is the revision of its offer"):
        _bind(
            config,
            item=new.items[2],
            member=new.member,
            offer=old.offers[2],
            revision=new.revision,
            quantity=2,
        )
    with pytest.raises(sqlite3.IntegrityError, match="current source revision of its member"):
        _bind(
            config,
            item=new.items[2],
            member=new.member,
            offer=old.offers[2],
            revision=old.revision,
            quantity=2,
        )
    with (
        pytest.raises(InputValidationError, match="current source revision"),
        container.product_store.transaction() as unit,
    ):
        unit.bind_source_offer(
            new.items[2], new.member, old.offers[2], decided_by="t", correlation_id="c"
        )
    # The exact binding itself is admitted: the rules refuse only what breaks them.
    _bind(
        config,
        item=new.items[2],
        member=new.member,
        offer=new.offers[2],
        revision=new.revision,
        quantity=2,
    )


@pytest.mark.parametrize("quantity", [1, 3])
def test_the_fulfillment_quantity_is_the_offer_quantity(
    container: Container, config: AppConfig, sources: Collections, quantity: int
) -> None:
    # Kickoff §J 21.
    t = _tiered(container, sources)
    _close(container, t.bindings[2])
    with pytest.raises(sqlite3.IntegrityError, match="exactly the quantity of its offer"):
        _bind(
            config,
            item=t.items[2],
            member=t.member,
            offer=t.offers[2],
            revision=t.revision,
            quantity=quantity,
        )


def test_the_item_is_exactly_the_offer_quantity_and_nothing_more(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 22: another quantity, or the same quantity as a pack, is not the offer's Item.
    t = _tiered(container, sources)
    _close(container, t.bindings[3])
    with pytest.raises(sqlite3.IntegrityError, match="Item of exactly that quantity"):
        _bind(
            config,
            item=t.items[3],
            member=t.member,
            offer=t.offers[2],
            revision=t.revision,
            quantity=2,
        )
    store = container.product_store
    packed = store.item(
        t.group, store.composition(CompositionSpec(quantity=2, pack_count=2)).composition_id
    )
    with pytest.raises(sqlite3.IntegrityError, match="Item of exactly that quantity"):
        _bind(
            config,
            item=packed.item_id,
            member=t.member,
            offer=t.offers[2],
            revision=t.revision,
            quantity=2,
        )
    with (
        pytest.raises(InputValidationError, match="exactly its quantity"),
        store.transaction() as unit,
    ):
        unit.bind_source_offer(
            packed.item_id, t.member, t.offers[2], decided_by="t", correlation_id="c"
        )


def test_one_open_binding_per_item_and_per_offer_and_bindings_only_close(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 23; a SOURCE_OFFER never relies on the generic prices.
    t = _tiered(container, sources)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _bind(
            config,
            item=t.items[2],
            member=t.member,
            offer=t.offers[2],
            revision=t.revision,
            quantity=2,
        )
    forged_composition, forged_item = str(uuid.uuid4()), str(uuid.uuid4())
    with contextlib.closing(raw(config)) as connection:
        connection.execute(
            "INSERT INTO listing_compositions VALUES (?, 2, NULL, NULL, NULL, NULL, NULL, ?,"
            " 'composition-signature/v1', ?)",
            (forged_composition, "f" * 64, AT),
        )
        connection.execute(
            "INSERT INTO product_items VALUES (?, ?, ?, ?, ?)",
            (forged_item, t.group, forged_composition, "f" * 64, AT),
        )
        connection.commit()
    with pytest.raises(
        sqlite3.IntegrityError,
        match=r"UNIQUE constraint failed: source_bindings\.quantity_offer_id",
    ):
        _bind(
            config,
            item=forged_item,
            member=t.member,
            offer=t.offers[2],
            revision=t.revision,
            quantity=2,
        )
    _close(container, t.bindings[2])
    with pytest.raises(sqlite3.IntegrityError, match="never relies on the generic prices"):
        _bind(
            config,
            item=t.items[2],
            member=t.member,
            offer=t.offers[2],
            revision=t.revision,
            quantity=2,
            fields=json.dumps(["prices", "quantity_tiers"]),
        )
    with contextlib.closing(raw(config)) as connection:
        for statement in (
            "UPDATE source_bindings SET quantity_offer_id = NULL WHERE valid_to IS NULL",
            f"UPDATE source_bindings SET quantity_offer_id = '{t.offers[3]}'"
            f" WHERE binding_id = '{t.bindings[1]}'",
            "UPDATE source_bindings SET fulfillment_quantity = 5",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="only ever closed"):
                connection.execute(statement)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM source_bindings")


def test_offers_are_immutable_and_exactly_their_revisions_tiers(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 24.
    t = _tiered(container, sources)
    _other_run, other = sources.collect(tiered(), source_product_id="5678")
    with contextlib.closing(raw(config)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE quantity_offers SET total_price_krw = 39800")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM quantity_offers")
    forged = {
        "one unit's price times two": (1, 2, 39800),
        "a unit price": (1, 2, 18950),
        "a tier never stated": (3, 4, 69900),
        "a quantity at another position": (0, 2, 37900),
    }
    for ordinal, quantity, total in forged.values():
        with pytest.raises(sqlite3.IntegrityError, match="exactly a CONFIRMED tier"):
            _insert(
                config, "quantity_offers", _offer_row(t.uid, t.revision, ordinal, quantity, total)
            )
    with pytest.raises(sqlite3.IntegrityError, match="source product of the offer"):
        _insert(config, "quantity_offers", _offer_row(t.uid, other.revision_id, 0, 1, 19900))
    with pytest.raises(sqlite3.IntegrityError, match="currency"):
        _insert(
            config,
            "quantity_offers",
            {**_offer_row(t.uid, t.revision, 0, 1, 19900), "currency": "USD"},
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert(config, "quantity_offers", _offer_row(t.uid, t.revision, 0, 1, 19900))
    assert [row[1:3] for row in _offers(config)] == [(1, 19900), (2, 37900), (3, 53900)]


def test_an_audit_failure_rolls_the_whole_quantity_decision_back(
    container: Container,
    config: AppConfig,
    sources: Collections,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Kickoff §J 25: offers, Items and bindings commit with the decision's audit, or not at all.
    run_id, _revision = sources.collect(tiered())
    append = AuditLog.append

    def refuse(self: AuditLog, entry: object, **kwargs: object) -> object:
        if getattr(entry, "event_type", None) is AuditEventType.PRODUCT_MATERIALIZED:
            raise RuntimeError("audit refused")
        return append(self, entry, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(AuditLog, "append", refuse)
    with pytest.raises(RuntimeError, match="audit refused"):
        container.materializer.materialize_run(run_id)
    for table in (*QUANTITY_TABLES, "current_source_revision_moves", "product_groups"):
        assert count(config, table) == 0, table
    monkeypatch.undo()
    assert len(container.materializer.materialize_run(run_id).quantity_offers_created) == 3


# ---------------------------------------------------------------- 26–38 pricing


def test_each_quantity_item_is_priced_from_its_own_offer_total(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 26–29, 33: q1 19,900, q2 37,900, q3 53,900. FREE shipping, no minimum, so each is
    # TARGET_MARGIN at 35% under a 10% fee. Two × 19,900 would price q2 at 72,365; it does not.
    t = _tiered(container, sources, tiered(price=12345))
    ctx = context()
    expected = {1: (19900, 36184), 2: (37900, 68910), 3: (53900, 98000)}
    for quantity, (cost, price) in expected.items():
        snapshot = _recorded(container.pricing.price(t.items[quantity], ctx)).snapshot
        assert snapshot is not None
        assert (snapshot.purchase_cost_krw, snapshot.supplier_shipping_krw) == (cost, 0)
        assert snapshot.minimum_sale_price_krw is None
        assert snapshot.final_sale_price_krw == snapshot.target_margin_price_krw == price
        assert snapshot.price_basis is PriceBasis.TARGET_MARGIN
        assert snapshot.price_guard is PriceGuard.OK
        assert snapshot.source_binding_id == t.bindings[quantity]
        assert snapshot.source_product_facts_revision_id == t.revision
        readiness = container.product_readiness.pricing_readiness(t.items[quantity], ctx)
        assert readiness.status is ReadinessStatus.READY and readiness.reasons == ()
        assert readiness.rule_version == PRICING_READINESS_RULE_VERSION == "pricing-readiness/v2"


def test_fixed_supplier_shipping_is_added_exactly(
    container: Container, sources: Collections
) -> None:
    # Kickoff §J 30–31 (FREE is priced as 0 above).
    t = _tiered(container, sources, tiered(shipping=fixed(3000)))
    snapshot = _recorded(container.pricing.price(t.items[2], context())).snapshot
    assert snapshot is not None
    assert (snapshot.purchase_cost_krw, snapshot.supplier_shipping_krw) == (37900, 3000)
    assert snapshot.final_sale_price_krw == 74365


UNSETTLED_SHIPPING = {
    "conditional": conditional,
    "unknown": unknown_shipping,
    "under review": lambda: review(".delivery"),
}


@pytest.mark.parametrize("shipping", UNSETTLED_SHIPPING.values(), ids=UNSETTLED_SHIPPING.keys())
def test_unsettled_shipping_leaves_every_offer_unpriced(
    container: Container,
    config: AppConfig,
    sources: Collections,
    shipping: Callable[[], FieldFact],
) -> None:
    # Kickoff §J 32: no 3,000 KRW guess.
    t = _tiered(container, sources, tiered(shipping=shipping()))
    for item in t.items.values():
        result = container.pricing.price(item, context())
        assert result.outcome is PricingOutcome.NOT_PRICED
        assert {(r.subject, r.status) for r in result.reasons} == {
            ("shipping", ReadinessStatus.REVIEW_REQUIRED)
        }
        readiness = container.product_readiness.pricing_readiness(item, context())
        assert readiness.status is ReadinessStatus.REVIEW_REQUIRED
    assert count(config, "pricing_snapshots") == 0


GENERIC_MINIMUMS = {
    "a stated generic minimum": lambda: minimum(15000),
    "a generic minimum under review": lambda: review(".minimum-price"),
}


@pytest.mark.parametrize("stated", GENERIC_MINIMUMS.values(), ids=GENERIC_MINIMUMS.keys())
def test_a_generic_minimum_is_never_applied_to_an_offer(
    container: Container,
    config: AppConfig,
    sources: Collections,
    stated: Callable[[], FieldFact],
) -> None:
    # Kickoff §J 34; ruling 5738760913 §7: never to every tier, never × quantity.
    t = _tiered(container, sources, tiered(minimum_sale_price=stated()))
    reason = Reason(
        MINIMUM_SALE_PRICE_NOT_OFFER_BOUND, ReadinessStatus.REVIEW_REQUIRED, "minimum_sale_price"
    )
    for item in t.items.values():
        result = container.pricing.price(item, context())
        assert result.outcome is PricingOutcome.NOT_PRICED and result.reasons == (reason,)
        readiness = container.product_readiness.pricing_readiness(item, context())
        assert readiness.status is ReadinessStatus.REVIEW_REQUIRED
        assert _codes(readiness) == [(MINIMUM_SALE_PRICE_NOT_OFFER_BOUND, "minimum_sale_price")]
    assert count(config, "pricing_snapshots") == 0


def test_a_source_offer_snapshot_is_its_offers_total_with_no_minimum(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # The database refuses a SOURCE_OFFER snapshot that prices anything but its offer's total, or
    # that applies a minimum; each forged row is otherwise self-consistent.
    t = _tiered(container, sources)
    snapshot = _recorded(container.pricing.price(t.items[2], context())).snapshot
    assert snapshot is not None
    row = _snapshot_row(config, snapshot.pricing_snapshot_id)
    doubled = {**row, "pricing_snapshot_id": str(uuid.uuid4()), "purchase_cost_krw": 39800}
    doubled["expected_net_profit_krw"] = (
        row["final_sale_price_krw"] - 39800 - row["platform_fee_krw"] - row["other_policy_cost_krw"]
    )
    doubled["expected_net_margin_bp"] = (
        doubled["expected_net_profit_krw"] * 10000 // row["final_sale_price_krw"]
    )
    with pytest.raises(sqlite3.IntegrityError, match="prices the total of its exact offer"):
        _insert(config, "pricing_snapshots", doubled)
    with_minimum = {
        **row,
        "pricing_snapshot_id": str(uuid.uuid4()),
        "minimum_sale_price_krw": row["final_sale_price_krw"],
        "price_basis": "MINIMUM_SALE_PRICE",
    }
    with pytest.raises(sqlite3.IntegrityError, match="applies no minimum"):
        _insert(config, "pricing_snapshots", with_minimum)


def test_the_base_product_minimum_rule_is_unchanged(
    container: Container, sources: Collections
) -> None:
    # Kickoff §J 35.
    run_id, _revision = sources.collect(product(minimum_sale_price=minimum(15000)))
    item = container.materializer.materialize_run(run_id).item_id
    assert item is not None
    snapshot = _recorded(container.pricing.price(item, context())).snapshot
    assert snapshot is not None
    assert (snapshot.purchase_cost_krw, snapshot.minimum_sale_price_krw) == (10000, 15000)
    assert snapshot.final_sale_price_krw == 15000
    assert snapshot.price_basis is PriceBasis.MINIMUM_SALE_PRICE


def test_a_new_offer_binding_supersedes_the_old_price(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 36: a new offer and binding stale the price; the old snapshot stays as it was.
    t = _tiered(container, sources)
    ctx = context()
    first = _recorded(container.pricing.price(t.items[2], ctx)).snapshot
    assert first is not None
    kept = _snapshot_row(config, first.pricing_snapshot_id)
    _tiered(container, sources, tiered((1, 18900), (2, 36900), (3, 52900)))
    readiness = container.product_readiness.pricing_readiness(t.items[2], ctx)
    assert readiness.status is ReadinessStatus.STALE
    assert _codes(readiness) == [(PRICING_SNAPSHOT_SUPERSEDED, None)]
    repriced = _recorded(container.pricing.price(t.items[2], ctx))
    assert repriced.snapshot is not None and repriced.move is not None
    assert repriced.snapshot.purchase_cost_krw == 36900
    assert repriced.snapshot.final_sale_price_krw == 67093
    assert repriced.move.reason is PricingMoveReason.REPRICED
    assert repriced.move.previous_pricing_snapshot_id == first.pricing_snapshot_id
    assert _snapshot_row(config, first.pricing_snapshot_id) == kept
    ready = container.product_readiness.pricing_readiness(t.items[2], ctx)
    assert ready.status is ReadinessStatus.READY


def test_an_offer_binding_left_behind_by_its_revision_is_stale(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §H: an old offer, revision or binding against the current revision is STALE.
    t = _tiered(container, sources)
    ctx = context()
    _recorded(container.pricing.price(t.items[2], ctx))
    _run_id, newer = sources.collect(tiered((1, 18900), (2, 36900), (3, 52900)))
    container.product_store.record_move(
        t.uid,
        newer.revision_id,
        reason=MoveReason.NEWER_REVISION,
        decided_by="test",
        correlation_id="c",
    )
    pricing = container.product_readiness.pricing_readiness(t.items[2], ctx)
    base = container.product_readiness.base_readiness(t.items[2])
    for readiness in (pricing, base):
        assert readiness.status is ReadinessStatus.STALE
        assert (BINDING_PROVENANCE_STALE, None) in _codes(readiness)
    assert container.pricing.price(t.items[2], ctx).outcome is PricingOutcome.NOT_PRICED


def test_pricing_contexts_stay_independent_for_an_offer(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 37.
    t = _tiered(container, sources)
    a = context()
    b = context(marketplace_key="market_b", fee_rate="0.2")
    first_a = _recorded(container.pricing.price(t.items[2], a)).snapshot
    first_b = _recorded(container.pricing.price(t.items[2], b)).snapshot
    assert first_a is not None and first_b is not None
    assert (first_a.final_sale_price_krw, first_b.final_sale_price_krw) == (68910, 84224)
    assert first_a.pricing_context_fingerprint != first_b.pricing_context_fingerprint
    assert container.pricing.price(t.items[2], a).outcome is PricingOutcome.UNCHANGED
    assert container.pricing.current_pricing_snapshot(t.items[2], b) == first_b


def test_offer_pricing_keeps_every_guard(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 38: an unreachable target is REVIEW_REQUIRED, and the stored guard still follows
    # exactly from the stored amounts: a loss claimed as OK is refused.
    t = _tiered(container, sources)
    unreachable = container.pricing.price(t.items[2], context(fee_rate="0.7"))
    assert unreachable.outcome is PricingOutcome.NOT_PRICED
    assert [r.code for r in unreachable.reasons] == [TARGET_MARGIN_UNREACHABLE]
    snapshot = _recorded(container.pricing.price(t.items[2], context())).snapshot
    assert snapshot is not None
    row = _snapshot_row(config, snapshot.pricing_snapshot_id)
    loss = {
        **row,
        "pricing_snapshot_id": str(uuid.uuid4()),
        "target_margin_price_krw": 40000,
        "final_sale_price_krw": 40000,
        "platform_fee_krw": 4000,
        "expected_net_profit_krw": 40000 - 37900 - 4000,
        "expected_net_margin_bp": (40000 - 37900 - 4000) * 10000 // 40000,
    }
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(config, "pricing_snapshots", loss)


def test_a_snapshot_from_the_previous_dependency_version_is_superseded_not_rewritten(
    container: Container, config: AppConfig, sources: Collections, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kickoff §G: the dependency structure changed, so pricing-dependency/v2. A snapshot recorded
    # under v1 stays as it is and reads as superseded; repricing records a new one, same amounts.
    run_id, _revision = sources.collect(product())
    item = container.materializer.materialize_run(run_id).item_id
    assert item is not None
    ctx = context()
    monkeypatch.setattr(pricing_rules, "DEPENDENCY_VERSION", "pricing-dependency/v1")
    old = _recorded(container.pricing.price(item, ctx)).snapshot
    monkeypatch.undo()
    assert old is not None and pricing_rules.DEPENDENCY_VERSION == "pricing-dependency/v2"
    kept = _snapshot_row(config, old.pricing_snapshot_id)
    readiness = container.product_readiness.pricing_readiness(item, ctx)
    assert _codes(readiness) == [(PRICING_SNAPSHOT_SUPERSEDED, None)]
    new = _recorded(container.pricing.price(item, ctx)).snapshot
    assert new is not None and new.dependency_fingerprint != old.dependency_fingerprint
    for amount in ("purchase_cost_krw", "final_sale_price_krw", "platform_fee_krw"):
        assert getattr(new, amount) == getattr(old, amount), amount
    assert _snapshot_row(config, old.pricing_snapshot_id) == kept


# ---------------------------------------------------------------- 39–43 readiness and regression


def test_a_valid_source_offer_is_valid_procurement(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 39–40: no BASE_PRODUCT-only restriction applies; each Item still needs its own
    # operator image selection.
    t = _tiered(container, sources)
    for item in t.items.values():
        base = container.product_readiness.base_readiness(item)
        assert base.rule_version == BASE_READINESS_RULE_VERSION == "base-readiness/v3"
        assert _codes(base) == [(IMAGE_SELECTION_MISSING, "images")]
        pricing = container.product_readiness.pricing_readiness(item, context())
        assert _codes(pricing) == [(PRICING_SNAPSHOT_MISSING, None)]


def test_new_quantity_items_never_inherit_an_image_selection(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 40: an operator selection for q1 is q1's alone; q2 and q3 have none, even
    # after a replay.
    t = _tiered(container, sources)
    stored = container.revisions.get(t.revision)
    assert stored is not None
    image = stored.images[0]
    container.images.record_operator_selection(
        t.items[1],
        source_revision_id=t.revision,
        decisions=[
            SourceDecision(
                role=image.role,
                ordinal=image.ordinal,
                sha256=str(image.sha256),
                decision=SourceDecisionKind.USE_SOURCE,
            )
        ],
        outputs=[
            SelectedOutput(
                role=ImageRole.REPRESENTATIVE, source_role=image.role, source_ordinal=image.ordinal
            )
        ],
        decided_by="operator",
    )
    container.materializer.materialize_source(SUPPLIER, PRODUCT)
    assert count(config, "image_selection_revisions") == 1
    assert count(config, "current_image_selection_moves", "item_id = ?", t.items[1]) == 1
    for quantity in (2, 3):
        base = container.product_readiness.base_readiness(t.items[quantity])
        assert (IMAGE_SELECTION_MISSING, "images") in _codes(base)
    q1 = container.product_readiness.base_readiness(t.items[1])
    assert (IMAGE_SELECTION_MISSING, "images") not in _codes(q1)


def test_quantity_items_are_not_registration_candidates(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §J 43; ADR-0013 §8: no REGISTERABLE truth.
    t = _tiered(container, sources)
    for item in t.items.values():
        _recorded(container.pricing.price(item, context()))
    assert RegisterService().registration_candidate_count() == 0
    register = container.screens.register()
    assert register.registration_candidates_total == 0
    assert register.meta.empty_reason is EmptyReason.NO_REGISTRATION_CANDIDATES


# ---------------------------------------------------------------- read-back and audit


def test_read_back_names_each_items_exact_offer_binding(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §I: quantity, binding kind and id, fulfillment quantity, offer, provenance; no
    # source price is read back or recomputed.
    t = _tiered(container, sources)
    view = product_route(t.group, container)
    assert len(view.items) == 3
    for item in view.items:
        quantity = item.composition.quantity
        binding = item.current_binding
        assert binding is not None
        assert (binding.binding_kind, binding.binding_id) == (
            BindingKind.SOURCE_OFFER,
            t.bindings[quantity],
        )
        assert binding.fulfillment_quantity == quantity
        assert binding.quantity_offer_id == t.offers[quantity]
        assert binding.provenance_revision_id == t.revision
        assert set(binding.model_dump()) == {
            "binding_id",
            "binding_kind",
            "group_member_id",
            "provenance_revision_id",
            "valid_from",
            "fulfillment_quantity",
            "quantity_offer_id",
        }
    assert "price" not in view.model_dump_json()


def test_quantity_decisions_are_audited_with_identifiers_quantities_and_totals_only(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    # Kickoff §I: IDs, quantities, totals, revisions, enums and versions; never page text, labels
    # or URLs.
    t = _tiered(container, sources)
    (event,) = _audit(config, AuditEventType.PRODUCT_MATERIALIZED)
    details = event["details"]
    assert details["quantity_offer_evidence"] == "PRODUCT_LEVEL"
    assert details["quantity_offer_rule_version"] == "quantity-offer-rule/v1"
    assert details["offers"] == [
        {
            "quantity_offer_id": t.offers[q],
            "tier_ordinal": q - 1,
            "quantity": q,
            "total_krw": total,
            "created": True,
        }
        for q, total in CANONICAL
    ]
    assert details["quantity_items"] == [t.items[q] for q in (1, 2, 3)]
    assert sorted(details["offer_bindings_opened"]) == sorted(t.bindings.values())
    _recorded(container.pricing.price(t.items[2], context()))
    (priced,) = _audit(config, AuditEventType.PRODUCT_PRICING_SNAPSHOT_RECORDED)
    assert (
        priced["details"]["binding_kind"],
        priced["details"]["quantity_offer_id"],
        priced["details"]["fulfillment_quantity"],
    ) == ("SOURCE_OFFER", t.offers[2], 2)
    for audited in (event, priced):
        for forbidden in ("https://", "observed", "shop.example", "개", ".tiers", "label"):
            assert forbidden not in audited["raw"], forbidden


def test_the_quantity_checks_match_the_orm(config: AppConfig) -> None:
    with contextlib.closing(raw(config)) as connection:
        for table in ("quantity_offers", "source_bindings"):
            ddl = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()[0]
            checks = [
                c for c in metadata.tables[table].constraints if isinstance(c, CheckConstraint)
            ]
            assert checks, table
            for check in checks:
                assert f"CHECK ({check.sqltext})" in ddl, f"{table}: {check.name}"


# ---------------------------------------------------------------- 44–49 migration

BEFORE = "0014_m4_derived_image_lineage"
PRESERVED = (
    "product_facts_revisions",
    "product_facts_fields",
    "product_facts_image_refs",
    "source_assets",
    "collection_runs",
    "source_products",
    "current_source_revision_moves",
    "product_groups",
    "group_members",
    "group_membership_revisions",
    "listing_compositions",
    "product_items",
    "source_bindings",
    "pricing_snapshots",
    "current_pricing_snapshot_moves",
    "image_qa_results",
    "audit_events",
)


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _enforcing(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _rows(database: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every table, on the columns both revisions have."""
    with contextlib.closing(sqlite3.connect(database)) as connection:
        rows = {}
        for table in PRESERVED:
            names = [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]
            columns = ", ".join(n for n in names if n != "quantity_offer_id")
            rows[table] = connection.execute(
                f"SELECT {columns} FROM {table} ORDER BY 1, 2"
            ).fetchall()
        return rows


def _services(
    db: Database, tmp_path: Path
) -> tuple[ProductMaterializer, ProductPricingService, ProductImageService, Collections]:
    clock = FakeClock()
    store = ProductFoundationStore(db, clock)
    revisions = ProductFactsRevisionStore(db, clock)
    audit = AuditLog(db, clock)
    return (
        ProductMaterializer(db=db, store=store, revisions=revisions, audit=audit),
        ProductPricingService(store=store, revisions=revisions, audit=audit, clock=clock),
        ProductImageService(
            store=store,
            artifacts=DerivedImageStore(tmp_path / "derived-images", db, _NoDecoder()),
            audit=audit,
            clock=clock,
        ),
        Collections(db, tmp_path / "source-assets"),
    )


class _NoDecoder:
    def decode(self, data: bytes) -> None:
        return None


def _populated_at_0014(tmp_path: Path) -> Path:
    """A BASE_PRODUCT whose binding moved once (one closed, one open), priced, with an image QA
    verdict, at revision 0014. The services write the head schema, so they run at head and the
    database then steps down through 0015's fail-closed downgrade, which keeps every row."""
    database = tmp_path / "icbm.db"
    url = _url(database)
    upgrade_to_head(url)
    db = Database(url)
    try:
        materializer, pricing, images, collections = _services(db, tmp_path)
        run_id, _first = collections.collect(product(price=12900))
        materializer.materialize_run(run_id)
        run_id, second = collections.collect(product(price=9900))
        item = materializer.materialize_run(run_id).item_id
        assert item is not None
        _recorded(pricing.price(item, context()))
        images.record_qa(
            asset_kind=ImageAssetKind.SOURCE_ASSET,
            sha256=str(second.images[0].sha256),
            derivation_id=None,
            validated_source_revision_id=second.revision_id,
            verdict=QaVerdict.PASS,
            decided_by="qa",
        )
    finally:
        db.dispose()
    command.downgrade(alembic_config(url), BEFORE)
    return database


def test_a_fresh_database_migrates_through_0015_to_head(tmp_path: Path) -> None:
    # Kickoff §J 44.
    url = _url(tmp_path / "icbm.db")
    command.upgrade(alembic_config(url), BEFORE)
    command.upgrade(alembic_config(url), "head")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == head_revision() == "0015_m4_quantity_offers"
    finally:
        engine.dispose()
    with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
        columns = [r[1] for r in connection.execute("PRAGMA table_info(source_bindings)")]
        assert columns[-1] == "quantity_offer_id"
        assert connection.execute("SELECT COUNT(*) FROM quantity_offers").fetchone()[0] == 0
        triggers = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                " AND tbl_name IN ('quantity_offers', 'source_bindings', 'pricing_snapshots')"
            )
        }
    assert {
        "trg_quantity_offers_exact_tier",
        "trg_quantity_offers_no_update",
        "trg_quantity_offers_no_delete",
        "trg_source_bindings_scope",
        "trg_source_bindings_close_only",
        "trg_source_bindings_no_delete",
        "trg_pricing_snapshots_prices_current_state",
    } <= triggers


def test_0014_to_0015_keeps_every_row_binding_and_snapshot_and_backfills_no_offer(
    tmp_path: Path,
) -> None:
    # Kickoff §J 45–48: every earlier row, every binding id and window, every snapshot FK.
    database = _populated_at_0014(tmp_path)
    before = _rows(database)
    assert len(before["source_bindings"]) == 2 and before["pricing_snapshots"]
    assert before["image_qa_results"]
    upgrade_to_head(_url(database))
    assert _rows(database) == before
    with contextlib.closing(_enforcing(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM quantity_offers").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM source_bindings WHERE quantity_offer_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        joined = connection.execute(
            "SELECT COUNT(*) FROM pricing_snapshots s"
            " JOIN source_bindings b ON b.binding_id = s.source_binding_id"
        ).fetchone()[0]
        assert joined == len(before["pricing_snapshots"])
        # The rebuilt table keeps its rules: one open binding per Item, only ever closed.
        open_row = connection.execute(
            "SELECT binding_id, item_id FROM source_bindings WHERE valid_to IS NULL"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="only ever closed"):
            connection.execute(
                "UPDATE source_bindings SET decided_by = 'x' WHERE binding_id = ?", (open_row[0],)
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM source_bindings")


def test_downgrade_refuses_while_quantity_history_exists(tmp_path: Path) -> None:
    # Kickoff §J 49: offers and their bindings are never silently destroyed.
    database = tmp_path / "icbm.db"
    url = _url(database)
    upgrade_to_head(url)
    db = Database(url)
    try:
        materializer, _pricing, _images, collections = _services(db, tmp_path)
        run_id, _revision = collections.collect(tiered())
        assert len(materializer.materialize_run(run_id).quantity_offers_created) == 3
    finally:
        db.dispose()
    before = _rows(database)
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), BEFORE)
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0015_m4_quantity_offers"
    finally:
        engine.dispose()
    assert _rows(database) == before
    with contextlib.closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM quantity_offers").fetchone()[0] == 3
