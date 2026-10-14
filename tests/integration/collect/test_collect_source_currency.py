"""ADR-0038 O1a: the source currency a revision states, as stored, read back and migrated (0063).

The revision store, the only writer, records the one currency the values state; a KRW revision
records none; a page that mixes currencies is stored REVIEW_REQUIRED with no amount, never mixed.
"""

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command

from app.container import Container
from app.platform.core.errors import InputValidationError
from app.platform.db.migrate import alembic_config, upgrade_to_head
from app.stages.collect import revisions as revision_module
from app.stages.collect.facts import (
    CURRENCY_MIXED_LOCATOR,
    EvaluatedFacts,
    FactsStatus,
    FieldStatus,
    ForeignPrice,
    ForeignPricesValue,
    ForeignShippingValue,
    ShippingKind,
    ShippingValue,
    evaluate,
)
from tests.support.collect_support import base_fields, collected, confirmed

pytestmark = pytest.mark.integration

TABLE = "product_facts_revisions"
BEFORE = "0062_onboarding_sample_retirements"


def _jpy_fields() -> dict[str, object]:
    fields: dict[str, object] = dict(base_fields())
    fields["prices"] = confirmed(
        ForeignPricesValue(prices=(ForeignPrice(label="税込", currency="JPY", amount="1100"),)),
        ".price",
    )
    fields["shipping"] = confirmed(
        ForeignShippingValue(
            kind=ShippingKind.FIXED, policy_text="送料", currency="JPY", fee="800"
        ),
        ".delivery",
    )
    return fields


def test_the_store_records_the_one_currency_and_reads_it_back(container: Container) -> None:
    stored = container.revisions.append(collected(fields=_jpy_fields(), images=()))  # type: ignore[arg-type]
    assert (stored.source_currency, stored.currency) == ("JPY", "KRW")
    assert stored.fingerprints_intact()
    prices = stored.fields["prices"].value
    assert isinstance(prices, ForeignPricesValue) and prices.purchase() is not None
    assert prices.prices[0].amount == "1100"
    view = container.source_truth.revision(stored.revision_id)
    assert (view.source_currency, view.currency) == ("JPY", "KRW")
    assert json.loads(view.model_dump_json())["source_currency"] == "JPY"
    history = container.source_truth.history("kmretail", "1234")
    assert [revision.source_currency for revision in history.revisions] == ["JPY"]


def test_a_krw_revision_records_no_source_currency(container: Container) -> None:
    stored = container.revisions.append(collected(images=()))
    assert stored.source_currency is None
    assert container.source_truth.revision(stored.revision_id).source_currency is None


def test_a_mixed_page_is_stored_under_review_with_no_amount(container: Container) -> None:
    fields = _jpy_fields()
    fields["shipping"] = confirmed(
        ShippingValue(kind=ShippingKind.FIXED, policy_text="3,000원", fee_krw=3000), ".delivery"
    )
    stored = container.revisions.append(collected(fields=fields, images=()))  # type: ignore[arg-type]
    assert stored.facts_status is FactsStatus.REVIEW_REQUIRED
    assert stored.source_currency is None and stored.fingerprints_intact()
    for key in ("prices", "shipping"):
        field = stored.fields[key]
        assert (field.status, field.value) == (FieldStatus.REVIEW_REQUIRED, None)
        assert field.evidence[-1].evidence.locator == CURRENCY_MIXED_LOCATOR
        assert field.evidence[-1].evidence.normalized == "JPY+KRW"


def test_the_store_refuses_values_that_mix_currencies(
    container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Defence in depth: should an evaluation ever hand the store mixed values (JPY prices beside a
    # KRW shipping fee), nothing is written.
    krw = evaluate(collected(images=()))
    jpy = evaluate(collected(fields=_jpy_fields(), images=()))  # type: ignore[arg-type]
    fields = tuple(
        jpy_field if jpy_field.key == "prices" else krw_field
        for krw_field, jpy_field in zip(krw.fields, jpy.fields, strict=True)
    )

    def evaluated(*_args: object, **_kwargs: object) -> EvaluatedFacts:
        return EvaluatedFacts(
            krw.collected, fields, krw.images, krw.source_fingerprint, krw.facts_status, "JPY"
        )

    monkeypatch.setattr(revision_module, "evaluate", evaluated)
    with pytest.raises(InputValidationError) as caught:
        container.revisions.append(collected(images=()))
    assert caught.value.code == "COLLECT_FACTS_CURRENCY_MIXED"
    # And one that names a currency its values do not state.
    monkeypatch.setattr(
        revision_module,
        "evaluate",
        lambda *_a, **_k: EvaluatedFacts(
            krw.collected, krw.fields, krw.images, krw.source_fingerprint, krw.facts_status, "CNY"
        ),
    )
    with pytest.raises(InputValidationError, match="currency the evaluation derived"):
        container.revisions.append(collected(images=()))
    assert container.revisions.history("kmretail", "1234") == ()


def test_0063_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_to_head(url)

    def columns() -> list[str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            return [r[1] for r in connection.execute(f"PRAGMA table_info({TABLE})")]

    def triggers() -> set[str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            return {
                r[0]
                for r in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name = ?", (TABLE,)
                )
            }

    after, guards = columns(), triggers()
    assert after[-1] == "source_currency" and guards
    # Empty, it steps down to exactly what it added, and back up; the append-only triggers stay.
    command.downgrade(alembic_config(url), BEFORE)
    assert columns() == after[:-1] and triggers() == guards
    command.upgrade(alembic_config(url), "head")
    assert columns() == after and triggers() == guards

    at = "'2026-10-14 00:00:00'"

    def insert(revision_id: str, source_currency: str) -> None:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute(
                f"INSERT INTO {TABLE} VALUES ('{revision_id}', 'kmretail', '{revision_id}', 1,"
                f" 'https://shop.example/p', {at}, {at}, 'KRW', 'r1', '{'b' * 64}',"
                f" '{'c' * 64}', 'run-{revision_id}', 'cid', 'CONFIRMED', NULL, NULL, NULL,"
                f" {source_currency})"
            )
            connection.commit()

    insert("krw", "NULL")
    # A KRW revision loses nothing: the step down keeps it.
    command.downgrade(alembic_config(url), BEFORE)
    command.upgrade(alembic_config(url), "head")
    with pytest.raises(sqlite3.IntegrityError, match="source_currency_valid"):
        insert("usd", "'USD'")
    insert("cny", "'CNY'")
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), BEFORE)
    assert columns() == after
    with contextlib.closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            f"SELECT revision_id, source_currency FROM {TABLE} ORDER BY revision_id"
        ).fetchall() == [("cny", "CNY"), ("krw", None)]
