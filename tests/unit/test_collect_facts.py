"""The COLLECT source-truth model (ADR-0010 §6–§9, Issue #52 §13): pure, no database."""

import itertools
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from app.collect.facts import (
    FIELD_REGISTRY,
    MISSING_REPRESENTATIVE_LOCATOR,
    Availability,
    CollectedFacts,
    EvaluatedFacts,
    EvaluatedField,
    Evidence,
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
from app.collect.urls import UrlPolicy, sanitize_url
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

FACTS_INVALID = "COLLECT_FACTS_INVALID"
URL_UNSAFE = "COLLECT_URL_UNSAFE"
CONFIRMED, ABSENT, REVIEW = (
    FieldStatus.CONFIRMED,
    FieldStatus.ABSENT,
    FieldStatus.REVIEW_REQUIRED,
)


def _field(facts: EvaluatedFacts, key: str) -> EvaluatedField:
    return next(field for field in facts.fields if field.key == key)


def _invalid(collected_facts: object, code: str = FACTS_INVALID) -> InputValidationError:
    with pytest.raises(InputValidationError) as caught:
        evaluate(collected_facts)  # type: ignore[arg-type]
    assert caught.value.code == code
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
    REVIEW,
    None,
    (evidence(".notice img", REVIEW, kind=EvidenceKind.IMAGE, observed=SHA),),
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
                REVIEW,
                StockValue(availability=Availability.REVIEW_REQUIRED),
                (evidence("button.buy", REVIEW),),
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
        status=REVIEW,
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


# ---------------------------------------------------------------- evidence coherence


_NAME = TextValue(text="이름")


@pytest.mark.parametrize(
    ("status", "value", "evidence_statuses", "coherent"),
    [
        (CONFIRMED, _NAME, (CONFIRMED,), True),
        (CONFIRMED, _NAME, (CONFIRMED, ABSENT), True),
        (CONFIRMED, _NAME, (REVIEW,), False),  # backed only by unresolved evidence
        (CONFIRMED, _NAME, (CONFIRMED, REVIEW), False),  # unresolved evidence hidden in part
        (CONFIRMED, _NAME, (ABSENT,), False),  # nothing confirms it
        (ABSENT, None, (ABSENT,), True),
        (ABSENT, None, (ABSENT, ABSENT), True),
        (ABSENT, None, (CONFIRMED,), False),  # contradictory evidence collapsed to ABSENT
        (ABSENT, None, (REVIEW,), False),
        (ABSENT, None, (ABSENT, CONFIRMED), False),
        (REVIEW, _NAME, (REVIEW,), True),
        (REVIEW, None, (CONFIRMED, REVIEW), True),
        (REVIEW, _NAME, (CONFIRMED,), False),  # nothing under review
        (REVIEW, None, (ABSENT,), False),
    ],
)
def test_a_field_status_agrees_with_its_evidence(
    status: FieldStatus,
    value: TextValue | None,
    evidence_statuses: tuple[FieldStatus, ...],
    coherent: bool,
) -> None:
    entries = tuple(
        evidence(f".brand-{index}", entry, observed=None if entry is ABSENT else "브랜드")
        for index, entry in enumerate(evidence_statuses)
    )
    build = with_field("brand", FieldFact(status, value, entries))
    if coherent:
        evaluate(build)
    else:
        _invalid(build)


def test_review_required_evidence_always_reaches_the_revision() -> None:
    # Every field status × every two-entry evidence combination holding REVIEW_REQUIRED: the
    # collection is either refused or its field and revision are REVIEW_REQUIRED.
    accepted = 0
    for key in ("original_name", "brand"):
        for status in FieldStatus:
            for combination in itertools.product(FieldStatus, repeat=2):
                if REVIEW not in combination:
                    continue
                fact = FieldFact(
                    status,
                    None if status is ABSENT else TextValue(text="값"),
                    tuple(evidence(f".e{i}", s, observed=None) for i, s in enumerate(combination)),
                )
                try:
                    facts = evaluate(with_field(key, fact))
                except InputValidationError:
                    continue
                accepted += 1
                assert _field(facts, key).status is FieldStatus.REVIEW_REQUIRED
                assert facts.facts_status is FactsStatus.REVIEW_REQUIRED
    assert accepted > 0


def test_a_missing_representative_image_is_review_required_evidence() -> None:
    detail_only = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=0,
        host="img.shop.example",
        provenance=".detail img:nth-of-type(1)",
        status=CONFIRMED,
        sha256=SHA,
    )
    images = _field(evaluate(collected(images=(detail_only,))), "images")
    assert images.status is FieldStatus.REVIEW_REQUIRED
    marker = [e.evidence for e in images.evidence if e.evidence.status is REVIEW]
    assert [e.locator for e in marker] == [MISSING_REPRESENTATIVE_LOCATOR]


# ---------------------------------------------------------------- URL boundary


def test_url_policy_keeps_only_explicitly_safe_query_keys() -> None:
    assert sanitize_url("https://Shop.Example/p?a=1&token=x#frag") == "https://shop.example/p"
    assert sanitize_url("https://shop.example:443/p") == "https://shop.example/p"
    policy = UrlPolicy({"shop.example": frozenset({"no"})})
    kept = sanitize_url("https://shop.example/p?token=x&no=12&b=2", policy)
    assert kept == "https://shop.example/p?no=12"
    assert sanitize_url(kept, policy) == kept  # a sanitized URL is its own sanitized form
    assert sanitize_url("https://img.shop.example/p?no=1", policy) == "https://img.shop.example/p"
    for refused in (
        "http://shop.example/p",
        "https://user:secret@shop.example/p",
        "https://shop.example:8443/p",
        "https://shop.example/p;jsessionid=abc",
        "https://shop.example/p q",
        "//shop.example/p",
        "ftp://shop.example/p",
    ):
        with pytest.raises(InputValidationError):
            sanitize_url(refused)
    for secret in ("token", "X-Amz-Signature", "sig", "Expires", "auth", "session_id", "key"):
        with pytest.raises(ValueError):
            UrlPolicy({"shop.example": frozenset({secret})})


_SIGNED = "https://img.shop.example/p.png?X-Amz-Signature=SECRET-SIGNATURE"
_TOKEN_URL = "https://shop.example/products/1234?token=SECRET-TOKEN"


def _named(item: Evidence) -> CollectedFacts:
    return with_field("original_name", FieldFact(CONFIRMED, _NAME, (item,)))


_LEAKS: dict[str, Callable[[], CollectedFacts]] = {
    "source-url-query": lambda: collected(source_url=_TOKEN_URL),
    "source-url-fragment": lambda: collected(
        source_url="https://shop.example/products/1234#access_token=SECRET"
    ),
    "image-locator": lambda: collected(images=(replace(REPRESENTATIVE, locator=_SIGNED),)),
    "image-provenance": lambda: collected(
        images=(replace(REPRESENTATIVE, provenance=f"img[src='{_SIGNED}']"),)
    ),
    "url-evidence": lambda: _named(
        Evidence(EvidenceKind.URL, "link[rel=canonical]", CONFIRMED, observed=_TOKEN_URL)
    ),
    "attribute-evidence": lambda: _named(
        Evidence(EvidenceKind.ATTRIBUTE, "img@src", CONFIRMED, observed=_SIGNED)
    ),
    "evidence-locator": lambda: _named(
        Evidence(EvidenceKind.DOM_TEXT, f"a[href='{_TOKEN_URL}']", CONFIRMED, observed="이름")
    ),
    "value-text": lambda: with_field(
        "detail_description", confirmed(TextValue(text=f"자세히 {_SIGNED}"), ".detail")
    ),
    "scheme-less": lambda: with_field(
        "detail_description",
        confirmed(TextValue(text="cdn.shop.example/p.png?Signature=SECRET"), ".detail"),
    ),
    "http-in-text": lambda: with_field(
        "detail_description", confirmed(TextValue(text="http://shop.example/p"), ".detail")
    ),
}


@pytest.mark.parametrize("build", _LEAKS.values(), ids=_LEAKS.keys())
def test_secret_bearing_url_material_never_passes(build: Callable[[], CollectedFacts]) -> None:
    error = _invalid(build(), URL_UNSAFE)
    assert "SECRET" not in error.message  # a refusal never echoes the URL


def test_explicitly_safe_query_keys_survive_under_their_policy() -> None:
    url = "https://shop.example/products?no=1234"
    _invalid(collected(source_url=url), URL_UNSAFE)  # the default keeps no query key
    policy = UrlPolicy({"shop.example": frozenset({"no"})})
    assert evaluate(collected(source_url=url), policy).collected.source_url == url


# ---------------------------------------------------------------- values


@pytest.mark.parametrize("amount", [12900.0, True, -1, "12900"])
def test_money_is_a_non_negative_integer_of_krw(amount: object) -> None:
    with pytest.raises(ValidationError):
        SourcePrice(label="회원가", amount_krw=amount)  # type: ignore[arg-type]


def test_a_minimum_sale_price_is_only_an_observed_value() -> None:
    field = _field(evaluate(collected()), "minimum_sale_price")
    assert (field.status, field.value_json) == (FieldStatus.ABSENT, None)
    derived = MoneyValue(label="최저판매가", amount_krw=10000)
    absent_evidence = (evidence(".minimum-price", ABSENT, observed=None),)
    _invalid(with_field("minimum_sale_price", FieldFact(ABSENT, derived, absent_evidence)))
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
    _invalid(with_field("stock", FieldFact(REVIEW, decided, (evidence("button.soldout", REVIEW),))))
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
    review = FieldFact(REVIEW, unknown, (evidence(".delivery", REVIEW),))
    assert evaluate(with_field("shipping", review)).facts_status is FactsStatus.REVIEW_REQUIRED


def test_a_value_built_without_validation_is_refused() -> None:
    bypass = PricesValue.model_construct(prices=())
    _invalid(with_field("prices", confirmed(bypass, ".price")))
    wrong_type = TextValue(text="12900")
    _invalid(with_field("prices", confirmed(wrong_type, ".price")))


# ---------------------------------------------------------------- evidence and images


def test_every_field_keeps_bounded_evidence() -> None:
    _invalid(with_field("original_name", FieldFact(CONFIRMED, _NAME, ())))
    _invalid(with_field("original_name", FieldFact(CONFIRMED, None, (evidence("x"),))))
    at_bound = FieldFact(CONFIRMED, _NAME, (evidence(".name", observed="가" * 1365),))
    evaluate(with_field("original_name", at_bound))  # 4095 bytes
    over = FieldFact(CONFIRMED, _NAME, (evidence(".name", observed="x" * 4097),))
    _invalid(with_field("original_name", over))


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        (replace(REPRESENTATIVE, sha256=None), FACTS_INVALID),  # CONFIRMED without bytes
        (replace(REPRESENTATIVE, status=REVIEW), FACTS_INVALID),  # review without an issue
        (replace(REPRESENTATIVE, status=ABSENT), FACTS_INVALID),
        (replace(REPRESENTATIVE, sha256="A" * 64), FACTS_INVALID),
        (replace(REPRESENTATIVE, locator="https://other.example/p.png"), FACTS_INVALID),
        (replace(REPRESENTATIVE, host="IMG.shop.example"), FACTS_INVALID),
        (replace(REPRESENTATIVE, provenance=" "), FACTS_INVALID),
        (replace(REPRESENTATIVE, ordinal=-1), FACTS_INVALID),
        (replace(REPRESENTATIVE, locator="https://img.shop.example/p.png#frag"), URL_UNSAFE),
        (replace(REPRESENTATIVE, locator="http://img.shop.example/p.png"), URL_UNSAFE),
    ],
)
def test_image_references_are_checked(reference: ImageReference, code: str) -> None:
    _invalid(collected(images=(reference,)), code)


def test_image_positions_are_distinct_per_role() -> None:
    _invalid(collected(images=(REPRESENTATIVE, replace(REPRESENTATIVE, sha256="c" * 64))))


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"source_product_id": ""}, FACTS_INVALID),
        ({"source_product_id": " 1234"}, FACTS_INVALID),
        ({"captured_at": datetime(2026, 9, 16, 1, 0)}, FACTS_INVALID),
        ({"extractor_fingerprint": "not-a-digest"}, FACTS_INVALID),
        ({"supplier_key": "KM Retail"}, FACTS_INVALID),
        ({"collection_run_id": ""}, FACTS_INVALID),
        ({"source_url": "http://shop.example/products/1234"}, URL_UNSAFE),
        ({"source_url": "https://user:secret@shop.example/products/1234"}, URL_UNSAFE),
        ({"source_url": "https://shop.example/products/1234#top"}, URL_UNSAFE),
    ],
)
def test_revision_provenance_is_checked(overrides: dict[str, object], code: str) -> None:
    _invalid(collected(**overrides), code)
