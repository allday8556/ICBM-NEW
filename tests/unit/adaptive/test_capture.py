"""The independent ValidationSample capture owner (ADR-0017 §7.3)."""

import inspect

import pytest

from app.collect.adaptive.capture import (
    BLOCK_MAX_BYTES,
    CaptureRefused,
    OperatorExclusion,
    OperatorReason,
    OperatorScope,
    capture_sample,
    final_scan,
)
from app.collect.adaptive.document import from_structure
from tests.adaptive_support import SAMPLES, expected, page, sample, scope_for


def _inject(extra: str) -> str:
    return page("on_sale").replace('<h2 class="goods-name">', f'{extra}<h2 class="goods-name">')


def _capture(html: str) -> object:
    return capture_sample(html, scope_for(html), expected("on_sale"))


def test_the_capture_takes_no_profile_and_records_the_scope() -> None:
    assert list(inspect.signature(capture_sample).parameters) == ["html", "scope", "expected"]
    provenance = sample("on_sale").provenance
    assert set(provenance) == {"capture_revision", "scope", "excluded", "removals"}
    assert provenance["scope"]["product_boundary"] == "goods-view"
    assert provenance["scope"]["boundary_element"] == "section#goods.goods-view"


def test_only_the_approved_boundary_is_captured() -> None:
    captured = sample("on_sale")
    assert [c["attrs"]["class"] for c in captured.structure["children"]] == ["goods-view"]
    for outside in ("top-bar", "analytics", "하단 리뷰", "합성마트</"):
        assert outside not in captured.structure_json + captured.provenance_json


@pytest.mark.parametrize(
    ("boundary", "why"),
    [("", "no product boundary"), ("nothing-here", "0 elements"), ("review-area", "2 elements")],
)
def test_a_missing_or_ambiguous_boundary_refuses(boundary: str, why: str) -> None:
    with pytest.raises(CaptureRefused, match=why):
        capture_sample(page("on_sale"), OperatorScope("op", "t", boundary, ()), expected("on_sale"))


def test_a_non_authoritative_boundary_refuses() -> None:
    html = page("on_sale").replace('class="related-goods"', 'class="related-goods" id="rel"')
    with pytest.raises(CaptureRefused, match="non-product region"):
        capture_sample(html, OperatorScope("op", "t", "rel", ()), expected("on_sale"))


def test_regions_inside_the_boundary_are_excluded_by_boundary_and_class_only() -> None:
    captured = sample("on_sale")
    assert dict(captured.provenance["excluded"]) == {
        "div#.member-box": "PRIVATE",
        "div#.review-area": "NON_AUTHORITATIVE",
        "div#.related-goods": "NON_AUTHORITATIVE",
    }
    for gone in ("15,000원", "쟁여요", "합성회원", "연관상품"):
        assert gone not in captured.structure_json + captured.provenance_json


def test_an_unconfirmed_region_refuses() -> None:
    with pytest.raises(CaptureRefused, match="not confirmed"):
        capture_sample(
            page("on_sale"), OperatorScope("op", "t", "goods-view", ()), expected("on_sale")
        )


def test_operator_exclusions_are_closed_and_recorded() -> None:
    assert {r.value for r in OperatorReason} == {"PRIVACY", "NON_AUTHORITATIVE"}
    html = page("on_sale")
    scope = scope_for(html)
    narrowed = OperatorScope(
        scope.decided_by,
        scope.decided_at,
        scope.product_boundary,
        scope.confirmed_regions,
        (OperatorExclusion("legal", OperatorReason.PRIVACY),),
    )
    captured = capture_sample(html, narrowed, expected("on_sale"))
    assert ["table#.legal", "OPERATOR_PRIVACY"] in captured.provenance["excluded"]


def test_controls_and_admissible_literals_are_kept() -> None:
    text = sample("on_sale").structure_json
    for kept in ('"btn-order"', '"btn-basket"', '"min":"1"', '"max":"30"', '"@type":"Product"'):
        assert kept in text, kept
    assert '"assignment":"window.goodsInfo"' in text and '"stock":"AVAILABLE"' in text


def test_hidden_controls_are_kept_and_only_sensitive_values_are_stripped() -> None:
    root = from_structure(sample("on_sale").structure)
    inputs = {e.attributes.get("name"): e.attributes for e in root.walk() if e.tag == "input"}
    assert inputs["goods_no"]["value"] == "SM-5001"
    assert "value" not in inputs["xsrf_token"] and "value" not in inputs["ea"]
    assert inputs["ea"]["max"] == "30"


def test_urls_handlers_secrets_member_data_and_code_are_stripped() -> None:
    captured = sample("on_sale")
    for gone in (
        "sig=abc",
        "expires=99",
        "?w=800",
        "goods=SM-5001",
        "xsrf=q",
        "onclick",
        "order()",
        "not-a-real-session",
        "sessionKey",
        "memberLevel",
        "resolveViewer",
        "xsrf-not-a-real",
    ):
        assert gone not in captured.structure_json, gone
    assert '"thumb":"https://img.synmart.example/sm-5001.jpg"' in captured.structure_json
    removals = {what for what, _ in captured.provenance["removals"]}
    assert {
        "EMBEDDED_URL_QUERY",
        "EMBEDDED_KEY:sessionKey",
        "EMBEDDED_KEY:memberLevel",
        "SCRIPT_NOT_LITERAL",
        "EVENT_HANDLER:onclick",
        "INPUT_VALUE",
        "URL_QUERY:src",
    } <= removals


def test_private_attribute_names_are_stripped_everywhere() -> None:
    captured = _capture(
        _inject('<div class="meta" data-member-id="m1" data-account-no="a1">i</div>')
    )
    text = captured.structure_json  # type: ignore[attr-defined]
    assert "data-member-id" not in text and "data-account-no" not in text


@pytest.mark.parametrize(
    "residual",
    [
        '<span class="note" data-note="buyer@example.com">판매자</span>',
        '<div class="note" data-ref="sid=0a1b2c3d4e">참고</div>',
        '<p class="cs">문의 010-2345-6789</p>',
        '<p class="debug">eyJhbGciOiJIUzI1NiJ9.payload</p>',
        '<script>var sellerNote = {"note": "문의 buyer@example.com"};</script>',
    ],
    ids=["email-attribute", "sid-attribute", "phone-text", "jwt-text", "email-embedded"],
)
def test_residual_private_material_refuses_before_any_sample_exists(residual: str) -> None:
    with pytest.raises(CaptureRefused, match="residual") as refused:
        _capture(_inject(residual))
    for value in ("buyer@example.com", "0a1b2c3d4e", "2345-6789", "eyJhbGci"):
        assert value not in str(refused.value)


def test_every_fixture_sample_passes_the_final_scan() -> None:
    for name in SAMPLES:
        assert final_scan(sample(name).structure) == [], name


def test_an_oversized_block_is_digest_only_and_marks_the_sample_truncated() -> None:
    import json

    big = json.dumps({"sku": "SM-5001", "blob": "가" * BLOCK_MAX_BYTES})
    captured = _capture(_inject(f"<script>var huge = {big};</script>"))
    assert captured.truncated  # type: ignore[attr-defined]
    text = captured.structure_json  # type: ignore[attr-defined]
    assert "가가가" not in text and '"truncated":true' in text


def test_a_sample_is_immutable_and_content_addressed() -> None:
    first, again = sample("on_sale"), sample("on_sale")
    assert first.digest == again.digest
    view = first.structure
    view["children"].clear()
    assert first.structure["children"]
    with pytest.raises(AttributeError):
        first.digest = "x"  # type: ignore[misc]
