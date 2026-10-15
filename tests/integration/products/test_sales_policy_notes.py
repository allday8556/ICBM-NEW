"""ADR-0039 §2, §3 (P2): what a policy-not-applied warning quotes, on the real owners.

A migrated database with the real COLLECT store, Product owner and the application API. Every
product is invented; no supplier, marketplace or AI provider is contacted.

Proven here:
- the note names the Item's supplier by its key and the supplier catalog's display name, and
  carries the supplier's policy as it holds now;
- a CONFIRMED sales-channel statement is quoted by its own ``policy_text``; an unread one by the
  first text the collector observed for it, or by nothing when it observed none;
- a CONFIRMED minimum carries its source label, its currency and its exact amount — KRW, or a
  source currency never converted; an unread one is quoted by its observed text;
- the API reads it for an Item and refuses an unknown Item;
- reading a note writes nothing, and changes no preparation fingerprint: a unit whose suppliers
  state no restriction prepares exactly as before (the golden is the ungated preparation's own).
"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.stages.collect.facts import (
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    ForeignMoneyValue,
    ForeignPrice,
    ForeignPricesValue,
    ForeignShippingValue,
    SalesChannelScope,
    SalesChannelsValue,
    ShippingKind,
)
from app.stages.products.sales_policy import SaveSalesPolicyRequest
from app.stages.products.sales_policy_notes import QUOTE_MAX_CHARS, SalesPolicyNotes, clip
from app.stages.products.source_gates import SourceGates
from app.stages.register.preflight import RegistrationPreflightService
from tests.support.collect_support import confirmed
from tests.support.product_support import (
    SUPPLIER,
    Collections,
    count,
    minimum,
    product,
)
from tests.support.register_support import (
    CID,
    MARKET,
    OPERATOR,
    draft,
    establish,
    preparation,
    prepared,
    ready_final,
    ready_item,
    request,
)

pytestmark = pytest.mark.integration

POLICY_TEXT = "폐쇄몰 전용 상품입니다. 오픈마켓 판매 금지"
OBSERVED = "판매가능 플랫폼: 이미지 참조"
LOCATOR = "th:판매가능플랫폼 + td"


def _channels(text: str = POLICY_TEXT) -> FieldFact:
    return FieldFact(
        FieldStatus.CONFIRMED,
        SalesChannelsValue(scope=SalesChannelScope.CLOSED_MALL_ONLY, policy_text=text),
        (Evidence(EvidenceKind.DOM_TEXT, LOCATOR, FieldStatus.CONFIRMED),),
    )


def _unread(*observed: str | None) -> FieldFact:
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        tuple(
            Evidence(EvidenceKind.DOM_TEXT, LOCATOR, FieldStatus.REVIEW_REQUIRED, observed=text)
            for text in observed
        ),
    )


def _materialized(
    container: Container, config: AppConfig, fields: dict[str, FieldFact], source: str = "1234"
) -> str:
    run_id, _ = Collections.of(container, config).collect(fields, source_product_id=source)
    item_id = container.materializer.materialize_run(run_id).item_id
    assert item_id is not None
    return item_id


def _notes(container: Container) -> SalesPolicyNotes:
    return SalesPolicyNotes(container.product_store, container.supplier_names)


def _waive(container: Container) -> None:
    container.sales_policies.save(
        SUPPLIER,
        SaveSalesPolicyRequest(
            expected_revision_no=None,
            apply_channel_restrictions=False,
            apply_minimum_price=False,
        ),
        actor=OPERATOR,
        correlation_id=CID,
    )


def test_the_note_names_the_supplier_and_quotes_its_channel_words(
    container: Container, config: AppConfig
) -> None:
    item_id = _materialized(
        container, config, product(sales_channels=_channels(), minimum_sale_price=minimum(50000))
    )
    note = _notes(container).note(item_id)
    assert note.item_id == item_id and note.source_revision_id is not None
    assert note.supplier_key == SUPPLIER
    # The supplier catalog's own name for the key.
    assert note.supplier_name == container.supplier_names[SUPPLIER] == "KM통상"
    assert (note.apply_channel_restrictions, note.apply_minimum_price) == (True, True)
    channels = note.sales_channels
    assert channels is not None
    assert (channels.status, channels.scope, channels.quote) == (
        FieldStatus.CONFIRMED,
        SalesChannelScope.CLOSED_MALL_ONLY,
        POLICY_TEXT,
    )
    minimum_note = note.minimum_sale_price
    assert minimum_note is not None
    assert (
        minimum_note.status,
        minimum_note.label,
        minimum_note.currency,
        minimum_note.amount,
        minimum_note.quote,
    ) == (FieldStatus.CONFIRMED, "minimum", "KRW", "50000", None)
    # The policy as it holds now.
    _waive(container)
    waived = _notes(container).note(item_id)
    assert (waived.apply_channel_restrictions, waived.apply_minimum_price) == (False, False)


def test_an_unread_statement_is_quoted_by_what_the_collector_observed(
    container: Container, config: AppConfig
) -> None:
    observed = _materialized(
        container,
        config,
        product(sales_channels=_unread(None, f"  {OBSERVED}\n", "두 번째 문구")),
    )
    channels = _notes(container).note(observed).sales_channels
    assert channels is not None
    # The first non-empty observation, whitespace-normalized; no scope is guessed.
    assert (channels.status, channels.scope, channels.quote) == (
        FieldStatus.REVIEW_REQUIRED,
        None,
        OBSERVED,
    )
    silent = _materialized(container, config, product(sales_channels=_unread(None)), "5678")
    quiet = _notes(container).note(silent).sales_channels
    assert quiet is not None and (quiet.status, quiet.quote) == (FieldStatus.REVIEW_REQUIRED, None)
    unread_minimum = _materialized(
        container,
        config,
        product(
            minimum_sale_price=FieldFact(
                FieldStatus.REVIEW_REQUIRED,
                None,
                (
                    Evidence(
                        EvidenceKind.DOM_TEXT,
                        ".minimum-price",
                        FieldStatus.REVIEW_REQUIRED,
                        observed="권장 소비자가 준수",
                    ),
                ),
            )
        ),
        "9012",
    )
    stated = _notes(container).note(unread_minimum).minimum_sale_price
    assert stated is not None
    assert (stated.status, stated.label, stated.amount, stated.quote) == (
        FieldStatus.REVIEW_REQUIRED,
        None,
        None,
        "권장 소비자가 준수",
    )


def test_a_foreign_minimum_keeps_its_currency_and_exact_amount(
    container: Container, config: AppConfig
) -> None:
    fields = product(
        prices=confirmed(
            ForeignPricesValue(
                prices=(ForeignPrice(label="price", currency="CNY", amount="5.80"),)
            ),
            ".price",
        ),
        shipping=confirmed(
            ForeignShippingValue(kind=ShippingKind.FREE, policy_text="free", currency="CNY"),
            ".delivery",
        ),
        minimum_sale_price=confirmed(
            ForeignMoneyValue(label="最低售价", currency="CNY", amount="12.50"), ".min"
        ),
    )
    stated = _notes(container).note(_materialized(container, config, fields)).minimum_sale_price
    assert stated is not None
    assert (stated.label, stated.currency, stated.amount) == ("最低售价", "CNY", "12.50")


def test_a_statement_the_revision_does_not_hold_is_none(
    container: Container, config: AppConfig
) -> None:
    # The base product states no channel restriction (absent) and no minimum (absent).
    note = _notes(container).note(_materialized(container, config, product()))
    assert note.sales_channels is not None and note.sales_channels.status is FieldStatus.ABSENT
    assert note.sales_channels.quote is None
    assert note.minimum_sale_price is not None
    assert note.minimum_sale_price.status is FieldStatus.ABSENT


def test_a_quote_is_bounded_and_a_cut_is_marked() -> None:
    assert clip("  a \n b  ") == "a b"
    long = "가" * (QUOTE_MAX_CHARS + 10)
    clipped = clip(long)
    assert len(clipped) == QUOTE_MAX_CHARS and clipped.endswith("…")


def test_the_api_reads_the_note_and_refuses_an_unknown_item(
    client: TestClient, config: AppConfig
) -> None:
    served: Container = client.app.state.container  # type: ignore[attr-defined]
    item_id = _materialized(served, config, product(sales_channels=_channels()))
    found = client.get(f"/api/v1/products/items/{item_id}/sales-policy-note")
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["supplier_key"] == SUPPLIER
    assert body["sales_channels"]["quote"] == POLICY_TEXT
    assert body["sales_channels"]["status"] == "CONFIRMED"
    unknown = client.get("/api/v1/products/items/no-such-item/sales-policy-note")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "PRODUCTS_ITEM_UNKNOWN"


def test_reading_a_note_writes_nothing_and_moves_no_fingerprint(
    container: Container, config: AppConfig
) -> None:
    # The warning words are read beside the preparation, never into it: a unit with no warning
    # prepares exactly as the ungated preflight does (the golden), before and after a note read.
    account = establish(container, config, MARKET, "uid-market-a-1")
    item = ready_item(container, Collections.of(container, config), fields=product())
    plain = preparation(container, account)
    gates = SourceGates(container.product_store, lambda key: None)
    gated = replace(
        plain,
        service=RegistrationPreflightService(
            registrations=container.registrations,
            readiness=container.product_readiness,
            pricing=container.pricing,
            images=container.images,
            capability=plain.capability,
            metadata=plain.metadata,
            policies=plain.policies,
            source_gates=gates.read,
        ),
    )
    draft_id = draft(container.registrations, account, [item])
    req = request(container.registrations, draft_id, account, [item])
    req, before = ready_final(gated, req)
    golden = plain.service.candidate(req).candidate_fingerprint
    assert gated.service.candidate(req).candidate_fingerprint == golden
    assert not {"source_gates", "sales_policies"} & set(before.dependencies["candidate"])
    tables = ("supplier_sales_policy_revisions", "audit_events", "registration_preparations")
    counted = {table: count(config, table) for table in tables}
    _notes(container).note(item.item_id)
    assert {table: count(config, table) for table in tables} == counted
    candidate = gated.service.candidate(req)
    after = gated.service.final(req, prepared(candidate))
    assert candidate.candidate_fingerprint == after.candidate_fingerprint == golden
    assert after.dependency_fingerprint == before.dependency_fingerprint
