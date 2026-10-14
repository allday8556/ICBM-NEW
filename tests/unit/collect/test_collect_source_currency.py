"""ADR-0038 O1a: source currency on facts (§1, FX-01), pure: no database.

A KRW value keeps its shape, canonical JSON and every fingerprint. A foreign value states its
currency and an exact decimal amount at that currency's scale, never a float. A revision states
one currency: a page that mixes them is REVIEW_REQUIRED, and nothing mixed is ever derived.
"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.platform.core.errors import InputValidationError
from app.stages.collect.facts import (
    CURRENCY_MIXED_LOCATOR,
    FIELD_REGISTRY,
    FINGERPRINT_SCHEME,
    Availability,
    CollectedFacts,
    EvaluatedFacts,
    EvaluatedField,
    Evidence,
    EvidenceKind,
    FactsStatus,
    FieldFact,
    FieldStatus,
    ForeignMoneyValue,
    ForeignPrice,
    ForeignPricesValue,
    ForeignQuantityTier,
    ForeignQuantityTiersValue,
    ForeignShippingValue,
    ImageReference,
    ImageRole,
    MoneyValue,
    NoticeItem,
    NoticeValue,
    OptionAxis,
    OptionConfiguration,
    OptionsValue,
    PriceRole,
    PricesValue,
    QuantityTier,
    QuantityTiersValue,
    ShippingKind,
    ShippingValue,
    SourcePrice,
    StockValue,
    TextValue,
    evaluate,
    revision_source_currency,
    stated_currencies,
    value_from_json,
)
from tests.support.collect_support import absent, base_fields, collected, confirmed, evidence

CONFIRMED, REVIEW = FieldStatus.CONFIRMED, FieldStatus.REVIEW_REQUIRED
MONEY_FIELDS = ("prices", "shipping", "minimum_sale_price", "quantity_tiers")


def _field(facts: EvaluatedFacts, key: str) -> EvaluatedField:
    return next(field for field in facts.fields if field.key == key)


def cny_price(amount: str = "5.80", role: PriceRole | None = None) -> ForeignPrice:
    return ForeignPrice(label="价格", currency="CNY", amount=amount, role=role)


def foreign_fields(currency: str = "CNY") -> dict[str, FieldFact]:
    """A complete overseas page: every money field in one source currency."""
    amount, fee = ("5.80", "4.50") if currency == "CNY" else ("580", "450")
    fields = base_fields()
    fields["prices"] = confirmed(
        ForeignPricesValue(
            prices=(ForeignPrice(label="price", currency=currency, amount=amount),)  # type: ignore[arg-type]
        ),
        ".price",
    )
    fields["shipping"] = confirmed(
        ForeignShippingValue(
            kind=ShippingKind.FIXED,
            policy_text="freight",
            currency=currency,  # type: ignore[arg-type]
            fee=fee,
        ),
        ".delivery",
    )
    fields["quantity_tiers"] = confirmed(
        ForeignQuantityTiersValue(
            tiers=(
                ForeignQuantityTier(quantity=1, currency=currency, total_price=amount),  # type: ignore[arg-type]
            )
        ),
        ".tiers",
    )
    return fields


# ---------------------------------------------------------------- KRW stays byte-identical


def _rich_krw() -> CollectedFacts:
    """Every KRW money shape, with roles, surcharges and labelled tiers. Its golden digests were
    computed with the facts module of ``91565e8a``, before ADR-0038 O1a."""

    def ok(value: object, locator: str) -> FieldFact:
        return FieldFact(CONFIRMED, value, (evidence(locator),))  # type: ignore[arg-type]

    fields = {
        "original_name": ok(TextValue(text="생들기름 350ml"), ".name"),
        "prices": ok(
            PricesValue(
                prices=(
                    SourcePrice(label="도매가", amount_krw=12900, role=PriceRole.PURCHASE),
                    SourcePrice(label="소비자가", amount_krw=19900, role=PriceRole.LIST),
                    SourcePrice(label="회원가", amount_krw=12000),
                )
            ),
            ".price",
        ),
        "options": ok(
            OptionsValue(
                axes=(OptionAxis(name="용량", values=("350ml", "500ml")),),
                configurations=(
                    OptionConfiguration(
                        selections=("350ml",),
                        supplier_sku_id="S1",
                        additional_price_krw=0,
                        sold_out=False,
                    ),
                    OptionConfiguration(
                        selections=("500ml",), additional_price_krw=-500, sold_out=True
                    ),
                ),
            ),
            ".options",
        ),
        "stock": ok(StockValue(availability=Availability.ON_SALE), "button.buy"),
        "shipping": ok(
            ShippingValue(
                kind=ShippingKind.CONDITIONAL,
                policy_text="3,000원 (50,000원 이상 무료)",
                fee_krw=3000,
                free_over_krw=50000,
            ),
            ".delivery",
        ),
        "minimum_sale_price": ok(MoneyValue(label="최저판매가", amount_krw=15000), ".minimum"),
        "quantity_tiers": ok(
            QuantityTiersValue(
                tiers=(
                    QuantityTier(quantity=1, total_price_krw=12900),
                    QuantityTier(quantity=10, total_price_krw=120000, label="10개"),
                )
            ),
            ".tiers",
        ),
        "brand": ok(TextValue(text="예시 브랜드"), ".brand"),
        "manufacturer": absent(".maker"),
        "origin": ok(TextValue(text="국산"), ".origin"),
        "notice": ok(NoticeValue(items=(NoticeItem(label="용량", text="350ml"),)), ".notice"),
        "detail_description": ok(TextValue(text="상세 설명"), ".detail"),
        "sales_channels": absent(".sales-channels"),
    }
    return CollectedFacts(
        supplier_key="kmretail",
        source_product_id="1234",
        source_url="https://shop.example/products/1234",
        captured_at=datetime(2026, 9, 16, 1, 0, tzinfo=UTC),
        extractor_revision="test-collect-r1",
        extractor_fingerprint="b" * 64,
        collection_run_id="run-golden",
        correlation_id="cid-1",
        fields=fields,
        images=(
            ImageReference(
                role=ImageRole.REPRESENTATIVE,
                ordinal=0,
                host="img.shop.example",
                provenance=".gallery img:nth-of-type(1)",
                status=CONFIRMED,
                sha256="a" * 64,
            ),
        ),
    )


GOLDEN_SOURCE_FINGERPRINT = "894320c9282bf6b182d62048cc51c429992f914ca192f4de9683aac9b744ac71"
GOLDEN_FIELDS = {
    "prices": (
        "4253792a52b7ae59ddc2d89f684b4e8f2c5d2204593506750a81529d8235e6ce",
        '{"prices":[{"amount_krw":12900,"label":"도매가","role":"PURCHASE"},'
        '{"amount_krw":19900,"label":"소비자가","role":"LIST"},'
        '{"amount_krw":12000,"label":"회원가"}]}',
    ),
    "options": (
        "b299823dc50b1c9c3bd273bfd2255bfc457ff3588eb60503fcd7ecadca325b7c",
        '{"axes":[{"name":"용량","values":["350ml","500ml"]}],"configurations":['
        '{"additional_price_krw":0,"selections":["350ml"],"sold_out":false,'
        '"supplier_sku_id":"S1"},{"additional_price_krw":-500,"selections":["500ml"],'
        '"sold_out":true,"supplier_sku_id":null}]}',
    ),
    "shipping": (
        "f768ec2a29053ca7082cb7dd4094c503926bb9bda1922bf35967424833189c80",
        '{"fee_krw":3000,"free_over_krw":50000,"kind":"CONDITIONAL",'
        '"policy_text":"3,000원 (50,000원 이상 무료)"}',
    ),
    "minimum_sale_price": (
        "8f34fe8ee6bb8368d23a24d923a7b2e5dcaa79c585a17bb3865be7f6ffde5bc9",
        '{"amount_krw":15000,"label":"최저판매가"}',
    ),
    "quantity_tiers": (
        "1e924c21d06975e1641099bf91eabb99d1ca4521f5345d84def4874fbe65db99",
        '{"tiers":[{"label":null,"quantity":1,"total_price_krw":12900},'
        '{"label":"10개","quantity":10,"total_price_krw":120000}]}',
    ),
}


def test_every_krw_value_keeps_its_canonical_json_and_fingerprints() -> None:
    assert FINGERPRINT_SCHEME == "icbm-facts/v1"
    facts = evaluate(_rich_krw())
    assert facts.source_fingerprint == GOLDEN_SOURCE_FINGERPRINT
    for key, (fingerprint, value_json) in GOLDEN_FIELDS.items():
        field = _field(facts, key)
        assert (field.fingerprint, field.value_json) == (fingerprint, value_json), key
    assert facts.facts_status is FactsStatus.CONFIRMED and facts.source_currency is None


def test_a_krw_value_reads_back_as_its_krw_shape() -> None:
    facts = evaluate(_rich_krw())
    for key in MONEY_FIELDS:
        value = value_from_json(key, _field(facts, key).value_json)
        assert type(value) is FIELD_REGISTRY[key].value_type, key


# ---------------------------------------------------------------- foreign amounts


@pytest.mark.parametrize(
    ("currency", "amount"),
    [("CNY", "5.80"), ("CNY", "5.8"), ("CNY", "0"), ("CNY", "12"), ("JPY", "92"), ("JPY", "0")],
)
def test_a_foreign_amount_is_exact_decimal_text_at_its_scale(currency: str, amount: str) -> None:
    price = ForeignPrice(label="p", currency=currency, amount=amount)  # type: ignore[arg-type]
    assert price.amount == amount
    assert json.loads(price.model_dump_json()) == {
        "label": "p",
        "currency": currency,
        "amount": amount,
    }


@pytest.mark.parametrize(
    ("currency", "amount"),
    [
        ("JPY", "9.5"),  # yen are whole
        ("CNY", "9.555"),  # the fen is the smallest unit
        ("CNY", 9.5),  # never a float
        ("CNY", 9),  # never a number at all
        ("CNY", "01"),
        ("CNY", ""),
        ("CNY", "1."),
        ("CNY", ".5"),
        ("CNY", "-1"),
        ("CNY", "1e3"),
        ("CNY", " 1"),
        ("CNY", "1\n"),
        ("CNY", "１"),  # a full-width digit
        ("CNY", "٣"),  # an Arabic-Indic digit
        ("USD", "1"),  # not a source currency ICBM knows
        ("KRW", "1"),  # won is the KRW shape's, never a foreign one
    ],
)
def test_a_foreign_amount_outside_its_currency_is_refused(currency: str, amount: object) -> None:
    with pytest.raises(ValidationError):
        ForeignPrice(label="p", currency=currency, amount=amount)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ForeignMoneyValue(label="p", currency=currency, amount=amount)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ForeignQuantityTier(quantity=1, currency=currency, total_price=amount)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ForeignShippingValue(
            kind=ShippingKind.FIXED,
            policy_text="x",
            currency=currency,  # type: ignore[arg-type]
            fee=amount,  # type: ignore[arg-type]
        )


def test_prices_and_tiers_state_one_currency() -> None:
    jpy = ForeignPrice(label="p", currency="JPY", amount="92")
    with pytest.raises(ValidationError, match="one currency"):
        ForeignPricesValue(prices=(cny_price(), jpy))
    with pytest.raises(ValidationError, match="one currency"):
        ForeignQuantityTiersValue(
            tiers=(
                ForeignQuantityTier(quantity=1, currency="CNY", total_price="5.80"),
                ForeignQuantityTier(quantity=90, currency="JPY", total_price="530"),
            )
        )
    with pytest.raises(ValidationError, match="distinct quantities"):
        ForeignQuantityTiersValue(
            tiers=(
                ForeignQuantityTier(quantity=1, currency="CNY", total_price="5.80"),
                ForeignQuantityTier(quantity=1, currency="CNY", total_price="5.30"),
            )
        )


def test_the_foreign_purchase_price_follows_the_krw_rule() -> None:
    # ADR-0032 §3, mirrored: one declared PURCHASE price, else the only price without a role.
    purchase = cny_price("5.30", PriceRole.PURCHASE)
    listed = cny_price("5.80", PriceRole.LIST)
    assert ForeignPricesValue(prices=(listed, purchase, cny_price("5.50"))).purchase() == purchase
    assert ForeignPricesValue(prices=(listed, cny_price("5.50"))).purchase() == cny_price("5.50")
    assert ForeignPricesValue(prices=(listed,)).purchase() is None
    assert ForeignPricesValue(prices=(cny_price("1"), cny_price("2"))).purchase() is None
    assert ForeignPricesValue(prices=(purchase, purchase)).purchase() is None
    # A price without a role serializes without one, like a SourcePrice.
    assert "role" not in json.loads(cny_price().model_dump_json())


def test_foreign_shipping_follows_the_krw_kind_rules() -> None:
    def shipping(kind: ShippingKind, **amounts: str) -> ForeignShippingValue:
        return ForeignShippingValue(kind=kind, policy_text="x", currency="CNY", **amounts)

    assert shipping(ShippingKind.FREE).fee is None
    assert shipping(ShippingKind.FIXED, fee="4.50").fee == "4.50"
    assert shipping(ShippingKind.CONDITIONAL, fee="4.50", free_over="99").free_over == "99"
    for kind, amounts in (
        (ShippingKind.FREE, {"fee": "1"}),
        (ShippingKind.FIXED, {}),
        (ShippingKind.FIXED, {"fee": "1", "free_over": "2"}),
        (ShippingKind.UNKNOWN, {"fee": "1"}),
    ):
        with pytest.raises(ValidationError):
            shipping(kind, **amounts)


def test_a_configuration_states_a_unit_price_with_its_currency_only() -> None:
    priced = OptionConfiguration(selections=("红色",), unit_price="9.50", currency="CNY")
    assert json.loads(priced.model_dump_json())["unit_price"] == "9.50"
    # Absent, neither key is serialized: every existing configuration keeps its canonical JSON.
    plain = OptionConfiguration(selections=("红色",), additional_price_krw=0)
    assert set(json.loads(plain.model_dump_json())) == {
        "selections",
        "supplier_sku_id",
        "additional_price_krw",
        "sold_out",
    }
    with pytest.raises(ValidationError, match="together"):
        OptionConfiguration(selections=("x",), unit_price="9.50")
    with pytest.raises(ValidationError, match="together"):
        OptionConfiguration(selections=("x",), currency="CNY")
    with pytest.raises(ValidationError, match="one currency"):
        OptionConfiguration(
            selections=("x",), unit_price="9.50", currency="CNY", additional_price_krw=0
        )
    with pytest.raises(ValidationError):
        OptionConfiguration(selections=("x",), unit_price="9.5", currency="JPY")
    axis = OptionAxis(name="颜色", values=("红色", "蓝色"))
    with pytest.raises(ValidationError, match="one currency"):
        OptionsValue(
            axes=(axis,),
            configurations=(
                OptionConfiguration(selections=("红色",), unit_price="9.50", currency="CNY"),
                OptionConfiguration(selections=("蓝色",), unit_price="950", currency="JPY"),
            ),
        )


# ---------------------------------------------------------------- the registry


@pytest.mark.parametrize("key", MONEY_FIELDS)
def test_each_money_field_holds_its_krw_shape_or_its_foreign_twin(key: str) -> None:
    spec = FIELD_REGISTRY[key]
    assert spec.foreign_type is not None and spec.value_types == (
        spec.value_type,
        spec.foreign_type,
    )
    facts = evaluate(collected(fields=foreign_fields()))
    if key == "minimum_sale_price":
        value_json = ForeignMoneyValue(label="m", currency="CNY", amount="9").model_dump_json()
    else:
        value_json = _field(facts, key).value_json
    value = value_from_json(key, value_json)
    assert type(value) is spec.foreign_type
    assert value is not None and json.loads(value.model_dump_json()) == json.loads(value_json or "")


def test_no_other_field_has_a_foreign_twin() -> None:
    twins = {key for key, spec in FIELD_REGISTRY.items() if spec.foreign_type is not None}
    assert twins == set(MONEY_FIELDS)


@pytest.mark.parametrize(
    ("key", "value_json"),
    [
        # Both shapes' keys at once: each shape forbids the other's.
        ("prices", '{"prices":[{"label":"p","currency":"CNY","amount":"1","amount_krw":1}]}'),
        ("minimum_sale_price", '{"label":"m","currency":"CNY","amount":"1","amount_krw":1}'),
        ("shipping", '{"kind":"FREE","policy_text":"x","currency":"CNY","fee_krw":null}'),
        ("quantity_tiers", '{"tiers":[{"quantity":1,"currency":"CNY","total_price_krw":1}]}'),
        # A float in a foreign amount, read back from JSON.
        ("prices", '{"prices":[{"label":"p","currency":"CNY","amount":5.8}]}'),
        # One KRW and one foreign price in one value.
        (
            "prices",
            '{"prices":[{"label":"p","currency":"CNY","amount":"1"},{"label":"q","amount_krw":1}]}',
        ),
    ],
)
def test_a_value_mixing_both_shapes_never_reads_back(key: str, value_json: str) -> None:
    with pytest.raises(ValidationError):
        value_from_json(key, value_json)


def test_a_wrong_value_type_names_both_accepted_types() -> None:
    fact = confirmed(TextValue(text="5.80"), ".price")
    with pytest.raises(InputValidationError, match="PricesValue or ForeignPricesValue"):
        evaluate(collected(fields={**base_fields(), "prices": fact}))


def test_an_unknown_foreign_shipping_policy_is_review_required() -> None:
    unknown = ForeignShippingValue(kind=ShippingKind.UNKNOWN, policy_text="x", currency="CNY")
    with pytest.raises(InputValidationError, match="UNKNOWN"):
        evaluate(collected(fields={**foreign_fields(), "shipping": confirmed(unknown, ".d")}))
    review = FieldFact(REVIEW, unknown, (evidence(".d", REVIEW),))
    facts = evaluate(collected(fields={**foreign_fields(), "shipping": review}))
    assert facts.source_currency == "CNY"


# ---------------------------------------------------------------- one currency per revision


@pytest.mark.parametrize("currency", ["CNY", "JPY"])
def test_an_overseas_page_states_its_one_currency(currency: str) -> None:
    facts = evaluate(collected(fields=foreign_fields(currency)))
    assert facts.facts_status is FactsStatus.CONFIRMED
    assert facts.source_currency == currency
    prices = json.loads(_field(facts, "prices").value_json or "")
    assert prices["prices"][0]["currency"] == currency


def test_a_page_without_money_or_in_krw_states_no_source_currency() -> None:
    assert evaluate(collected()).source_currency is None
    no_money = {**base_fields(), "prices": FieldFact(REVIEW, None, (evidence(".p", REVIEW),))}
    no_money["shipping"] = absent(".delivery")
    assert evaluate(collected(fields=no_money)).source_currency is None


def test_options_priced_in_a_source_currency_name_it() -> None:
    fields = foreign_fields()
    fields["options"] = confirmed(
        OptionsValue(
            axes=(OptionAxis(name="颜色", values=("红色",)),),
            configurations=(
                OptionConfiguration(selections=("红色",), unit_price="9.50", currency="CNY"),
            ),
        ),
        ".options",
    )
    facts = evaluate(collected(fields=fields))
    assert facts.facts_status is FactsStatus.CONFIRMED and facts.source_currency == "CNY"


@pytest.mark.parametrize(
    ("override", "lowered", "mixed"),
    [
        # A KRW shipping fee beside CNY prices.
        ("shipping", {"prices", "shipping", "quantity_tiers"}, "CNY+KRW"),
        # A JPY minimum beside CNY prices.
        (
            "minimum_sale_price",
            {"prices", "shipping", "minimum_sale_price", "quantity_tiers"},
            "CNY+JPY",
        ),
        # A KRW option surcharge beside CNY prices.
        ("options", {"prices", "shipping", "options", "quantity_tiers"}, "CNY+KRW"),
    ],
)
def test_a_page_that_mixes_currencies_is_review_required_and_stores_no_amount(
    override: str, lowered: set[str], mixed: str
) -> None:
    fields = foreign_fields()
    fields[override] = {
        "shipping": confirmed(
            ShippingValue(kind=ShippingKind.FIXED, policy_text="3,000원", fee_krw=3000), ".d"
        ),
        "minimum_sale_price": confirmed(
            ForeignMoneyValue(label="m", currency="JPY", amount="900"), ".m"
        ),
        "options": confirmed(
            OptionsValue(
                axes=(OptionAxis(name="颜色", values=("红色",)),),
                configurations=(OptionConfiguration(selections=("红色",), additional_price_krw=0),),
            ),
            ".options",
        ),
    }[override]
    facts = evaluate(collected(fields=fields))
    assert facts.facts_status is FactsStatus.REVIEW_REQUIRED
    assert facts.source_currency is None
    for field in facts.fields:
        if field.key not in lowered:
            assert field.status is not REVIEW, field.key
            continue
        assert (field.status, field.value_json) == (REVIEW, None), field.key
        # The evidence still says what the page stated, and one guard says why.
        supplied = fields[field.key].evidence
        assert tuple(item.evidence for item in field.evidence[:-1]) == supplied
        guard = field.evidence[-1].evidence
        assert guard == Evidence(
            kind=EvidenceKind.DOM_TEXT,
            locator=CURRENCY_MIXED_LOCATOR,
            status=REVIEW,
            normalized=mixed,
        )
    # Deterministic: the same mixed page always evaluates to the same digests.
    assert evaluate(collected(fields=fields)).source_fingerprint == facts.source_fingerprint


def test_values_that_mix_currencies_never_yield_one_currency() -> None:
    cny = ForeignPricesValue(prices=(cny_price(),))
    krw = ShippingValue(kind=ShippingKind.FREE, policy_text="무료")
    assert stated_currencies(cny) == (frozenset({"CNY"}), False)
    # A KRW money shape is KRW whatever its amounts.
    assert stated_currencies(krw) == (frozenset(), True)
    assert stated_currencies(TextValue(text="x")) == (frozenset(), False)
    assert revision_source_currency([cny, None, TextValue(text="x")]) == "CNY"
    assert revision_source_currency([krw, None]) is None
    with pytest.raises(InputValidationError) as caught:
        revision_source_currency([cny, krw])
    assert caught.value.code == "COLLECT_FACTS_CURRENCY_MIXED"
    jpy = ForeignMoneyValue(label="m", currency="JPY", amount="9")
    with pytest.raises(InputValidationError):
        revision_source_currency([cny, jpy])
