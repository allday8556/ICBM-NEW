"""Proof 6 — ValidationSample capture and replay (ADR-0017 §7.3, V3a, V8)."""

import inspect
import json

import pytest

from prototypes.adaptive_collector.capture import (
    BLOCK_MAX_BYTES,
    CaptureRefused,
    OperatorExclusion,
    OperatorReason,
    OperatorScope,
    ValidationSample,
    capture_sample,
)
from prototypes.adaptive_collector.dom import from_snapshot
from prototypes.adaptive_collector.engine import extract
from prototypes.adaptive_collector.profile import Bundle
from prototypes.adaptive_collector.testsupport import (
    SAMPLE_PAGES,
    expected,
    page,
    sample,
    scope_for,
)
from prototypes.adaptive_collector.validation import (
    SampleRefused,
    Verdict,
    conflict_statements,
    validate,
)


def test_the_capture_takes_no_profile() -> None:
    assert list(inspect.signature(capture_sample).parameters) == ["html", "scope", "expected"]
    provenance = sample("simple_on_sale").provenance
    assert set(provenance) == {"capture_revision", "scope", "excluded", "removals"}
    assert provenance["scope"]["product_boundary"] == "product-detail"
    assert provenance["scope"]["boundary_element"] == "div#prd.product-detail"


# ---------------------------------------------------------------- the operator's product boundary


def test_only_the_approved_product_boundary_is_captured() -> None:
    captured = sample("simple_on_sale")
    snapshot = captured.snapshot
    assert [child["attrs"].get("class") for child in snapshot["children"]] == ["product-detail"]
    text = captured.snapshot_json
    # Everything outside the boundary is simply not in the sample, not even as an exclusion.
    for outside in ("gnb", "trackPage", "페이지 하단 리뷰", "(주)합성"):
        assert outside not in text and outside not in captured.provenance_json, outside


@pytest.mark.parametrize(
    ("boundary", "why"),
    [("", "no product boundary"), ("no-such-region", "0 elements"), ("review-list", "2 elements")],
)
def test_a_missing_or_ambiguous_boundary_refuses_the_sample(boundary: str, why: str) -> None:
    html = page("simple_on_sale")
    scope = OperatorScope("operator", "t", boundary, ())
    with pytest.raises(CaptureRefused, match=why):
        capture_sample(html, scope, expected("simple_on_sale"))


def test_a_boundary_that_is_itself_non_authoritative_is_refused() -> None:
    html = page("simple_on_sale").replace('class="recommend-box"', 'class="recommend-box" id="rec"')
    with pytest.raises(CaptureRefused, match="non-product region"):
        capture_sample(html, OperatorScope("operator", "t", "rec", ()), expected("simple_on_sale"))


def test_regions_inside_the_boundary_are_excluded_by_boundary_and_class_only() -> None:
    captured = sample("simple_on_sale")
    excluded = dict(captured.provenance["excluded"])
    assert excluded == {
        "div#.member-info": "PRIVATE",
        "div#.review-list": "NON_AUTHORITATIVE",
        "div#.recommend-box": "NON_AUTHORITATIVE",
    }
    # The review's price row and sold-out words never become sample evidence or provenance.
    for gone in ("9,000원", "품절될까", "홍길동", "다른 상품 구매"):
        assert gone not in captured.snapshot_json and gone not in captured.provenance_json, gone


def test_an_unconfirmed_detected_region_refuses_the_capture() -> None:
    html = page("simple_on_sale")
    with pytest.raises(CaptureRefused, match="not confirmed"):
        capture_sample(
            html, OperatorScope("operator", "t", "product-detail", ()), expected("simple_on_sale")
        )


def test_operator_exclusions_have_closed_reasons_and_are_recorded() -> None:
    assert {r.value for r in OperatorReason} == {"PRIVACY", "NON_AUTHORITATIVE"}
    html = page("simple_on_sale")
    scope = scope_for(html)
    narrowed = OperatorScope(
        scope.decided_by,
        scope.decided_at,
        scope.product_boundary,
        scope.confirmed_regions,
        (OperatorExclusion("notice", OperatorReason.PRIVACY),),
    )
    captured = capture_sample(html, narrowed, expected("simple_on_sale"))
    assert ["table#.notice", "OPERATOR_PRIVACY"] in captured.provenance["excluded"]


# ---------------------------------------------------------------- what is kept and what is stripped


def test_product_controls_and_admissible_embedded_literals_are_kept() -> None:
    snapshot = sample("simple_on_sale").snapshot_json
    for kept in ('"btn-buy"', '"btn-cart"', '"min":"1"', '"max":"99"', '"@type":"Product"'):
        assert kept in snapshot, kept
    assert '"assignment":"productData"' in snapshot and '"stock":"IN_STOCK"' in snapshot


def test_a_hidden_control_is_kept_and_only_its_secret_value_is_stripped() -> None:
    captured = sample("simple_on_sale")
    inputs = [
        node
        for node in from_snapshot(captured.snapshot).iter()
        if node.tag == "input" and node.attrs.get("type") == "hidden"
    ]
    by_name = {node.attrs["name"]: node.attrs for node in inputs}
    # A non-secret hidden identity declaration stays, value and all.
    assert by_name["product_no"]["value"] == "SYN-1001"
    # A security field is kept as a control, but its value is gone.
    assert "value" not in by_name["csrf_token"]
    assert "csrf-not-a-real-value" not in captured.snapshot_json
    # A user-entered value is gone; the quantity control and its bounds stay.
    quantity = next(
        n for n in from_snapshot(captured.snapshot).iter() if n.attrs.get("name") == "qty"
    )
    assert "value" not in quantity.attrs and quantity.attrs["max"] == "99"


def test_secret_bearing_url_material_inside_embedded_data_is_stripped() -> None:
    captured = sample("simple_on_sale")
    assert "token=abc123" not in captured.snapshot_json
    assert '"image":"https://cdn.example/syn-1001.jpg"' in captured.snapshot_json
    assert ["EMBEDDED_URL_QUERY", "script#."] in captured.provenance["removals"]


def test_event_handler_attributes_never_survive() -> None:
    captured = sample("simple_on_sale")
    assert "onclick" not in captured.snapshot_json and "buyNow" not in captured.snapshot_json
    assert ["EVENT_HANDLER:onclick", "button#.btn-buy"] in captured.provenance["removals"]


def test_secrets_member_data_and_code_are_stripped() -> None:
    captured = sample("simple_on_sale")
    text = captured.snapshot_json
    for gone in (
        "sessvalue-not-a-real-token",
        "sessionToken",
        "memberGrade",
        "token=zzz",
        "sig=abc123",
        "currentViewer",
    ):
        assert gone not in text, gone
    removals = {what for what, _ in captured.provenance["removals"]}
    assert {"SCRIPT_NOT_LITERAL", "INPUT_VALUE", "EMBEDDED_KEY:sessionToken"} <= removals


def test_a_sample_is_immutable_and_content_addressed() -> None:
    first, again = sample("simple_on_sale"), sample("simple_on_sale")
    assert first.digest == again.digest
    view = first.snapshot
    view["children"].clear()
    assert first.snapshot["children"], "a parsed view never mutates the stored sample"
    with pytest.raises(AttributeError):
        first.digest = "x"  # type: ignore[misc]


# ---------------------------------------------------------------- replay: V3a and V8


def test_v3a_flags_statements_the_bundle_neither_reads_nor_disposes(bundle: Bundle) -> None:
    captured = sample("simple_on_sale")
    root = from_snapshot(captured.snapshot)
    got = extract(bundle, root)
    kinds = {kind for kind, node in conflict_statements(root) if node.index not in got.read_nodes}
    assert kinds == {"EMBEDDED_PRODUCT_DATA"}  # the productData assignment nobody reads


def test_a_truncated_sample_makes_the_run_incomplete_and_is_never_inspected(
    bundle: Bundle, negatives: dict[str, str]
) -> None:
    big = json.dumps({"sku": "SYN-1001", "blob": "가" * BLOCK_MAX_BYTES})
    html = page("simple_on_sale").replace(
        '<h1 class="product-title">',
        f'<script>var bigData = {big};</script><h1 class="product-title">',
    )
    truncated = capture_sample(html, scope_for(html), expected("simple_on_sale"))
    assert truncated.truncated
    assert "가가가" not in truncated.snapshot_json and '"truncated":true' in truncated.snapshot_json
    others = [sample(name) for name in SAMPLE_PAGES]
    run = validate(bundle, [*others, truncated], negatives=negatives)
    assert run.verdict is Verdict.INCOMPLETE
    assert run.check("V8").outcome is Verdict.INCOMPLETE
    assert any("SAMPLE_TRUNCATED" in detail for detail in run.check("V8").details)


def test_a_sample_whose_provenance_names_a_profile_is_refused(
    bundle: Bundle, negatives: dict[str, str]
) -> None:
    good = sample("simple_on_sale")
    provenance = good.provenance
    provenance["scope"]["decided_by"] = "profile:candidate-epr"
    forged = ValidationSample(
        good.snapshot_json, good.expected_json, json.dumps(provenance), False, "f" * 64
    )
    with pytest.raises(SampleRefused):
        validate(bundle, [forged, sample("simple_sold_out")], negatives=negatives)
