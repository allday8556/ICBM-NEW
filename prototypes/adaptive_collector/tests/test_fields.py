"""Proof 3 — deterministic CORE/COVERAGE extraction, conflicts and ABSENT rules (ADR-0017 §8.2)."""

from typing import Any

from app.collect.facts import FIELD_REGISTRY, SUPPLIED_FIELDS, FieldLevel, FieldStatus
from prototypes.adaptive_collector.dom import from_snapshot, parse_html
from prototypes.adaptive_collector.engine import extract, value_json
from prototypes.adaptive_collector.fixtures import profiles
from prototypes.adaptive_collector.profile import Bundle, ProfileStore
from prototypes.adaptive_collector.tests.conftest import SAMPLE_PAGES, page, sample


def _extract(bundle: Bundle, name: str) -> Any:
    return extract(bundle, from_snapshot(sample(name).snapshot))


def test_extraction_equals_the_operator_expectation_for_every_field(bundle: Bundle) -> None:
    for name in SAMPLE_PAGES:
        captured = sample(name)
        got = extract(bundle, from_snapshot(captured.snapshot))
        assert got.identity == captured.expected["identity"]
        for key in SUPPLIED_FIELDS:
            want = captured.expected["fields"][key]
            assert got.fields[key].status.value == want["status"], (name, key)
            assert value_json(got.fields[key]) == want["value"], (name, key)


def test_absent_is_only_for_coverage_fields_whose_absence_condition_is_observed(
    bundle: Bundle,
) -> None:
    got = _extract(bundle, "simple_sold_out")
    for key in ("brand", "origin", "quantity_tiers"):
        assert got.fields[key].status is FieldStatus.ABSENT
        assert FIELD_REGISTRY[key].level is FieldLevel.COVERAGE
    for fact in got.fields.values():
        for evidence in fact.evidence:
            if evidence.status is FieldStatus.ABSENT:
                assert ":absent:" in evidence.locator


def test_a_missing_anchor_is_never_absent(store: ProfileStore) -> None:
    template = profiles.ptr("simple", optioned=False)
    template["fields"]["brand"]["absent_when_present"] = "div.brand-box"  # not on the page
    bundle = store.bundle(store.put(profiles.epr([store.put(template)])))
    got = extract(bundle, from_snapshot(sample("simple_sold_out").snapshot))
    assert got.fields["brand"].status is FieldStatus.REVIEW_REQUIRED
    assert "MISSING_ANCHOR:brand" in got.signals


def test_two_locators_that_disagree_are_review_required(bundle: Bundle) -> None:
    html = page("simple_on_sale").replace('"name": "합성 테스트 상품 A"', '"name": "다른 이름"')
    got = extract(bundle, parse_html(html))
    assert got.fields["original_name"].status is FieldStatus.REVIEW_REQUIRED
    assert got.fields["original_name"].value is None
    assert "CONFLICT:original_name" in got.signals


def test_only_a_declared_alternative_hitting_is_recorded(store: ProfileStore) -> None:
    template = profiles.ptr("simple", optioned=False)
    template["fields"]["original_name"]["primary"] = {"kind": "TEXT", "selector": "h2.renamed"}
    bundle = store.bundle(store.put(profiles.epr([store.put(template)])))
    got = extract(bundle, from_snapshot(sample("simple_on_sale").snapshot))
    assert value_json(got.fields["original_name"]) == {"text": "합성 테스트 상품 A"}
    assert "ALTERNATIVE_USED:original_name" in got.signals


def test_a_repeated_label_is_ambiguous_never_merged(bundle: Bundle) -> None:
    html = page("simple_on_sale").replace(
        "<tr><th>소비자가</th>", "<tr><th>판매가</th><td>11,000원</td></tr><tr><th>소비자가</th>"
    )
    got = extract(bundle, parse_html(html))
    assert got.fields["prices"].status is FieldStatus.REVIEW_REQUIRED


def test_the_m3_boundary_is_inherited_for_options_and_tiers(bundle: Bundle) -> None:
    got = _extract(bundle, "optioned")
    for key in ("options", "quantity_tiers"):
        assert got.fields[key].status is FieldStatus.REVIEW_REQUIRED
        assert got.fields[key].value is None
        assert f"M3_BOUNDARY:{key}" in got.signals


def test_a_conditional_shipping_policy_is_never_flattened(bundle: Bundle) -> None:
    got = _extract(bundle, "optioned")
    assert got.fields["shipping"].status is FieldStatus.REVIEW_REQUIRED
    assert value_json(got.fields["shipping"])["kind"] == "UNKNOWN"
    assert value_json(got.fields["shipping"])["fee_krw"] is None


def test_stock_follows_the_control_rules(bundle: Bundle) -> None:
    assert value_json(_extract(bundle, "simple_on_sale").fields["stock"]) == {
        "availability": "ON_SALE"
    }
    assert value_json(_extract(bundle, "simple_sold_out").fields["stock"]) == {
        "availability": "SOLD_OUT"
    }
    # An active purchase control beats visible sold-out text; hidden text is not a statement.
    html = page("simple_on_sale").replace(
        '<button class="btn-cart">',
        '<span class="soldout-badge">품절</span><button class="btn-cart">',
    )
    assert value_json(extract(bundle, parse_html(html)).fields["stock"]) == {
        "availability": "ON_SALE"
    }
    neither = page("simple_on_sale").replace("구매하기", "").replace('class="btn-buy"', 'class="x"')
    neither = neither.replace('class="btn-cart"', 'class="y"')
    got = extract(bundle, parse_html(neither))
    assert got.fields["stock"].status is FieldStatus.REVIEW_REQUIRED


def test_the_page_proving_no_option_control_has_zero_axes_confirmed(bundle: Bundle) -> None:
    got = _extract(bundle, "simple_on_sale")
    assert got.fields["options"].status is FieldStatus.CONFIRMED
    assert value_json(got.fields["options"]) == {"axes": [], "configurations": []}
