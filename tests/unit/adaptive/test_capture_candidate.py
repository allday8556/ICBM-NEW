"""Capture candidates (Phase C C0, Issue #110 `5826469852` item 3–4).

A candidate is cut from a whole document in memory with no operator scope; a sample is cut from the
candidate with the operator's scope later. The sample's structure and truncation must be exactly
what ``capture_sample`` cuts from the page itself with the same scope, including every bound. Its
``excluded`` diagnostics hold the same entries, but not byte-identically when the operator excludes
a region: the candidate path lists the generic exclusions first and then the operator's, each in
document order, where the page path interleaves them (review ``5313663701``, judgment 2). Its
provenance also names the candidate, so the two sample digests are never claimed equal.
"""

import json

import pytest

from app.collect.adaptive.capture import (
    SAMPLE_EMBEDDED_MAX_BYTES,
    CaptureRefused,
    OperatorExclusion,
    OperatorReason,
    OperatorScope,
    capture_candidate,
    capture_sample,
    sample_from_candidate,
)
from tests.adaptive_support import SAMPLES, expected, page, scope_for


@pytest.mark.parametrize("name", SAMPLES)
def test_a_sample_from_a_candidate_is_the_sample_the_page_would_give(name: str) -> None:
    html = page(name)
    scope = scope_for(html)
    direct = capture_sample(html, scope, expected(name))
    cut = sample_from_candidate(capture_candidate(html), scope, expected(name))
    assert cut.structure == direct.structure
    assert cut.truncated == direct.truncated
    for key in ("excluded", "removals", "scope", "capture_revision"):
        assert cut.provenance[key] == direct.provenance[key], key
    assert "candidate" in cut.provenance and "profile" not in json.dumps(cut.provenance).lower()


def test_an_operator_exclusion_applies_the_same_way() -> None:
    html = page("on_sale")
    base = scope_for(html)
    scope = OperatorScope(
        base.decided_by,
        base.decided_at,
        base.product_boundary,
        base.confirmed_regions,
        (OperatorExclusion("description", OperatorReason.PRIVACY),),
    )
    direct = capture_sample(html, scope, expected("on_sale"))
    cut = sample_from_candidate(capture_candidate(html), scope, expected("on_sale"))
    assert cut.structure == direct.structure
    # The same entries: the candidate lists the generically excluded regions first, then the
    # operator's own, each in document order; the page path interleaves them. Both are fixed.
    assert sorted(cut.provenance["excluded"]) == sorted(direct.provenance["excluded"])
    again = sample_from_candidate(capture_candidate(html), scope, expected("on_sale"))
    assert again.digest == cut.digest


def test_every_excluded_region_inside_the_boundary_needs_confirmation() -> None:
    html = page("on_sale")
    candidate = capture_candidate(html)
    scope = scope_for(html)
    assert scope.confirmed_regions, "the fixture has regions the operator must confirm"
    unconfirmed = OperatorScope(scope.decided_by, scope.decided_at, scope.product_boundary, ())
    with pytest.raises(CaptureRefused, match="not confirmed"):
        sample_from_candidate(candidate, unconfirmed, expected("on_sale"))
    assert {cls for _, cls in candidate.regions()} <= {"NON_AUTHORITATIVE", "PRIVATE", "NAVIGATION"}


def test_a_candidate_keeps_no_page_body_and_refuses_residual_material() -> None:
    html = page("on_sale")
    candidate = capture_candidate(html)
    text = candidate.structure_json
    assert "<" not in text and "sessionKey" not in text and "expires=" not in text
    with pytest.raises(CaptureRefused):
        capture_candidate(
            '<html><body><p>contact 010-1234-5678 now</p><p class="x">a@b.example</p></body></html>'
        )


def test_a_candidate_changed_after_capture_never_becomes_a_sample() -> None:
    html = page("on_sale")
    candidate = capture_candidate(html)
    structure = candidate.structure
    structure["children"].append("an added text")
    changed = type(candidate)(
        json.dumps(structure), candidate.excluded_json, candidate.removals_json, candidate.digest
    )
    with pytest.raises(CaptureRefused, match="does not recompute"):
        sample_from_candidate(changed, scope_for(html), expected("on_sale"))


def test_the_per_sample_embedded_bound_is_applied_where_the_sanitizer_applies_it() -> None:
    block = json.dumps({"rows": ["x" * 1000] * 60})  # about 60 KiB, under the per-block bound
    blocks = "".join(f'<script type="application/json">{block}</script>' for _ in range(6))
    html = (
        '<html><body><section class="goods-view"><h2 class="goods-name">Thing</h2>'
        f"{blocks}</section></body></html>"
    )
    scope = OperatorScope("operator:synthetic", "2026-09-25T00:00:00Z", "goods-view", ())
    direct = capture_sample(html, scope, {})
    cut = sample_from_candidate(capture_candidate(html), scope, {})
    assert direct.truncated and cut.truncated
    assert cut.structure == direct.structure
    kept = sum(
        len(json.dumps(node["data"], separators=(",", ":")))
        for node in cut.structure["children"][0]["children"]
        if isinstance(node, dict) and "data" in node
    )
    assert kept <= SAMPLE_EMBEDDED_MAX_BYTES
