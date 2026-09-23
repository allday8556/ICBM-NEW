"""A RegistrationDraft from a Product DB selection (Gate 1 G1-D, ADR-0015 §5, Issue #89 5796323069).

The real application on a migrated database: Products materialized by M4 from synthetic source
truth, a canonical account bound the way a committed M2 binding leaves it, and a target policy
saved through the durable G1-A owner. Proven here:
- a multi-Item selection is priced by M4 and becomes one Draft with the exact snapshot pins, the
  current policy revision and the revalidated membership; an UNCHANGED snapshot is pinned, not
  duplicated; M4's own price and basis come back, never recomputed;
- one Item M4 does not price refuses the whole command with M4's reasons, and no Draft or Draft
  Item remains, while the snapshot M4 recorded for the other Item stays as M4's history;
- a stale membership, an Item of another Product, an unbound Item, a missing policy, an unknown
  or unbound account, a mismatched pricing context and a malformed request create nothing;
- a Product that moves while it is being priced creates nothing;
- the command creates no preparation, Snapshot, Intent, Attempt or job, and a restart reads the
  same Draft and pins without pricing anything again.
"""

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.collect.facts import FieldFact, QuantityTier, QuantityTiersValue, TextValue
from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.products.materialization import MaterializationStatus
from app.products.model import MoveReason
from app.products.pricing import PriceBasis
from app.products.pricing_service import PricingResult
from app.register.drafting import CreateDraftRequest, DraftCommandService
from app.register.model import ListingShape, RegistrationConflictError
from app.register.policy import StaticRegistrationPolicy
from tests.collect_support import confirmed
from tests.conftest import LOCAL
from tests.gate1_support import CLIENT, MARKET, OPERATOR, pricing_context, save_policy
from tests.product_support import (
    SUPPLIER,
    Collections,
    context,
    minimum,
    product,
    raw,
    unknown_shipping,
)
from tests.register_support import bind, establish, target

pytestmark = pytest.mark.integration

DRAFTS = "/api/v1/register/drafts"
NEVER_BY_THE_COMMAND = (
    "registration_preparations",
    "registration_preparation_revisions",
    "registration_snapshots",
    "registration_item_snapshots",
    "registration_batches",
    "registration_intents",
    "registration_attempts",
)


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "provider-account-1")


def tiers(*quantities: int) -> FieldFact:
    return confirmed(
        QuantityTiersValue(
            tiers=tuple(
                QuantityTier(quantity=q, total_price_krw=q * 9900, label=f"{q}") for q in quantities
            )
        ),
        ".tiers",
    )


def materialize(
    container: Container, sources: Collections, source_id: str, fields: dict[str, FieldFact]
) -> str:
    run_id, _ = sources.collect(fields, source_product_id=source_id)
    result = container.materializer.materialize_run(run_id)
    assert result.status is MaterializationStatus.MATERIALIZED, result
    return str(result.product_group_id)


def join(container: Container, sources: Collections, group: str, source_id: str) -> None:
    """A second source product CONFIRMED into ``group``: the next membership revision."""
    fields = product(original_name=confirmed(TextValue(text="second"), ".name"))
    _, revision = sources.collect(fields, source_product_id=source_id)
    store = container.product_store
    uid = store.source_product(SUPPLIER, source_id).source_product_uid
    store.record_move(
        uid, revision.revision_id, reason=MoveReason.INITIAL, decided_by="t", correlation_id="c"
    )
    store.confirm_new_member(group, uid, reason="t-join", decided_by="t", correlation_id="c")


def selection(api: TestClient, group: str) -> tuple[str, dict[int, str]]:
    detail = api.get(f"/api/v1/products/{group}/detail", headers=CLIENT).json()
    items = {i["composition"]["quantity"]: i["item_id"] for i in detail["product"]["items"]}
    return detail["product"]["membership_revision_id"], items


def body(group: str, membership: str, items: list[str], account: str, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "product_group_id": group,
        "membership_revision_id": membership,
        "item_ids": items,
        "marketplace_key": MARKET,
        "marketplace_account_id": account,
        "listing_shape": "SINGLE_LISTING_WITH_OPTIONS",
        "actor": OPERATOR,
    }
    values.update(overrides)
    return values


def create(api: TestClient, payload: Any) -> Any:
    return api.post(DRAFTS, json=payload, headers=CLIENT)


def counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def refused(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code, error
    return dict(error)


# ---------------------------------------------------------------- the command


def test_a_multi_item_selection_becomes_one_draft_with_exact_m4_pins(
    api: TestClient, config: AppConfig, container: Container, sources: Collections, account: str
) -> None:
    policy_revision = save_policy(api, account)
    group = materialize(container, sources, "S-TIER", product(quantity_tiers=tiers(1, 2, 3)))
    membership, items = selection(api, group)
    chosen = [items[3], items[1], items[2]]
    before = counts(config)
    response = create(api, body(group, membership, chosen, account))
    assert response.status_code == 200, response.text
    created = response.json()
    assert (created["product_group_id"], created["membership_revision_id"]) == (group, membership)
    assert created["policy_revision"] == policy_revision
    assert (created["marketplace_key"], created["marketplace_account_id"]) == (MARKET, account)
    assert created["listing_shape"] == "SINGLE_LISTING_WITH_OPTIONS"
    # One Draft, every chosen Item pinned, in the Product's own Item order.
    draft = container.registrations.draft(created["draft_id"])
    assert draft is not None and draft.draft_revision == created["draft_revision"]
    product_order = [
        i["item_id"]
        for i in api.get(f"/api/v1/products/{group}", headers=CLIENT).json()["items"]
        if i["item_id"] in chosen
    ]
    assert [i.item_id for i in draft.items] == [i["item_id"] for i in created["items"]]
    assert [i.item_id for i in draft.items] == product_order
    for pin, row in zip(created["items"], draft.items, strict=True):
        assert row.pricing_snapshot_id == pin["pricing_snapshot_id"]
        assert row.product_group_id == group
        snapshot = container.registrations.pricing_pin(pin["pricing_snapshot_id"])
        assert snapshot is not None
        assert (snapshot.item_id, snapshot.marketplace_key, snapshot.account_id) == (
            pin["item_id"],
            MARKET,
            None,
        )
        assert snapshot.membership_revision_id == membership
        # M4's own price and basis, as M4 recorded them.
        assert pin["final_sale_price_krw"] == snapshot.final_sale_price_krw
        assert pin["price_basis"] == snapshot.price_basis.value
        assert pin["pricing_outcome"] == "RECORDED"
    after = counts(config)
    assert after["registration_drafts"] == before["registration_drafts"] + 1
    assert after["registration_draft_items"] == before["registration_draft_items"] + 3
    assert after["pricing_snapshots"] == before["pricing_snapshots"] + 3
    assert {t: after[t] for t in NEVER_BY_THE_COMMAND} == {
        t: before[t] for t in NEVER_BY_THE_COMMAND
    }
    assert container.jobs.count(job_type_prefix="register.") == 0
    # The existing Registration Management read model shows the new Draft's unit, DRAFTED.
    units = api.get(f"/api/v1/register/units/{created['draft_id']}", headers=CLIENT).json()
    assert [u["preparation"] for u in units] == ["DRAFTED"]


def test_an_unchanged_snapshot_is_pinned_and_not_duplicated(
    api: TestClient, config: AppConfig, container: Container, sources: Collections, account: str
) -> None:
    save_policy(api, account)
    group = materialize(container, sources, "S-ONE", product(minimum_sale_price=minimum(15000)))
    membership, items = selection(api, group)
    policy = container.registration_preflight._policies.target(MARKET, account)
    assert policy is not None
    earlier = container.pricing.price(items[1], policy.pricing_context).snapshot
    assert earlier is not None
    before = counts(config)["pricing_snapshots"]
    created = create(api, body(group, membership, [items[1]], account)).json()
    pin = created["items"][0]
    assert (pin["pricing_outcome"], pin["pricing_snapshot_id"]) == (
        "UNCHANGED",
        earlier.pricing_snapshot_id,
    )
    assert counts(config)["pricing_snapshots"] == before
    # The minimum sale price is M4's decision, carried as M4 recorded it.
    assert pin["price_basis"] == PriceBasis.MINIMUM_SALE_PRICE.value
    assert pin["final_sale_price_krw"] == earlier.final_sale_price_krw


def test_one_unpriced_item_leaves_no_draft_and_returns_m4s_reasons(
    api: TestClient, config: AppConfig, container: Container, sources: Collections, account: str
) -> None:
    save_policy(api, account)
    group = materialize(container, sources, "S-A", product(quantity_tiers=tiers(1, 2)))
    # A second member states three-packs, with shipping M4 cannot price.
    fields = product(quantity_tiers=tiers(3), shipping=unknown_shipping())
    _, revision = sources.collect(fields, source_product_id="S-B")
    store = container.product_store
    uid = store.source_product(SUPPLIER, "S-B").source_product_uid
    store.record_move(
        uid, revision.revision_id, reason=MoveReason.INITIAL, decided_by="t", correlation_id="c"
    )
    store.confirm_new_member(group, uid, reason="t-join", decided_by="t", correlation_id="c")
    container.materializer.materialize_source(SUPPLIER, "S-B")
    membership, items = selection(api, group)
    assert set(items) == {1, 2, 3}
    before = counts(config)
    error = refused(
        create(api, body(group, membership, [items[1], items[3]], account)),
        409,
        "REGISTER_DRAFT_ITEM_NOT_PRICED",
    )
    reasons = error["details"]["items"]
    assert set(reasons) == {items[3]}
    direct = container.pricing.price(
        items[3], container.registration_preflight._policies.target(MARKET, account).pricing_context
    )
    assert reasons[items[3]] == [{"code": r.code, "subject": r.subject} for r in direct.reasons]
    after = counts(config)
    # No Draft and no Draft Item; the snapshot M4 recorded for the priced Item is M4's history.
    assert (after["registration_drafts"], after["registration_draft_items"]) == (
        before["registration_drafts"],
        before["registration_draft_items"],
    )
    assert after["pricing_snapshots"] == before["pricing_snapshots"] + 1


def test_a_stale_or_foreign_selection_creates_nothing(
    api: TestClient, config: AppConfig, container: Container, sources: Collections, account: str
) -> None:
    save_policy(api, account)
    group = materialize(container, sources, "S-TIER", product(quantity_tiers=tiers(1, 2, 3)))
    other = materialize(container, sources, "S-OTHER", product())
    membership, items = selection(api, group)
    _, foreign = selection(api, other)
    before = counts(config)
    # An Item of another Product.
    refused(
        create(api, body(group, membership, [items[1], foreign[1]], account)),
        409,
        "PRODUCTS_SELECTION_ITEM_OUTSIDE_PRODUCT",
    )
    # A newer revision drops the three-pack: its Item stays, unbound.
    run_id, _ = sources.collect(product(quantity_tiers=tiers(1, 2)), source_product_id="S-TIER")
    container.materializer.materialize_run(run_id)
    refused(
        create(api, body(group, membership, [items[3]], account)),
        409,
        "PRODUCTS_SELECTION_ITEM_NOT_SELECTABLE",
    )
    # The membership moves after the operator chose.
    join(container, sources, group, "S-JOIN")
    refused(
        create(api, body(group, membership, [items[1]], account)),
        409,
        "PRODUCTS_SELECTION_MEMBERSHIP_STALE",
    )
    # Malformed selections are refused whole.
    refused(create(api, body(group, membership, [], account)), 422, "REQUEST_INVALID")
    refused(
        create(api, body(group, membership, [items[1], items[1]], account)),
        422,
        "PRODUCTS_SELECTION_INVALID",
    )
    after = counts(config)
    assert after["registration_drafts"] == before["registration_drafts"]
    assert after["pricing_snapshots"] == before["pricing_snapshots"], "nothing was priced"


def test_a_missing_policy_or_a_wrong_account_creates_nothing(
    api: TestClient, config: AppConfig, container: Container, sources: Collections, account: str
) -> None:
    group = materialize(container, sources, "S-ONE", product())
    membership, items = selection(api, group)
    before = counts(config)
    refused(
        create(api, body(group, membership, [items[1]], account)),
        409,
        "REGISTER_TARGET_POLICY_MISSING",
    )
    refused(
        create(api, body(group, membership, [items[1]], "no-such-account")),
        404,
        "REGISTER_TARGET_ACCOUNT_UNKNOWN",
    )
    refused(
        create(api, body(group, membership, [items[1]], account, marketplace_key="coupang")),
        404,
        "REGISTER_TARGET_ACCOUNT_UNKNOWN",
    )
    save_policy(api, account)
    # The provider identity was rebound to another account since: this one is no longer bound.
    bind(config, MARKET, "provider-account-2")
    error = refused(
        create(api, body(group, membership, [items[1]], account)),
        403,
        "MARKETPLACE_ACCOUNT_NOT_BOUND",
    )
    assert error["details"]["binding"] != "BOUND"
    after = counts(config)
    assert after["registration_drafts"] == before["registration_drafts"]
    assert after["pricing_snapshots"] == before["pricing_snapshots"], "nothing was priced"


def test_the_client_supplies_no_server_owned_value(
    api: TestClient, config: AppConfig, container: Container, sources: Collections, account: str
) -> None:
    save_policy(api, account)
    group = materialize(container, sources, "S-ONE", product())
    membership, items = selection(api, group)
    for extra in (
        {"pricing_snapshot_id": "x"},
        {"policy_revision": "x"},
        {"pricing_context": pricing_context()},
        {"sale_price_krw": 1},
        {"draft_id": "x"},
    ):
        response = create(api, {**body(group, membership, [items[1]], account), **extra})
        assert response.status_code == 422, extra
    refused(
        create(api, body(group, membership, [items[1]], account, listing_shape="ONE_BIG_LISTING")),
        422,
        "REQUEST_INVALID",
    )
    assert counts(config)["registration_drafts"] == 0


# ---------------------------------------------------------------- owner-level guards


def _service(container: Container, pricing: Any, policies: Any = None) -> DraftCommandService:
    return DraftCommandService(
        products=container.products,
        accounts=container.accounts,
        policies=policies or container.registration_preflight._policies,
        pricing=pricing,
        registrations=container.registrations,
        marketplaces=[MARKET],
    )


def test_a_pricing_context_of_another_account_is_refused(
    api: TestClient, config: AppConfig, container: Container, sources: Collections, account: str
) -> None:
    group = materialize(container, sources, "S-ONE", product())
    membership, items = selection(api, group)
    # This account's policy, whose pricing context names another account: never repaired here.
    foreign = target(
        account,
        marketplace_key=MARKET,
        pricing_context=context(marketplace_key=MARKET, account_id="another-account"),
    )
    service = _service(container, container.pricing, StaticRegistrationPolicy((foreign,)))
    request = CreateDraftRequest(**body(group, membership, [items[1]], account))
    with pytest.raises(RegistrationConflictError) as caught:
        service.create(request, correlation_id="cid-g1d")
    assert caught.value.code == "REGISTER_PRICING_CONTEXT_MISMATCH"
    assert counts(config)["registration_drafts"] == 0


class _MovingPricing:
    """M4 pricing, with the Product's membership moving just before the first Item is priced."""

    def __init__(self, container: Container, move: Callable[[], None]) -> None:
        self._pricing = container.pricing
        self._move: Callable[[], None] | None = move

    def price(
        self, item_id: str, context: Any, *, correlation_id: str | None = None
    ) -> PricingResult:
        if self._move is not None:
            move, self._move = self._move, None
            move()
        return self._pricing.price(item_id, context, correlation_id=correlation_id)


def test_a_product_that_moves_while_it_is_priced_creates_nothing(
    api: TestClient, config: AppConfig, container: Container, sources: Collections, account: str
) -> None:
    save_policy(api, account)
    group = materialize(container, sources, "S-ONE", product())
    membership, items = selection(api, group)
    moving = _MovingPricing(container, lambda: join(container, sources, group, "S-LATE"))
    service = _service(container, moving)
    request = CreateDraftRequest(**body(group, membership, [items[1]], account))
    with pytest.raises(RegistrationConflictError) as caught:
        service.create(request, correlation_id="cid-g1d")
    assert caught.value.code == "REGISTER_DRAFT_SELECTION_MOVED"
    assert caught.value.details == {"item_ids": [items[1]]}
    assert counts(config)["registration_drafts"] == 0


# ---------------------------------------------------------------- targets and restart


def test_the_targets_are_the_owners_accounts_bindings_and_policies(
    api: TestClient, config: AppConfig, account: str
) -> None:
    listed = api.get("/api/v1/register/draft-targets", headers=CLIENT).json()
    assert listed["listing_shapes"] == [shape.value for shape in ListingShape]
    assert listed["targets"] == [
        {
            "marketplace_key": MARKET,
            "marketplace_account_id": account,
            "binding": "BOUND",
            "policy_revision": None,
            "pricing_account_scoped": None,
            "unavailable_reason": "REGISTER_TARGET_POLICY_MISSING",
        }
    ]
    revision = save_policy(api, account, account_scoped=True)
    (entry,) = api.get("/api/v1/register/draft-targets", headers=CLIENT).json()["targets"]
    assert (entry["policy_revision"], entry["pricing_account_scoped"]) == (revision, True)
    assert entry["unavailable_reason"] is None


def test_a_restart_reads_the_same_draft_and_pins_without_pricing_again(
    config: AppConfig,
) -> None:
    with TestClient(create_app(config), base_url=LOCAL) as api:
        served: Container = api.app.state.container
        account = establish(served, config, MARKET, "provider-account-1")
        save_policy(api, account, account_scoped=True)
        group = materialize(
            served, Collections.of(served, config), "S-TIER", product(quantity_tiers=tiers(1, 2))
        )
        membership, items = selection(api, group)
        created = create(api, body(group, membership, [items[1], items[2]], account)).json()
        units = api.get(f"/api/v1/register/units/{created['draft_id']}", headers=CLIENT).json()
        before = counts(config)
    with TestClient(create_app(config), base_url=LOCAL) as api:
        served = api.app.state.container
        draft = served.registrations.draft(created["draft_id"])
        assert draft is not None
        assert [(i.item_id, i.pricing_snapshot_id) for i in draft.items] == [
            (pin["item_id"], pin["pricing_snapshot_id"]) for pin in created["items"]
        ]
        for pin in created["items"]:
            snapshot = served.registrations.pricing_pin(pin["pricing_snapshot_id"])
            assert snapshot is not None and snapshot.account_id == account
        again = api.get(f"/api/v1/register/units/{created['draft_id']}", headers=CLIENT).json()
        assert [u["unit_ref"] for u in again] == [u["unit_ref"] for u in units]
        assert counts(config)["pricing_snapshots"] == before["pricing_snapshots"]
