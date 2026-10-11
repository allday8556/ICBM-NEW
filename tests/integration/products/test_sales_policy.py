"""ADR-0039 P1: the per-supplier sales policy on the real owners.

A migrated database with the real COLLECT store, Product owner, pricing and readiness owners, the
REGISTER preflight and Snapshot builder, and the application API. Every product is invented; no
supplier, marketplace or AI provider is contacted.

Proven here:
- a supplier with no revision uses the defaults, pins nothing, and every gate and price is exactly
  what it was (SP-02);
- a save appends one audited revision; a save against a moved policy, or one that changes
  nothing, is refused and writes nothing; the revisions are append-only in the database;
- with ``apply_channel_restrictions`` false a forbidding or unread statement is a READY-status
  warning, the candidate is READY and permits the upload an ASSET grant requires, and the final
  freezes (§2);
- with ``apply_minimum_price`` false the price ignores the minimum, an unread minimum no longer
  stops pricing, and the snapshot records ``minimum_applied`` and the stated minimum (§3);
- a policy change makes a READY preparation stale, exactly as a target-policy change does (SP-03);
- the API reads and saves through the CSRF guard and refuses an unknown supplier.
"""

import contextlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.platform.db.migrate import alembic_config, upgrade_to_head
from app.stages.collect.facts import (
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    SalesChannelScope,
    SalesChannelsValue,
)
from app.stages.products.model import ReadinessStatus
from app.stages.products.pricing import MINIMUM_SALE_PRICE_NOT_APPLIED, PriceBasis
from app.stages.products.pricing_service import PricingOutcome
from app.stages.products.sales_policy import (
    SALES_POLICY_REVISION_MOVED,
    SALES_POLICY_UNCHANGED,
    SalesPolicyConflictError,
    SaveSalesPolicyRequest,
)
from app.stages.products.source_gates import (
    SOURCE_CHANNEL_FORBIDDEN,
    SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED,
    SOURCE_CHANNEL_UNRESOLVED,
    SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED,
    SourceGates,
)
from app.stages.register.builder import RegistrationSnapshotBuilder
from app.stages.register.model import RegistrationConflictError
from app.stages.register.preflight import RegistrationPreflightService
from tests.support.product_support import (
    SUPPLIER,
    Collections,
    context,
    count,
    minimum,
    product,
    raw,
    review,
)
from tests.support.register_support import (
    CID,
    MARKET,
    OPERATOR,
    Preparation,
    ReadyItem,
    draft,
    establish,
    preparation,
    ready_final,
    ready_item,
    request,
)

pytestmark = pytest.mark.integration

HEADERS = {"X-ICBM-Client": "pytest"}
EVENT = "SUPPLIER_SALES_POLICY_REVISED"


def _channels(scope: SalesChannelScope) -> FieldFact:
    return FieldFact(
        FieldStatus.CONFIRMED,
        SalesChannelsValue(scope=scope, forbidden=(), policy_text="폐쇄몰 전용 상품입니다"),
        (Evidence(EvidenceKind.DOM_TEXT, "th:판매가능플랫폼 + td", FieldStatus.CONFIRMED),),
    )


UNREAD = FieldFact(
    FieldStatus.REVIEW_REQUIRED,
    None,
    (Evidence(EvidenceKind.DOM_TEXT, "th:판매가능플랫폼 + td", FieldStatus.REVIEW_REQUIRED),),
)


def _save(
    container: Container,
    expected: int | None,
    *,
    channels: bool = True,
    minimum_price: bool = True,
) -> int | None:
    saved = container.sales_policies.save(
        SUPPLIER,
        SaveSalesPolicyRequest(
            expected_revision_no=expected,
            apply_channel_restrictions=channels,
            apply_minimum_price=minimum_price,
        ),
        actor=OPERATOR,
        correlation_id=CID,
    )
    return saved.revision_no


def _gated(container: Container, account: str) -> Preparation:
    """The test preparation's sources, over a preflight that reads the real source gates and the
    sales-policy pins exactly as the application wires them."""
    prep = preparation(container, account)
    gates = SourceGates(container.product_store, lambda key: None)
    service = RegistrationPreflightService(
        registrations=container.registrations,
        readiness=container.product_readiness,
        pricing=container.pricing,
        images=container.images,
        capability=prep.capability,
        metadata=prep.metadata,
        policies=prep.policies,
        source_gates=gates,
        sales_policies=gates.policy_pins,
    )
    return replace(prep, service=service)


def _item(container: Container, config: AppConfig, **fields: FieldFact) -> ReadyItem:
    return ready_item(container, Collections.of(container, config), fields=product(**fields))


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


# ---------------------------------------------------------------- the owner


def test_a_supplier_with_no_revision_uses_the_defaults_and_pins_nothing(
    container: Container, config: AppConfig
) -> None:
    view = container.sales_policies.policy(SUPPLIER)
    assert (view.apply_channel_restrictions, view.apply_minimum_price) == (True, True)
    assert (view.revision_no, view.changed_by, view.changed_at) == (None, None, None)
    assert view.surface == "SUPPLIER_SALES_POLICY" and view.editable
    item = _item(container, config, sales_channels=_channels(SalesChannelScope.CLOSED_MALL_ONLY))
    gates = SourceGates(container.product_store, lambda key: None)
    assert gates(MARKET, [item.item_id]) == ((item.item_id, SOURCE_CHANNEL_FORBIDDEN),)
    assert gates.policy_pins([item.item_id]) == ()
    assert count(config, "supplier_sales_policy_revisions") == 0


def test_a_save_appends_one_audited_revision(container: Container, config: AppConfig) -> None:
    assert _save(container, None, channels=False) == 1
    view = container.sales_policies.policy(SUPPLIER)
    assert (view.apply_channel_restrictions, view.apply_minimum_price) == (False, True)
    assert (view.revision_no, view.changed_by) == (1, OPERATOR)
    assert view.changed_at is not None and view.fingerprint is not None
    assert _save(container, 1, channels=False, minimum_price=False) == 2
    with contextlib.closing(raw(config)) as connection:
        events = connection.execute(
            "SELECT actor, target_ref, correlation_id FROM audit_events WHERE event_type = ?"
            " ORDER BY seq",
            (EVENT,),
        ).fetchall()
    assert events == [(OPERATOR, SUPPLIER, CID), (OPERATOR, SUPPLIER, CID)]
    gates = SourceGates(container.product_store, lambda key: None)
    item = _item(container, config)
    assert gates.policy_pins([item.item_id]) == (
        (item.item_id, SUPPLIER, 2, container.sales_policies.policy(SUPPLIER).fingerprint),
    )


def test_a_save_against_a_moved_policy_or_with_no_change_is_refused(
    container: Container, config: AppConfig
) -> None:
    with pytest.raises(SalesPolicyConflictError) as unchanged:
        _save(container, None)
    assert unchanged.value.code == SALES_POLICY_UNCHANGED
    assert _save(container, None, channels=False) == 1
    for stale in (None, 2):
        with pytest.raises(SalesPolicyConflictError) as moved:
            _save(container, stale, minimum_price=False)
        assert moved.value.code == SALES_POLICY_REVISION_MOVED
        assert moved.value.details == {"current_revision_no": 1}
    assert count(config, "supplier_sales_policy_revisions") == 1
    assert count(config, "audit_events", "event_type = ?", EVENT) == 1


def test_the_revisions_are_append_only(container: Container, config: AppConfig) -> None:
    _save(container, None, channels=False)
    with contextlib.closing(raw(config)) as connection:
        for statement in (
            "UPDATE supplier_sales_policy_revisions SET apply_minimum_price = 0",
            "DELETE FROM supplier_sales_policy_revisions",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
        # A gap in the numbering is refused by the database itself.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO supplier_sales_policy_revisions (revision_id, supplier_key,"
                " revision_no, apply_channel_restrictions, apply_minimum_price, fingerprint,"
                " changed_by, correlation_id, changed_at) VALUES ('r-x', ?, 3, 1, 1, ?, 'x',"
                " 'cid', '2026-10-11 00:00:00')",
                (SUPPLIER, "a" * 64),
            )


# ---------------------------------------------------------------- §2 channel restrictions


def test_a_restriction_not_applied_is_a_warning_on_the_fact_unchanged(
    container: Container, config: AppConfig
) -> None:
    forbidden = _item(
        container, config, sales_channels=_channels(SalesChannelScope.CLOSED_MALL_ONLY)
    )
    unread = ready_item(
        container,
        Collections.of(container, config),
        source_product_id="5678",
        fields=product(sales_channels=UNREAD),
    )
    ids = [forbidden.item_id, unread.item_id]
    gates = SourceGates(container.product_store, lambda key: None)
    assert set(gates(MARKET, ids)) == {
        (forbidden.item_id, SOURCE_CHANNEL_FORBIDDEN),
        (unread.item_id, SOURCE_CHANNEL_UNRESOLVED),
    }
    _save(container, None, channels=False)
    assert set(gates(MARKET, ids)) == {
        (forbidden.item_id, SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED),
        (unread.item_id, SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED),
    }
    # SP-01: the fact is what the page said; only its enforcement moved.
    with contextlib.closing(raw(config)) as connection:
        statuses = {
            row[0]
            for row in connection.execute(
                "SELECT status FROM product_facts_fields WHERE field_key = 'sales_channels'"
            )
        }
    assert statuses == {"CONFIRMED", "REVIEW_REQUIRED"}
    _save(container, 1, channels=True)
    assert set(gates(MARKET, ids)) == {
        (forbidden.item_id, SOURCE_CHANNEL_FORBIDDEN),
        (unread.item_id, SOURCE_CHANNEL_UNRESOLVED),
    }


def test_a_restriction_not_applied_leaves_the_candidate_ready_and_the_grant_open(
    container: Container, config: AppConfig, account: str
) -> None:
    item = _item(container, config, sales_channels=_channels(SalesChannelScope.CLOSED_MALL_ONLY))
    prep = _gated(container, account)
    draft_id = draft(container.registrations, account, [item])
    req = request(container.registrations, draft_id, account, [item])
    blocked = prep.service.candidate(req)
    assert blocked.status is ReadinessStatus.BLOCKED
    assert (SOURCE_CHANNEL_FORBIDDEN, ReadinessStatus.BLOCKED) in {
        (r.code, r.status) for r in blocked.reasons
    }
    assert not blocked.upload_permitted
    _save(container, None, channels=False)
    req, final = ready_final(prep, req)
    candidate = prep.service.candidate(req)
    # The ASSET grant's own predicate (LiveAuthorityService): a READY candidate that permits the
    # upload. The warning is on the unit, with READY status, and never hidden (SP-04).
    assert candidate.status is ReadinessStatus.READY and candidate.upload_permitted
    warning = [r for r in candidate.reasons if r.code == SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED]
    assert [(r.status, r.subject) for r in warning] == [(ReadinessStatus.READY, item.item_id)]
    assert SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED in final.codes
    builder = RegistrationSnapshotBuilder(
        preflight=prep.service, registrations=container.registrations
    )
    frozen = builder.freeze(final, created_by=OPERATOR, correlation_id=CID)
    assert frozen.preflight_fingerprint == final.dependency_fingerprint


# ---------------------------------------------------------------- SP-03 the preparation pin


def test_a_policy_change_makes_the_preparation_stale(
    container: Container, config: AppConfig, account: str
) -> None:
    item = _item(container, config)
    prep = _gated(container, account)
    plain = preparation(container, account)
    draft_id = draft(container.registrations, account, [item])
    req = request(container.registrations, draft_id, account, [item])
    req, before = ready_final(prep, req)
    # No revision: the gated preflight's fingerprint is exactly the one without the policy owner.
    assert "sales_policies" not in before.dependencies["candidate"]
    assert (
        prep.service.candidate(req).candidate_fingerprint
        == plain.service.candidate(req).candidate_fingerprint
    )
    _save(container, None, channels=False)
    after = prep.service.final(req, ())
    assert after.dependencies["candidate"]["sales_policies"] == [
        [item.item_id, SUPPLIER, 1, container.sales_policies.policy(SUPPLIER).fingerprint]
    ]
    builder = RegistrationSnapshotBuilder(
        preflight=prep.service, registrations=container.registrations
    )
    with pytest.raises(RegistrationConflictError) as refused:
        builder.freeze(before, created_by=OPERATOR, correlation_id=CID)
    assert refused.value.code == "REGISTER_PREFLIGHT_STALE"
    _req, refreshed = ready_final(prep, req)
    assert refreshed.dependency_fingerprint != before.dependency_fingerprint
    builder.freeze(refreshed, created_by=OPERATOR, correlation_id=CID)


# ---------------------------------------------------------------- §3 the minimum sale price


def test_a_minimum_not_applied_is_ignored_by_the_price_and_recorded(
    container: Container, config: AppConfig
) -> None:
    item = _item(container, config, price=10000, minimum_sale_price=minimum(50000))
    pricing = context()
    applied = container.pricing.price(item.item_id, pricing)
    assert applied.snapshot is not None
    assert (applied.snapshot.final_sale_price_krw, applied.snapshot.price_basis) == (
        50000,
        PriceBasis.MINIMUM_SALE_PRICE,
    )
    assert (
        applied.snapshot.minimum_applied and applied.snapshot.stated_minimum_sale_price_krw is None
    )
    _save(container, None, minimum_price=False)
    # The snapshot priced under the applied policy no longer prices the current state.
    stale = container.product_readiness.pricing_readiness(item.item_id, pricing)
    assert stale.status is ReadinessStatus.STALE
    waived = container.pricing.price(item.item_id, pricing)
    assert waived.outcome is PricingOutcome.RECORDED and waived.snapshot is not None
    snapshot = waived.snapshot
    assert snapshot.price_basis is PriceBasis.TARGET_MARGIN
    assert snapshot.final_sale_price_krw == snapshot.target_margin_price_krw < 50000
    assert snapshot.minimum_sale_price_krw is None
    assert (snapshot.minimum_applied, snapshot.stated_minimum_sale_price_krw) == (False, 50000)
    assert snapshot.dependency_fingerprint != applied.snapshot.dependency_fingerprint
    with contextlib.closing(raw(config)) as connection:
        row = connection.execute(
            "SELECT minimum_applied, stated_minimum_sale_price_krw, minimum_sale_price_krw"
            " FROM pricing_snapshots WHERE pricing_snapshot_id = ?",
            (snapshot.pricing_snapshot_id,),
        ).fetchone()
    assert row == (0, 50000, None)
    ready = container.product_readiness.pricing_readiness(item.item_id, pricing)
    assert ready.status is ReadinessStatus.READY
    assert [(r.code, r.status) for r in ready.reasons] == [
        (MINIMUM_SALE_PRICE_NOT_APPLIED, ReadinessStatus.READY)
    ]
    # Applied again: the default fingerprint, and a new snapshot at the minimum.
    _save(container, 1, minimum_price=True)
    again = container.pricing.price(item.item_id, pricing)
    assert again.snapshot is not None
    assert again.snapshot.dependency_fingerprint == applied.snapshot.dependency_fingerprint
    assert again.snapshot.final_sale_price_krw == 50000 and again.snapshot.minimum_applied


def test_an_unread_minimum_blocks_pricing_only_while_applied(
    container: Container, config: AppConfig
) -> None:
    run_id, _revision = Collections.of(container, config).collect(
        product(minimum_sale_price=review(".minimum-price"))
    )
    item_id = container.materializer.materialize_run(run_id).item_id
    assert item_id is not None
    refused = container.pricing.price(item_id, context())
    assert refused.outcome is PricingOutcome.NOT_PRICED
    assert [r.code for r in refused.reasons] == ["PRICING_MINIMUM_SALE_PRICE_UNRESOLVED"]
    _save(container, None, minimum_price=False)
    priced = container.pricing.price(item_id, context())
    assert priced.outcome is PricingOutcome.RECORDED and priced.snapshot is not None
    assert not priced.snapshot.minimum_applied
    assert priced.snapshot.stated_minimum_sale_price_krw is None
    readiness = container.product_readiness.pricing_readiness(item_id, context())
    assert [(r.code, r.status) for r in readiness.reasons] == [
        (MINIMUM_SALE_PRICE_NOT_APPLIED, ReadinessStatus.READY)
    ]


def test_the_default_policy_prices_exactly_as_before(
    container: Container, config: AppConfig
) -> None:
    item = _item(container, config, price=10000, minimum_sale_price=minimum(50000))
    before = container.pricing.evaluate(item.item_id, context()).dependency_fingerprint
    # A revision that applies both, as the defaults do, changes no pricing fingerprint.
    _save(container, None, channels=False)
    _save(container, 1, channels=True)
    assert container.pricing.evaluate(item.item_id, context()).dependency_fingerprint == before


# ---------------------------------------------------------------- the API


def test_the_api_reads_saves_and_refuses(client: TestClient, config: AppConfig) -> None:
    path = f"/api/v1/suppliers/{SUPPLIER}/sales-policy"
    read = client.get(path)
    assert read.status_code == 200, read.text
    assert read.json()["revision_no"] is None
    assert read.json()["apply_channel_restrictions"] is True
    unknown = client.get("/api/v1/suppliers/no-such-supplier/sales-policy")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "SALES_POLICY_SUPPLIER_UNKNOWN"
    body = {
        "expected_revision_no": None,
        "apply_channel_restrictions": False,
        "apply_minimum_price": True,
    }
    # The global CSRF guard: no client header, no write.
    unguarded = client.put(path, json=body)
    assert unguarded.status_code == 403
    assert count(config, "supplier_sales_policy_revisions") == 0
    refused = client.put(
        "/api/v1/suppliers/no-such-supplier/sales-policy", json=body, headers=HEADERS
    )
    assert refused.status_code == 404
    saved = client.put(path, json=body, headers=HEADERS)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision_no"] == 1
    assert saved.json()["changed_by"] == config.operator_actor
    moved = client.put(path, json=body | {"apply_minimum_price": False}, headers=HEADERS)
    assert moved.status_code == 409
    assert moved.json()["error"]["code"] == SALES_POLICY_REVISION_MOVED
    invalid = client.put(path, json=body | {"apply_minimum_price": "no"}, headers=HEADERS)
    assert invalid.status_code == 422
    assert count(config, "supplier_sales_policy_revisions") == 1
    settings = client.get("/api/v1/screens/settings").json()
    assert "SUPPLIER_SALES_POLICY" in settings["editable_surfaces"]


# ---------------------------------------------------------------- migration 0060


def test_0060_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)

    def shape() -> tuple[set[str], list[str]]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            columns = [r[1] for r in connection.execute("PRAGMA table_info(pricing_snapshots)")]
        return tables, columns

    tables, columns = shape()
    assert "supplier_sales_policy_revisions" in tables
    assert columns[-2:] == ["minimum_applied", "stated_minimum_sale_price_krw"]
    # Empty, it steps down to exactly what it added, and back up.
    command.downgrade(alembic_config(url), "0059_detail_guidance_placement")
    assert shape() == (tables - {"supplier_sales_policy_revisions"}, columns[:-2])
    command.upgrade(alembic_config(url), "head")
    assert shape() == (tables, columns)
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO supplier_sales_policy_revisions VALUES ('r-1', 'kmretail', 1, 0, 1,"
            " ?, 'operator', 'cid', '2026-10-11 00:00:00')",
            ("a" * 64,),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0059_detail_guidance_placement")
    assert shape() == (tables, columns)
