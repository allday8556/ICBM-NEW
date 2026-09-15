"""The COLLECT source-truth model (ADR-0010 §6–§9, Issue #52 §13): pure, no database."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from app.collect.facts import (
    FIELD_REGISTRY,
    Availability,
    EvaluatedFacts,
    EvaluatedField,
    EvidenceKind,
    FactsStatus,
    FieldFact,
    FieldLevel,
    FieldStatus,
    ImageIssue,
    ImageReference,
    ImageRole,
    MoneyValue,
    OptionAxis,
    OptionConfiguration,
    OptionsValue,
    PricesValue,
    QuantityTier,
    QuantityTiersValue,
    ShippingKind,
    ShippingValue,
    SourcePrice,
    StockValue,
    TextValue,
    atomic_configuration_key,
    evaluate,
)
from app.core.errors import InputValidationError
from tests.collect_support import (
    CAPTURED,
    REPRESENTATIVE,
    SHA,
    absent,
    base_fields,
    collected,
    confirmed,
    evidence,
    with_field,
)


def _field(facts: EvaluatedFacts, key: str) -> EvaluatedField:
    return next(field for field in facts.fields if field.key == key)


def _invalid(collected_facts: object) -> InputValidationError:
    with pytest.raises(InputValidationError) as caught:
        evaluate(collected_facts)  # type: ignore[arg-type]
    assert caught.value.code == "COLLECT_FACTS_INVALID"
    return caught.value


# ---------------------------------------------------------------- registry and completeness


def test_registry_splits_core_and_source_coverage_facts() -> None:
    # ADR-0010 §7: identity and URL are revision columns; these are the fields.
    core = {key for key, spec in FIELD_REGISTRY.items() if spec.level is FieldLevel.CORE}
    assert core == {"original_name", "prices", "options", "images", "stock"}
    assert set(FIELD_REGISTRY) - core == {
        "shipping",
        "minimum_sale_price",
        "quantity_tiers",
        "brand",
        "manufacturer",
        "origin",
        "notice",
        "detail_description",
    }


def test_a_complete_collection_is_confirmed_in_registry_order() -> None:
    facts = evaluate(collected())
    assert facts.facts_status is FactsStatus.CONFIRMED
    assert [field.key for field in facts.fields] == list(FIELD_REGISTRY)
    images = _field(facts, "images")
    assert images.status is FieldStatus.CONFIRMED
    assert json.loads(images.value_json or "") == {
        "references": [
            {"role": "REPRESENTATIVE", "ordinal": 0, "sha256": SHA, "status": "CONFIRMED"}
        ]
    }


def test_every_supplied_field_reports_a_status() -> None:
    fields = base_fields()
    del fields["brand"]
    assert "missing" in str(_invalid(collected(fields=fields)).message)
    _invalid(collected(fields={**base_fields(), "discount": absent(".discount")}))
    # images is derived from the references and can never be supplied.
    _invalid(collected(fields={**base_fields(), "images": absent(".gallery")}))


# ---------------------------------------------------------------- fingerprints


def test_an_unchanged_source_has_equal_fingerprints_whatever_the_run() -> None:
    first = evaluate(collected())
    later = evaluate(
        collected(
            captured_at=CAPTURED + timedelta(days=1),
            collection_run_id="run-2",
            correlation_id="cid-2",
            images=(
                replace(
                    REPRESENTATIVE,
                    locator="https://img.shop.example/p/1234.png",
                    etag='"v1"',
                    last_modified="Wed, 16 Sep 2026 00:00:00 GMT",
                ),
            ),
        )
    )
    assert later.source_fingerprint == first.source_fingerprint
    assert [f.fingerprint for f in later.fields] == [f.fingerprint for f in first.fields]


def test_one_changed_fact_changes_its_field_and_the_source_fingerprint() -> None:
    before = evaluate(collected())
    after = evaluate(
        with_field(
            "prices",
            confirmed(
                PricesValue(prices=(SourcePrice(label="회원가", amount_krw=13900),)), ".price"
            ),
        )
    )
    changed = {b.key for b, a in zip(before.fields, after.fields, strict=True) if b != a}
    assert changed == {"prices"}
    assert after.source_fingerprint != before.source_fingerprint


def test_changed_image_bytes_change_the_images_and_source_fingerprints() -> None:
    before = evaluate(collected())
    after = evaluate(collected(images=(replace(REPRESENTATIVE, sha256="c" * 64),)))
    changed = {b.key for b, a in zip(before.fields, after.fields, strict=True) if b != a}
    assert changed == {"images"}
    assert after.source_fingerprint != before.source_fingerprint


# ---------------------------------------------------------------- facts status (ruling on Q3)

_IMAGE_ONLY_NOTICE = FieldFact(
    FieldStatus.REVIEW_REQUIRED,
    None,
    (evidence(".notice img", FieldStatus.REVIEW_REQUIRED, kind=EvidenceKind.IMAGE, observed=SHA),),
)


@pytest.mark.parametrize(
    ("key", "fact", "expected"),
    [
        ("brand", absent(".brand"), FactsStatus.CONFIRMED),  # coverage ABSENT is legitimate
        ("notice", _IMAGE_ONLY_NOTICE, FactsStatus.REVIEW_REQUIRED),  # image-only, no OCR
        ("original_name", absent(".name"), FactsStatus.REVIEW_REQUIRED),  # core ABSENT
        (
            "stock",
            FieldFact(
                FieldStatus.REVIEW_REQUIRED,
                StockValue(availability=Availability.REVIEW_REQUIRED),
                (evidence("button.buy", FieldStatus.REVIEW_REQUIRED),),
            ),
            FactsStatus.REVIEW_REQUIRED,
        ),
    ],
    ids=["coverage-absent", "image-only-notice", "core-absent", "mixed-stock"],
)
def test_facts_status_follows_the_q3_ruling(
    key: str, fact: FieldFact, expected: FactsStatus
) -> None:
    assert evaluate(with_field(key, fact)).facts_status is expected


def test_images_are_derived_from_the_references() -> None:
    detail_under_review = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=0,
        host="img.shop.example",
        provenance=".detail img:nth-of-type(1)",
        status=FieldStatus.REVIEW_REQUIRED,
        issue=ImageIssue.OVERSIZE,
    )
    facts = evaluate(collected(images=(detail_under_review, REPRESENTATIVE)))
    assert _field(facts, "images").status is FieldStatus.REVIEW_REQUIRED
    assert [ref.role for ref in facts.images] == [ImageRole.REPRESENTATIVE, ImageRole.DETAIL]
    no_images = evaluate(collected(images=()))
    assert (_field(no_images, "images").status, no_images.facts_status) == (
        FieldStatus.ABSENT,
        FactsStatus.REVIEW_REQUIRED,
    )


# ---------------------------------------------------------------- values


@pytest.mark.parametrize("amount", [12900.0, True, -1, "12900"])
def test_money_is_a_non_negative_integer_of_krw(amount: object) -> None:
    with pytest.raises(ValidationError):
        SourcePrice(label="회원가", amount_krw=amount)  # type: ignore[arg-type]


def test_a_minimum_sale_price_is_only_an_observed_value() -> None:
    field = _field(evaluate(collected()), "minimum_sale_price")
    assert (field.status, field.value_json) == (FieldStatus.ABSENT, None)
    derived = MoneyValue(label="최저판매가", amount_krw=10000)
    _invalid(
        with_field("minimum_sale_price", FieldFact(FieldStatus.ABSENT, derived, (evidence("x"),)))
    )
    observed = _field(
        evaluate(with_field("minimum_sale_price", confirmed(derived, ".minimum-price"))),
        "minimum_sale_price",
    )
    assert json.loads(observed.value_json or "") == {"label": "최저판매가", "amount_krw": 10000}


def test_quantity_tiers_keep_their_original_totals() -> None:
    tiers = QuantityTiersValue(
        tiers=(
            QuantityTier(quantity=1, total_price_krw=12900),
            QuantityTier(quantity=3, total_price_krw=36000, label="3개 묶음"),
        )
    )
    field = _field(
        evaluate(with_field("quantity_tiers", confirmed(tiers, ".tiers"))), "quantity_tiers"
    )
    assert json.loads(field.value_json or "") == {
        "tiers": [
            {"quantity": 1, "total_price_krw": 12900, "label": None},
            {"quantity": 3, "total_price_krw": 36000, "label": "3개 묶음"},
        ]
    }
    with pytest.raises(ValidationError):
        QuantityTiersValue(
            tiers=(
                QuantityTier(quantity=2, total_price_krw=1),
                QuantityTier(quantity=2, total_price_krw=2),
            )
        )


def test_atomic_options_keep_count_grade_and_weight_distinct() -> None:
    axes = (
        OptionAxis(name="구성", values=("1kg x 2", "2kg x 1")),
        OptionAxis(name="등급", values=("특", "상")),
    )
    configurations = tuple(
        OptionConfiguration(selections=(composition, grade))
        for composition in axes[0].values
        for grade in axes[1].values
    )
    options = OptionsValue(axes=axes, configurations=configurations)
    assert all(c.supplier_sku_id is None for c in options.configurations)  # never fabricated
    keys = {atomic_configuration_key("1234", c.selections) for c in configurations}
    assert len(keys) == 4  # same total weight, still four atomic configurations
    assert atomic_configuration_key("1234", ("1kg x 2", "특")) != atomic_configuration_key(
        "5678", ("1kg x 2", "특")
    )
    assert atomic_configuration_key("1234", ("a", "b")) != atomic_configuration_key(
        "1234", ("b", "a")
    )
    for broken in (
        (OptionConfiguration(selections=("3kg", "특")),),  # not a listed value
        (OptionConfiguration(selections=("1kg x 2",)),),  # one selection per axis
        (configurations[0], configurations[0]),  # listed once
    ):
        with pytest.raises(ValidationError):
            OptionsValue(axes=axes, configurations=broken)
    with pytest.raises(ValidationError):
        OptionsValue(axes=(), configurations=(OptionConfiguration(selections=()),))


def test_stock_status_matches_the_availability() -> None:
    undecided = StockValue(availability=Availability.REVIEW_REQUIRED)
    _invalid(with_field("stock", confirmed(undecided, "button.buy")))
    decided = StockValue(availability=Availability.SOLD_OUT)
    _invalid(
        with_field(
            "stock",
            FieldFact(FieldStatus.REVIEW_REQUIRED, decided, (evidence("button.soldout"),)),
        )
    )
    assert evaluate(with_field("stock", confirmed(decided, "button.soldout"))).facts_status is (
        FactsStatus.CONFIRMED
    )


def test_shipping_is_never_flattened_into_a_guessed_fee() -> None:
    with pytest.raises(ValidationError):
        ShippingValue(kind=ShippingKind.FIXED, policy_text="배송비 착불")
    with pytest.raises(ValidationError):
        ShippingValue(kind=ShippingKind.UNKNOWN, policy_text="지역별 상이", fee_krw=3000)
    unknown = ShippingValue(kind=ShippingKind.UNKNOWN, policy_text="지역별 상이")
    _invalid(with_field("shipping", confirmed(unknown, ".delivery")))
    review = FieldFact(FieldStatus.REVIEW_REQUIRED, unknown, (evidence(".delivery"),))
    assert evaluate(with_field("shipping", review)).facts_status is FactsStatus.REVIEW_REQUIRED


def test_a_value_built_without_validation_is_refused() -> None:
    bypass = PricesValue.model_construct(prices=())
    _invalid(with_field("prices", confirmed(bypass, ".price")))
    wrong_type = TextValue(text="12900")
    _invalid(with_field("prices", confirmed(wrong_type, ".price")))


# ---------------------------------------------------------------- evidence and images


def test_every_field_keeps_bounded_evidence() -> None:
    name = TextValue(text="이름")
    _invalid(with_field("original_name", FieldFact(FieldStatus.CONFIRMED, name, ())))
    _invalid(with_field("original_name", FieldFact(FieldStatus.CONFIRMED, None, (evidence("x"),))))
    at_bound = FieldFact(FieldStatus.CONFIRMED, name, (evidence(".name", observed="가" * 1365),))
    evaluate(with_field("original_name", at_bound))  # 4095 bytes
    over = FieldFact(FieldStatus.CONFIRMED, name, (evidence(".name", observed="x" * 4097),))
    _invalid(with_field("original_name", over))


@pytest.mark.parametrize(
    "reference",
    [
        replace(REPRESENTATIVE, sha256=None),  # CONFIRMED without observed bytes
        replace(REPRESENTATIVE, status=FieldStatus.REVIEW_REQUIRED),  # review without an issue
        replace(REPRESENTATIVE, status=FieldStatus.ABSENT),
        replace(REPRESENTATIVE, sha256="A" * 64),
        replace(REPRESENTATIVE, locator="https://other.example/p.png"),  # off its host
        replace(REPRESENTATIVE, locator="https://img.shop.example/p.png#frag"),
        replace(REPRESENTATIVE, locator="http://img.shop.example/p.png"),
        replace(REPRESENTATIVE, host="IMG.shop.example"),
        replace(REPRESENTATIVE, provenance=" "),
        replace(REPRESENTATIVE, ordinal=-1),
    ],
)
def test_image_references_are_checked(reference: ImageReference) -> None:
    _invalid(collected(images=(reference,)))


def test_image_positions_are_distinct_per_role() -> None:
    _invalid(collected(images=(REPRESENTATIVE, replace(REPRESENTATIVE, sha256="c" * 64))))


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_product_id": ""},
        {"source_product_id": " 1234"},
        {"source_url": "http://shop.example/products/1234"},
        {"source_url": "https://user:secret@shop.example/products/1234"},
        {"source_url": "https://shop.example/products/1234#top"},
        {"captured_at": datetime(2026, 9, 16, 1, 0)},
        {"extractor_fingerprint": "not-a-digest"},
        {"supplier_key": "KM Retail"},
        {"collection_run_id": ""},
    ],
)
def test_revision_provenance_is_checked(overrides: dict[str, object]) -> None:
    _invalid(collected(**overrides))
