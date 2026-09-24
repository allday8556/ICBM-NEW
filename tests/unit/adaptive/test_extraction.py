"""Template matching and CORE/COVERAGE extraction (ADR-0017 §8), images without bytes (V2)."""

from typing import Any

import pytest

from app.collect.adaptive.document import read_html
from app.collect.adaptive.engine import (
    Extraction,
    ImageCompleteness,
    TemplateOutcome,
    extract,
    fact_value,
    match_template,
    replay,
)
from app.collect.facts import FIELD_REGISTRY, SUPPLIED_FIELDS, FieldLevel, FieldStatus
from tests.adaptive_support import (
    SAMPLES,
    TEMPLATE_OF,
    bundle_of,
    page,
    sample,
    synmart_bundle,
    template,
)


def _of(name: str) -> Extraction:
    return replay(synmart_bundle(), sample(name).structure)


def _html(html: str) -> Extraction:
    return extract(synmart_bundle(), read_html(html))


# ---------------------------------------------------------------- templates


def test_every_sample_matches_exactly_its_template() -> None:
    for name in SAMPLES:
        outcome, matched = match_template(synmart_bundle(), read_html(page(name)))
        assert outcome is TemplateOutcome.MATCHED
        assert matched is not None and matched.template_key == TEMPLATE_OF[name]


@pytest.mark.parametrize("name", ["login", "listing"])
def test_a_non_product_page_matches_no_template_and_yields_nothing(name: str) -> None:
    got = _html(page(name))
    assert got.outcome is TemplateOutcome.TEMPLATE_UNMATCHED
    assert got.source_product_id is None and not got.fields and not got.images


def test_two_matching_templates_are_ambiguous_never_the_closest() -> None:
    loose = template("loose", choice=False)
    loose["signature"] = {"required": ["h2.goods-name"], "forbidden": []}
    got = extract(bundle_of([template("plain", choice=False), loose]), read_html(page("on_sale")))
    assert got.outcome is TemplateOutcome.TEMPLATE_AMBIGUOUS and not got.fields


# ---------------------------------------------------------------- fields


def test_every_field_equals_the_operator_expectation() -> None:
    for name in SAMPLES:
        captured = sample(name)
        got = replay(synmart_bundle(), captured.structure)
        assert got.source_product_id == captured.expected["source_product_id"]
        for key in SUPPLIED_FIELDS:
            want = captured.expected["fields"][key]
            assert got.fields[key].status.value == want["status"], (name, key)
            assert fact_value(got.fields[key]) == want["value"], (name, key)


def test_absent_is_only_a_coverage_field_whose_absence_condition_is_observed() -> None:
    got = _of("sold_out")
    for key in ("brand", "origin", "quantity_tiers"):
        assert got.fields[key].status is FieldStatus.ABSENT
        assert FIELD_REGISTRY[key].level is FieldLevel.COVERAGE
    for fact in got.fields.values():
        if fact.status is FieldStatus.ABSENT:
            assert all(":absent:" in e.locator for e in fact.evidence)


def test_a_missing_anchor_is_never_absent() -> None:
    document = template("plain", choice=False)
    document["fields"]["brand"]["absent_when"] = "div.brand-box"
    got = extract(bundle_of([document]), read_html(page("sold_out")))
    assert got.fields["brand"].status is FieldStatus.REVIEW_REQUIRED
    assert "MISSING_ANCHOR:brand" in got.signals


def test_a_missing_rule_is_review_never_absent() -> None:
    document = template("plain", choice=False)
    del document["fields"]["brand"]
    got = extract(bundle_of([document]), read_html(page("sold_out")))
    assert got.fields["brand"].status is FieldStatus.REVIEW_REQUIRED
    assert "NO_RULE:brand" in got.signals


def test_two_locators_that_disagree_are_review_required() -> None:
    html = page("on_sale").replace('"name": "합성마트 사과즙 30포"', '"name": "다른 이름"')
    got = _html(html)
    assert got.fields["original_name"].status is FieldStatus.REVIEW_REQUIRED
    assert got.fields["original_name"].value is None and "CONFLICT:original_name" in got.signals


def test_only_a_declared_alternative_hitting_is_signalled() -> None:
    document = template("plain", choice=False)
    document["fields"]["original_name"]["primary"] = {"kind": "TEXT", "locator": "h3.renamed"}
    got = extract(bundle_of([document]), read_html(page("on_sale")))
    assert fact_value(got.fields["original_name"]) == {"text": "합성마트 사과즙 30포"}
    assert "ALTERNATIVE_USED:original_name" in got.signals


def test_a_repeated_label_is_ambiguous_never_merged() -> None:
    html = page("on_sale").replace(
        "<tr><th>정가</th>", "<tr><th>판매가</th><td>18,000원</td></tr><tr><th>정가</th>"
    )
    assert _html(html).fields["prices"].status is FieldStatus.REVIEW_REQUIRED


def test_the_m3_boundary_is_inherited_for_options_and_tiers() -> None:
    got = _of("optioned")
    for key in ("options", "quantity_tiers"):
        assert (
            got.fields[key].status is FieldStatus.REVIEW_REQUIRED and got.fields[key].value is None
        )
        assert f"M3_BOUNDARY:{key}" in got.signals


def test_a_page_proving_no_option_control_has_zero_axes_confirmed() -> None:
    options = _of("on_sale").fields["options"]
    assert options.status is FieldStatus.CONFIRMED
    assert fact_value(options) == {"axes": [], "configurations": []}


def test_a_conditional_shipping_policy_is_never_flattened() -> None:
    shipping = _of("optioned").fields["shipping"]
    assert shipping.status is FieldStatus.REVIEW_REQUIRED
    assert fact_value(shipping)["kind"] == "UNKNOWN" and fact_value(shipping)["fee_krw"] is None


# ---------------------------------------------------------------- stock and control state


def _stock(buttons: str) -> tuple[FieldStatus, Any]:
    html = page("on_sale")
    start = html.index('<div class="buttons">')
    end = html.index("</div>", start) + len("</div>")
    got = _html(html[:start] + f'<div class="buttons">{buttons}</div>' + html[end:])
    return got.fields["stock"].status, fact_value(got.fields["stock"])


SOLD = '<em class="state">품절</em>'


def test_stock_follows_the_control_rules() -> None:
    assert _stock('<button class="btn-basket">담기</button>')[1] == {"availability": "ON_SALE"}
    assert _stock(f'<button class="btn-basket">담기</button>{SOLD}')[1] == {
        "availability": "ON_SALE"
    }
    assert _stock(SOLD)[1] == {"availability": "SOLD_OUT"}
    assert _stock("<span>문의</span>")[0] is FieldStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "hidden",
    [
        'style="visibility: hidden"',
        'style="opacity:0"',
        'style="display:none !important"',
        'aria-hidden="true"',
        "hidden",
        'class="btn-basket invisible"',
    ],
)
def test_a_non_presented_purchase_control_is_never_stock_authority(hidden: str) -> None:
    control = f'<button class="btn-basket" {hidden}>담기</button>'
    assert _stock(control + SOLD) == (FieldStatus.CONFIRMED, {"availability": "SOLD_OUT"})
    assert _stock(control)[0] is FieldStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "disabled",
    [
        '<button class="btn-basket" disabled>담기</button>',
        '<a class="btn-order" aria-disabled="true">바로구매</a>',
        '<fieldset disabled><button class="btn-basket">담기</button></fieldset>',
    ],
)
def test_a_non_operable_purchase_control_is_never_active(disabled: str) -> None:
    assert _stock(disabled)[0] is FieldStatus.REVIEW_REQUIRED
    assert _stock(disabled + SOLD)[1] == {"availability": "SOLD_OUT"}


@pytest.mark.parametrize(
    "hidden_sold",
    ['<em aria-hidden="true">품절</em>', '<em style="visibility:hidden">품절</em>'],
)
def test_hidden_sold_out_text_is_not_a_statement(hidden_sold: str) -> None:
    assert _stock('<button class="btn-basket">담기</button>' + hidden_sold)[1] == {
        "availability": "ON_SALE"
    }
    assert _stock('<button class="btn-basket" disabled>담기</button>' + hidden_sold)[0] is (
        FieldStatus.REVIEW_REQUIRED
    )


# ---------------------------------------------------------------- images (V2, no bytes)


def test_image_references_keep_role_and_ordinal_with_no_fetch() -> None:
    for name in SAMPLES:
        captured = sample(name)
        got = replay(synmart_bundle(), captured.structure)
        assert [[i.role, i.ordinal, i.reference] for i in got.images] == captured.expected["images"]
        assert got.image_completeness is ImageCompleteness.COMPLETE


def test_images_is_core_but_no_parser_field_produces_it() -> None:
    assert "images" not in _of("on_sale").fields


def test_a_missing_representative_is_never_complete() -> None:
    got = _html(page("sold_out").replace('class="cover"', 'class="other"'))
    assert got.image_completeness is ImageCompleteness.REVIEW_REQUIRED
    assert "MISSING_ANCHOR:images:cover" in got.signals


def test_identity_needs_every_source_to_agree() -> None:
    assert _of("on_sale").source_product_id == "SM-5001"
    contradicted = page("on_sale").replace('"sku": "SM-5001", "name"', '"sku": "SM-9999", "name"')
    assert _html(contradicted).identity_reason == "IDENTITY_CONTRADICTORY"
    missing = page("on_sale").replace('<meta name="goods-code" content="SM-5001">', "")
    got = _html(missing)
    assert got.source_product_id is None and got.identity_reason == "IDENTITY_MISSING"


# ---------------------------------------------------------------- every declared location decides

MISSING = {"kind": "TEXT", "locator": "h3.absent"}
HEADING = {"kind": "TEXT", "locator": "section.goods-view h2.goods-name"}
LD_NAME = {"kind": "EMBEDDED", "source": "JSON_LD", "json_ld_type": "Product", "path": ["name"]}
LD_SKU = {"kind": "EMBEDDED", "source": "JSON_LD", "json_ld_type": "Product", "path": ["sku"]}


def _name_rule(primary: dict[str, Any], alternatives: list[dict[str, Any]]) -> Extraction:
    document = template("plain", choice=False)
    document["fields"]["original_name"] = {"primary": primary, "alternatives": alternatives}
    return extract(bundle_of([document]), read_html(page("on_sale")))


def test_primary_missing_and_two_disagreeing_alternatives_is_review() -> None:
    got = _name_rule(MISSING, [LD_NAME, LD_SKU])
    assert got.fields["original_name"].status is FieldStatus.REVIEW_REQUIRED
    assert {"ALTERNATIVE_USED:original_name", "CONFLICT:original_name"} <= set(got.signals)


def test_primary_missing_and_agreeing_alternatives_is_confirmed_and_signalled() -> None:
    got = _name_rule(MISSING, [LD_NAME, HEADING])
    assert fact_value(got.fields["original_name"]) == {"text": "합성마트 사과즙 30포"}
    assert "ALTERNATIVE_USED:original_name" in got.signals
    assert len(got.fields["original_name"].evidence) == 2


def test_cardinality_one_counts_hits_not_distinct_texts() -> None:
    heading = '<h2 class="goods-name">합성마트 사과즙 30포</h2>'
    got = _html(page("on_sale").replace(heading, heading + heading))
    assert got.fields["original_name"].status is FieldStatus.REVIEW_REQUIRED
    assert "CARDINALITY:original_name" in got.signals


def _price_alternative(extra_table: str) -> Extraction:
    document = template("plain", choice=False)
    document["fields"]["prices"]["alternatives"] = [
        {
            "kind": "LABELLED_ROW",
            "container": "section.goods-view table.summary",
            "vocabulary": "price_labels",
        }
    ]
    html = page("on_sale").replace(
        '<div class="choice"></div>', f'{extra_table}<div class="choice"></div>'
    )
    return extract(bundle_of([document]), read_html(html))


def test_a_row_field_alternative_that_disagrees_is_review() -> None:
    got = _price_alternative(
        '<table class="summary"><tr><th>판매가</th><td>18,000원</td></tr></table>'
    )
    assert got.fields["prices"].status is FieldStatus.REVIEW_REQUIRED
    assert "CONFLICT:prices" in got.signals


def test_a_row_field_alternative_that_agrees_is_confirmed() -> None:
    got = _price_alternative(
        '<table class="summary"><tr><th>판매가</th><td>19,800원</td></tr>'
        "<tr><th>정가</th><td>24,000원</td></tr></table>"
    )
    assert got.fields["prices"].status is FieldStatus.CONFIRMED


def test_a_row_field_is_read_from_an_alternative_when_the_primary_is_missing() -> None:
    document = template("plain", choice=False)
    document["fields"]["prices"] = {
        "primary": {
            "kind": "LABELLED_ROW",
            "container": "table.gone",
            "vocabulary": "price_labels",
        },
        "alternatives": [
            {"kind": "LABELLED_ROW", "container": "table.spec", "vocabulary": "price_labels"}
        ],
        "cardinality": "MANY",
    }
    got = extract(bundle_of([document]), read_html(page("on_sale")))
    assert got.fields["prices"].status is FieldStatus.CONFIRMED
    assert "ALTERNATIVE_USED:prices" in got.signals


def test_a_quantity_tier_alternative_hit_is_review_never_absent() -> None:
    document = template("plain", choice=False)
    document["fields"]["quantity_tiers"]["alternatives"] = [
        {
            "kind": "LABELLED_ROW",
            "container": "section.goods-view table.promo",
            "vocabulary": "tier_labels",
        }
    ]
    promo = '<table class="promo"><tr><th>수량별 할인</th><td>3포 55,000원</td></tr></table>'
    html = page("on_sale").replace(
        '<div class="choice"></div>', promo + '<div class="choice"></div>'
    )
    got = extract(bundle_of([document]), read_html(html))
    assert got.fields["quantity_tiers"].status is FieldStatus.REVIEW_REQUIRED
    assert {"ALTERNATIVE_USED:quantity_tiers", "M3_BOUNDARY:quantity_tiers"} <= set(got.signals)
