"""ADR-0038 O2: a source-currency product priced at its landed cost, end to end, with its pins.

The real container on a migrated database. Rates are recorded through the FX owner's own store from
invented publications; nothing reaches Korea Eximbank. The clock is the fixture's (Sunday
2026-09-13, 09:00 KST), so Friday's publication is the newest that may price.

Proven here:
- without a rate or any overseas shipping, nothing is priced, and every reason is named;
- a CNY and a JPY product price at the exact landed cost, and the snapshot, its raw row and its
  audit event record every pin; a KRW product records none;
- a new rate, a settings change and the product's own inputs each make the current snapshot STALE,
  and pricing again records the new pins; a stale rate is never priced;
- duties above USD 150 are included, then excluded by the user's choice;
- the database refuses a pin that does not describe its row or names no recorded rate;
- the product's own inputs: API (GET/PUT, CSRF, 404/409/422) and audit;
- migration 0065 is additive, restores 0064 exactly and fails closed while it holds anything.
"""

import contextlib
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.capabilities.overseas.fx import FxSnapshotStore, RefreshTrigger
from app.capabilities.overseas.settings import SaveOverseasSettingsRequest
from app.config import AppConfig
from app.container import Container
from app.platform.db.migrate import alembic_config, upgrade_to_head
from app.stages.collect.facts import (
    FieldFact,
    ForeignMoneyValue,
    ForeignPrice,
    ForeignPricesValue,
    ForeignShippingValue,
    ShippingKind,
)
from app.stages.products.landed_cost import (
    FX_STALE,
    FX_UNAVAILABLE,
    LANDED_COST_COLUMNS,
    OVERSEAS_SHIPPING_UNSET,
    DutyDecision,
    OverseasShippingSource,
)
from app.stages.products.model import ReadinessStatus, Reason
from app.stages.products.overseas_costs import (
    OVERSEAS_COSTS_REVISION_MOVED,
    OVERSEAS_COSTS_SOURCE_UNKNOWN,
    OVERSEAS_COSTS_UNCHANGED,
    SaveOverseasCostsRequest,
)
from app.stages.products.pricing import PriceBasis, PricingMoveReason, SourceInputs, calculate
from app.stages.products.pricing_service import PricingOutcome
from app.stages.products.pricing_store import PricingSnapshotRecord
from app.stages.products.readiness import PRICING_SNAPSHOT_SUPERSEDED
from tests.integration.overseas.test_fx_snapshots import publication
from tests.support.collect_support import confirmed
from tests.support.jobs_support import FakeClock
from tests.support.product_support import (
    PRODUCT,
    SUPPLIER,
    Collections,
    context,
    count,
    product,
    raw,
)

pytestmark = pytest.mark.integration

FRI, MON, THU = date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 17)
HEADERS = {"X-ICBM-Client": "pytest"}
OPERATOR = "operator:local"


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


def _foreign(
    price: str = "5.80", currency: str = "CNY", **overrides: FieldFact
) -> dict[str, FieldFact]:
    fields: dict[str, FieldFact] = {
        "prices": confirmed(
            ForeignPricesValue(
                prices=(ForeignPrice(label="price", currency=currency, amount=price),)
            ),  # type: ignore[arg-type]
            ".price",
        ),
        "shipping": confirmed(
            ForeignShippingValue(kind=ShippingKind.FREE, policy_text="free", currency=currency),  # type: ignore[arg-type]
            ".delivery",
        ),
        **overrides,
    }
    return product(**fields)


def _item(container: Container, sources: Collections, fields: dict[str, FieldFact]) -> str:
    run_id, _revision = sources.collect(fields)
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None
    return result.item_id


def _rates(container: Container, clock: FakeClock, day: date, **rates: str) -> None:
    FxSnapshotStore(container.db, clock, container.audit).record(
        publication(day, **rates),
        trigger=RefreshTrigger.MANUAL,
        actor="test",
        correlation_id="cid-fx",
    )


def _fx_id(config: AppConfig, currency: str, day: date) -> str:
    with contextlib.closing(raw(config)) as connection:
        row = connection.execute(
            "SELECT snapshot_id FROM fx_snapshots WHERE currency = ? AND publication_date = ?",
            (currency, day.isoformat()),
        ).fetchone()
    return str(row[0])


def _settings(container: Container, **changes: Any) -> None:
    current = container.overseas_settings.current()
    body: dict[str, Any] = {
        "expected_revision_no": current.revision_no,
        "fx_margin_percent": current.fx_margin_percent,
        "overseas_shipping_default_krw": current.overseas_shipping_default_krw,
        "duties_included_over_usd150": current.duties_included_over_usd150,
        "duty_rate_percent": current.duty_rate_percent,
        "smartstore_overseas_shipping_address_id": None,
        "overseas_delivery_days_min": 7,
        "overseas_delivery_days_max": 14,
        "importer_label": None,
        **changes,
    }
    container.overseas_settings.save(
        SaveOverseasSettingsRequest(**body), actor=OPERATOR, correlation_id="cid-settings"
    )


def _priced(container: Container, item_id: str) -> PricingSnapshotRecord:
    result = container.pricing.price(item_id, context())
    assert result.outcome is PricingOutcome.RECORDED, result.reasons
    assert result.snapshot is not None
    return result.snapshot


def _row(config: AppConfig, snapshot_id: str) -> dict[str, Any]:
    with contextlib.closing(raw(config)) as connection:
        connection.row_factory = sqlite3.Row
        found = connection.execute(
            "SELECT * FROM pricing_snapshots WHERE pricing_snapshot_id = ?", (snapshot_id,)
        ).fetchone()
    return dict(found)


def _to_kst_noon(clock: FakeClock, day: date) -> None:
    """Move the fixture clock (Sunday 2026-09-13 09:00 KST) to 12:00 KST of ``day``."""
    clock.advance(((day - date(2026, 9, 13)) * 1).days * 86400 + 3 * 3600)


# ---------------------------------------------------------------- fail closed


def test_without_a_rate_or_any_overseas_shipping_nothing_is_priced(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    item_id = _item(container, sources, _foreign())
    result = container.pricing.price(item_id, context())
    assert result.outcome is PricingOutcome.NOT_PRICED
    assert result.reasons == (
        Reason(FX_UNAVAILABLE, ReadinessStatus.REVIEW_REQUIRED, "fx:CNY"),
        Reason(FX_UNAVAILABLE, ReadinessStatus.REVIEW_REQUIRED, "fx:USD"),
        Reason(OVERSEAS_SHIPPING_UNSET, ReadinessStatus.REVIEW_REQUIRED, "overseas_shipping"),
    )
    readiness = container.product_readiness.pricing_readiness(item_id, context())
    assert readiness.status is ReadinessStatus.REVIEW_REQUIRED
    assert count(config, "pricing_snapshots") == 0


def test_a_stale_rate_is_never_priced(
    container: Container, clock: FakeClock, sources: Collections
) -> None:
    item_id = _item(container, sources, _foreign())
    _rates(container, clock, FRI)
    _settings(container, overseas_shipping_default_krw=8000)
    # Thursday is the fourth business day after Friday's publication.
    _to_kst_noon(clock, THU)
    result = container.pricing.price(item_id, context())
    assert result.outcome is PricingOutcome.NOT_PRICED
    assert result.reasons == (
        Reason(FX_STALE, ReadinessStatus.REVIEW_REQUIRED, "fx:CNY"),
        Reason(FX_STALE, ReadinessStatus.REVIEW_REQUIRED, "fx:USD"),
    )


# ---------------------------------------------------------------- the price and its pins


def test_a_cny_product_is_priced_at_its_landed_cost_with_every_pin(
    container: Container, config: AppConfig, clock: FakeClock, sources: Collections
) -> None:
    item_id = _item(container, sources, _foreign())
    _rates(container, clock, FRI)
    _settings(container, overseas_shipping_default_krw=8000)
    snapshot = _priced(container, item_id)
    # 5.80 × 195.12 × 1.05 = 1188.2808 → 1189; shipping is the settings default.
    assert (snapshot.purchase_cost_krw, snapshot.supplier_shipping_krw) == (1189, 8000)
    expected = calculate(SourceInputs(1189, 8000, None), context())
    assert not isinstance(expected, Reason)
    assert snapshot.final_sale_price_krw == expected.final_sale_price_krw
    assert snapshot.price_basis is PriceBasis.TARGET_MARGIN
    pin = snapshot.landed_cost
    assert pin is not None
    assert pin.columns() == {
        "source_currency": "CNY",
        "source_price": "5.80",
        "fx_snapshot_id": _fx_id(config, "CNY", FRI),
        "fx_publication_date": "2026-09-11",
        "usd_fx_snapshot_id": _fx_id(config, "USD", FRI),
        "fx_margin_bp": 500,
        "overseas_settings_revision_no": 1,
        "overseas_cost_override_revision_no": None,
        "overseas_shipping_krw": 8000,
        "overseas_shipping_source": "DEFAULT",
        "duty_decision": "NOT_OVER_150",
        "duty_rate_bp": 800,
        "duties_krw": 0,
        # 5.80 × 195.12 / 1399.9 = 0.8084… → 0.81 (rounded up to the cent).
        "declared_value_usd": "0.81",
    }
    row = _row(config, snapshot.pricing_snapshot_id)
    assert {name: row[name] for name in LANDED_COST_COLUMNS} == pin.columns()
    with contextlib.closing(raw(config)) as connection:
        (details,) = connection.execute(
            "SELECT details_json FROM audit_events WHERE target_ref = ?"
            " AND action = 'pricing_snapshot.record'",
            (snapshot.pricing_snapshot_id,),
        ).fetchone()
    assert json.loads(details)["landed_cost"] == pin.columns()
    readiness = container.product_readiness.pricing_readiness(item_id, context())
    assert readiness.status is ReadinessStatus.READY, readiness.reasons
    # Idempotent: nothing moved, nothing is written.
    again = container.pricing.price(item_id, context())
    assert again.outcome is PricingOutcome.UNCHANGED
    assert count(config, "pricing_snapshots") == 1


def test_a_jpy_product_reads_its_per_100_yen_rate(
    container: Container, clock: FakeClock, sources: Collections
) -> None:
    item_id = _item(container, sources, _foreign("980", "JPY"))
    _rates(container, clock, FRI)
    _settings(container, overseas_shipping_default_krw=8000)
    snapshot = _priced(container, item_id)
    # JPY(100) 938.4 → 9.384 per yen; 980 × 9.384 × 1.05 = 9656.136 → 9657.
    assert snapshot.purchase_cost_krw == 9657
    assert snapshot.landed_cost is not None and snapshot.landed_cost.source_currency == "JPY"


def test_a_stated_foreign_minimum_is_applied_in_won(
    container: Container, clock: FakeClock, sources: Collections
) -> None:
    minimum = confirmed(ForeignMoneyValue(label="min", currency="CNY", amount="200"), ".min")
    item_id = _item(container, sources, _foreign(minimum_sale_price=minimum))
    _rates(container, clock, FRI)
    _settings(container, overseas_shipping_default_krw=8000)
    snapshot = _priced(container, item_id)
    # 200 × 195.12 = 39024, at the TTS without the margin.
    assert snapshot.minimum_sale_price_krw == snapshot.final_sale_price_krw == 39024
    assert snapshot.price_basis is PriceBasis.MINIMUM_SALE_PRICE


def test_a_krw_product_records_no_pin(
    container: Container, config: AppConfig, clock: FakeClock, sources: Collections
) -> None:
    item_id = _item(container, sources, product())
    _rates(container, clock, FRI)
    _settings(container, overseas_shipping_default_krw=8000)
    evaluation = container.pricing.evaluate(item_id, context())
    assert evaluation.dependencies is not None and evaluation.dependencies.landed_cost is None
    snapshot = _priced(container, item_id)
    assert snapshot.landed_cost is None
    row = _row(config, snapshot.pricing_snapshot_id)
    assert {name: row[name] for name in LANDED_COST_COLUMNS} == dict.fromkeys(LANDED_COST_COLUMNS)


# ---------------------------------------------------------------- STALE and re-price


def _superseded(container: Container, item_id: str) -> bool:
    readiness = container.product_readiness.pricing_readiness(item_id, context())
    return readiness.status is ReadinessStatus.STALE and PRICING_SNAPSHOT_SUPERSEDED in {
        reason.code for reason in readiness.reasons
    }


def test_a_new_rate_settings_or_product_input_makes_the_price_stale(
    container: Container, config: AppConfig, clock: FakeClock, sources: Collections
) -> None:
    item_id = _item(container, sources, _foreign())
    _rates(container, clock, FRI)
    _settings(container, overseas_shipping_default_krw=8000)
    first = _priced(container, item_id)
    assert not _superseded(container, item_id)

    # A new publication day: the newest rate now prices.
    _to_kst_noon(clock, MON)
    _rates(container, clock, MON, cny="196.00")
    assert _superseded(container, item_id)
    repriced = container.pricing.price(item_id, context())
    assert repriced.outcome is PricingOutcome.RECORDED and repriced.move is not None
    assert repriced.move.reason is PricingMoveReason.REPRICED
    assert repriced.snapshot is not None and repriced.snapshot.landed_cost is not None
    assert repriced.snapshot.landed_cost.fx_snapshot_id == _fx_id(config, "CNY", MON)
    assert repriced.snapshot.purchase_cost_krw == 1194  # 5.80 × 196 × 1.05 = 1193.64 → 1194
    assert first.landed_cost is not None
    assert first.landed_cost.fx_snapshot_id == _fx_id(config, "CNY", FRI)

    # The product's own overseas shipping.
    container.overseas_costs.save(
        SUPPLIER,
        PRODUCT,
        SaveOverseasCostsRequest(
            expected_revision_no=None, overseas_shipping_krw=12000, duty_rate_percent=None
        ),
        actor=OPERATOR,
        correlation_id="cid-costs",
    )
    assert _superseded(container, item_id)
    own = _priced(container, item_id)
    assert own.supplier_shipping_krw == 12000 and own.landed_cost is not None
    assert own.landed_cost.overseas_shipping_source is OverseasShippingSource.PRODUCT
    assert own.landed_cost.overseas_cost_override_revision_no == 1

    # A settings change (the margin).
    _settings(container, fx_margin_percent="7")
    assert _superseded(container, item_id)
    margin = _priced(container, item_id)
    assert margin.landed_cost is not None
    assert (margin.landed_cost.fx_margin_bp, margin.landed_cost.overseas_settings_revision_no) == (
        700,
        2,
    )
    assert margin.purchase_cost_krw == 1217  # 5.80 × 196 × 1.07 = 1216.376 → 1217
    assert count(config, "pricing_snapshots") == 4


def test_duties_above_usd_150_are_included_then_excluded_by_choice(
    container: Container, clock: FakeClock, sources: Collections
) -> None:
    item_id = _item(container, sources, _foreign("1200.00"))
    _rates(container, clock, FRI)
    _settings(container, overseas_shipping_default_krw=8000)
    included = _priced(container, item_id)
    # Declared 1200 × 195.12 / 1399.9 = USD 167.2577… → 167.26. Customs value 234144;
    # duty ceil(8%) = 18732; VAT ceil((234144 + 18732) × 10%) = 25288. Supplier cost
    # ceil(234144 × 1.05) = 245852.
    pin = included.landed_cost
    assert pin is not None and pin.duty_decision is DutyDecision.INCLUDED
    assert pin.duties_krw == 18732 + 25288 and pin.declared_value_usd == "167.26"
    assert included.purchase_cost_krw == 245852 + 44020
    _settings(container, duties_included_over_usd150=False)
    assert _superseded(container, item_id)
    excluded = _priced(container, item_id)
    assert excluded.landed_cost is not None
    assert excluded.landed_cost.duty_decision is DutyDecision.EXCLUDED
    assert excluded.landed_cost.duties_krw == 0 and excluded.purchase_cost_krw == 245852


# ---------------------------------------------------------------- the database's own refusals


def _copy(config: AppConfig, snapshot_id: str, **changes: object) -> None:
    """Insert a copy of a recorded snapshot with ``changes``, as a raw writer would."""
    with contextlib.closing(raw(config)) as connection:
        columns = [r[1] for r in connection.execute("PRAGMA table_info(pricing_snapshots)")]
        values = {"pricing_snapshot_id": "tampered", **changes}
        select = ", ".join("?" if name in values else name for name in columns)
        args = [values[name] for name in columns if name in values]
        connection.execute(
            f"INSERT INTO pricing_snapshots ({', '.join(columns)}) SELECT {select}"
            " FROM pricing_snapshots WHERE pricing_snapshot_id = ?",
            (*args, snapshot_id),
        )
        connection.commit()


def test_the_database_refuses_a_pin_that_does_not_describe_its_row(
    container: Container, config: AppConfig, clock: FakeClock, sources: Collections
) -> None:
    item_id = _item(container, sources, _foreign())
    _rates(container, clock, FRI)
    _settings(container, overseas_shipping_default_krw=8000)
    snapshot = _priced(container, item_id)
    with pytest.raises(sqlite3.IntegrityError, match="landed_cost_recorded"):
        _copy(config, snapshot.pricing_snapshot_id, duties_krw=5)
    with pytest.raises(sqlite3.IntegrityError, match="landed_cost_recorded"):
        _copy(config, snapshot.pricing_snapshot_id, fx_margin_bp=None)
    with pytest.raises(sqlite3.IntegrityError, match="landed_cost_recorded"):
        _copy(config, snapshot.pricing_snapshot_id, overseas_shipping_krw=1)
    with pytest.raises(sqlite3.IntegrityError, match="source_price_decimal"):
        _copy(config, snapshot.pricing_snapshot_id, source_price="5,80")
    with pytest.raises(sqlite3.IntegrityError, match="recorded rates of one day"):
        _copy(config, snapshot.pricing_snapshot_id, fx_snapshot_id="no-such-rate")
    with pytest.raises(sqlite3.IntegrityError, match="recorded rates of one day"):
        _copy(config, snapshot.pricing_snapshot_id, fx_publication_date="2026-09-10")
    assert count(config, "pricing_snapshots") == 1


def test_a_foreign_snapshot_holds_the_0065_downgrade(
    container: Container, config: AppConfig, clock: FakeClock, sources: Collections, tmp_path: Path
) -> None:
    item_id = _item(container, sources, _foreign())
    _rates(container, clock, FRI)
    _settings(container, overseas_shipping_default_krw=8000)
    _priced(container, item_id)
    # The data directory is the container's: the step down runs on an exact copy of it.
    copy = tmp_path / "copy.db"
    with (
        contextlib.closing(raw(config)) as source,
        contextlib.closing(sqlite3.connect(copy)) as target,
    ):
        source.backup(target)
    url = f"sqlite:///{copy.as_posix()}"
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0064_fx_snapshots")
    with contextlib.closing(sqlite3.connect(copy)) as connection:
        held = connection.execute(
            "SELECT COUNT(*) FROM pricing_snapshots WHERE source_currency = 'CNY'"
        ).fetchone()
    assert held == (1,)


# ---------------------------------------------------------------- the product's own inputs


def test_the_overseas_costs_api_reads_saves_and_refuses(
    client: TestClient, config: AppConfig
) -> None:
    served: Container = client.app.state.container  # type: ignore[attr-defined]
    Collections.of(served, config).collect(_foreign())
    served.product_store.source_product(SUPPLIER, PRODUCT)
    path = f"/api/v1/products/by-source/{SUPPLIER}/{PRODUCT}/overseas-costs"
    read = client.get(path)
    assert read.status_code == 200, read.text
    assert read.json() == {
        "supplier_key": SUPPLIER,
        "source_product_id": PRODUCT,
        "overseas_shipping_krw": None,
        "duty_rate_percent": None,
        "revision_no": None,
        "fingerprint": None,
        "changed_by": None,
        "changed_at": None,
    }
    unknown = client.get(f"/api/v1/products/by-source/{SUPPLIER}/no-such/overseas-costs")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == OVERSEAS_COSTS_SOURCE_UNKNOWN
    body = {"expected_revision_no": None, "overseas_shipping_krw": 12000, "duty_rate_percent": "13"}
    # The global CSRF guard: no client header, no write.
    assert client.put(path, json=body).status_code == 403
    assert count(config, "overseas_cost_override_revisions") == 0
    saved = client.put(path, json=body, headers=HEADERS)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision_no"] == 1 and saved.json()["duty_rate_percent"] == "13"
    assert saved.json()["changed_by"] == config.operator_actor
    unchanged = client.put(path, json=body | {"expected_revision_no": 1}, headers=HEADERS)
    assert unchanged.status_code == 409
    assert unchanged.json()["error"]["code"] == OVERSEAS_COSTS_UNCHANGED
    moved = client.put(path, json=body | {"overseas_shipping_krw": 9000}, headers=HEADERS)
    assert moved.status_code == 409
    assert moved.json()["error"]["code"] == OVERSEAS_COSTS_REVISION_MOVED
    for invalid in (
        {"overseas_shipping_krw": -1},
        {"overseas_shipping_krw": "12000"},
        {"duty_rate_percent": "101"},
        {"duty_rate_percent": 8},
        {"extra": True},
    ):
        refused = client.put(path, json=body | invalid, headers=HEADERS)
        assert refused.status_code == 422, invalid
    cleared = client.put(
        path,
        json={"expected_revision_no": 1, "overseas_shipping_krw": None, "duty_rate_percent": None},
        headers=HEADERS,
    )
    assert cleared.status_code == 200 and cleared.json()["revision_no"] == 2
    assert cleared.json()["overseas_shipping_krw"] is None
    assert count(config, "overseas_cost_override_revisions") == 2
    assert count(config, "audit_events", "event_type = 'OVERSEAS_COST_OVERRIDE_REVISED'") == 2
    with contextlib.closing(raw(config)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="never updated"):
            connection.execute("UPDATE overseas_cost_override_revisions SET duty_rate_bp = 1")
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            connection.execute("DELETE FROM overseas_cost_override_revisions")
        with pytest.raises(sqlite3.IntegrityError, match="follows the one before"):
            connection.execute(
                "INSERT INTO overseas_cost_override_revisions SELECT 'r-x', source_product_uid,"
                " 5, NULL, NULL, fingerprint, changed_by, correlation_id, changed_at"
                " FROM overseas_cost_override_revisions WHERE revision_no = 1"
            )


# ---------------------------------------------------------------- migration 0065


def test_0065_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)
    # Judged at 0065 itself, whatever later revisions add.
    command.downgrade(alembic_config(url), "0065_landed_cost")

    def schema() -> dict[str, str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            return dict(
                connection.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
            )

    def columns(table: str) -> list[str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            return [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]

    at_0065 = schema()
    for table in ("pricing_snapshots", "atomic_sku_pricing_snapshots"):
        assert columns(table)[-len(LANDED_COST_COLUMNS) :] == list(LANDED_COST_COLUMNS)
    assert columns("atomic_sku_source_bindings")[-2:] == ["source_currency", "source_unit_price"]
    assert "overseas_cost_override_revisions" in at_0065
    # Empty, it steps down to exactly 0064 — both 0047 triggers restored — and back up.
    command.downgrade(alembic_config(url), "0064_fx_snapshots")
    at_0064 = schema()
    assert "overseas_cost_override_revisions" not in at_0064
    assert columns("pricing_snapshots")[-1] == "stated_minimum_sale_price_krw"
    trigger = "trg_atomic_sku_pricing_snapshots_prices_current_state"
    assert "source_currency" not in at_0064[trigger]
    assert "b.purchase_cost_krw = NEW.purchase_cost_krw)" in at_0064[trigger]
    assert "source_currency" in at_0065[trigger]
    command.upgrade(alembic_config(url), "0065_landed_cost")
    assert schema() == at_0065
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO overseas_cost_override_revisions VALUES ('r-1', 'sp-1', 1, 12000, NULL,"
            " ?, 'operator', 'cid', '2026-10-14 00:00:00')",
            ("a" * 64,),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0064_fx_snapshots")
    assert schema() == at_0065


def test_the_fixture_clock_is_sunday_morning_kst(clock: FakeClock) -> None:
    from app.capabilities.overseas.fx import KST

    local = clock.now().astimezone(KST)
    assert (local.date(), local.hour) == (date(2026, 9, 13), 9)
    later = FakeClock(clock.now())
    _to_kst_noon(later, MON)
    assert later.now().astimezone(KST).replace(tzinfo=None) == (
        local.replace(tzinfo=None) + timedelta(days=1, hours=3)
    )
