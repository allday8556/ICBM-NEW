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
from prototypes.adaptive_collector.tests.conftest import (
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


def test_product_controls_and_admissible_embedded_literals_are_kept() -> None:
    snapshot = sample("simple_on_sale").snapshot_json
    for kept in ('"btn-buy"', '"btn-cart"', '"min":"1"', '"max":"99"', '"@type":"Product"'):
        assert kept in snapshot, kept
    assert '"assignment":"productData"' in snapshot and '"stock":"IN_STOCK"' in snapshot


def test_secrets_member_data_user_values_and_code_are_stripped() -> None:
    captured = sample("simple_on_sale")
    text = captured.snapshot_json
    for gone in (
        "sessvalue-not-a-real-token",
        "sessionToken",
        "memberGrade",
        "csrf-not-a-real-value",
        "csrf_token",
        "token=zzz",
        "sig=abc123",
        "trackPage",
        "currentViewer",
        "홍길동",
        '"value":"3"',
    ):
        assert gone not in text, gone
    removals = {what for what, _ in captured.provenance["removals"]}
    assert {
        "SCRIPT_NOT_LITERAL",
        "INPUT_SECURITY_OR_HIDDEN",
        "EMBEDDED_KEY:sessionToken",
    } <= removals


def test_non_authoritative_regions_are_excluded_by_boundary_and_class_only() -> None:
    captured = sample("simple_on_sale")
    excluded = dict(captured.provenance["excluded"])
    assert excluded["div#.review-list"] == "NON_AUTHORITATIVE"
    assert excluded["div#.recommend-box"] == "NON_AUTHORITATIVE"
    assert excluded["div#.member-info"] == "PRIVATE"
    # The review's price row and its sold-out words never become sample evidence.
    assert "9,000원" not in captured.snapshot_json and "품절될까" not in captured.snapshot_json
    assert "9,000원" not in captured.provenance_json


def test_an_unconfirmed_detected_region_refuses_the_capture() -> None:
    html = page("simple_on_sale")
    with pytest.raises(CaptureRefused):
        capture_sample(html, OperatorScope("operator", "t", ()), expected("simple_on_sale"))


def test_operator_exclusions_have_closed_reasons_and_are_recorded() -> None:
    assert {r.value for r in OperatorReason} == {"PRIVACY", "NON_AUTHORITATIVE"}
    html = page("simple_on_sale")
    scope = scope_for(html)
    narrowed = OperatorScope(
        scope.decided_by,
        scope.decided_at,
        scope.confirmed_regions,
        (OperatorExclusion("notice", OperatorReason.PRIVACY),),
    )
    captured = capture_sample(html, narrowed, expected("simple_on_sale"))
    assert ["table#.notice", "OPERATOR_PRIVACY"] in captured.provenance["excluded"]


def test_a_sample_is_immutable_and_content_addressed() -> None:
    first, again = sample("simple_on_sale"), sample("simple_on_sale")
    assert first.digest == again.digest
    view = first.snapshot
    view["children"].clear()
    assert first.snapshot["children"], "a parsed view never mutates the stored sample"
    with pytest.raises(AttributeError):
        first.digest = "x"  # type: ignore[misc]


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
        "<style>", f"<script>var bigData = {big};</script><style>"
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
