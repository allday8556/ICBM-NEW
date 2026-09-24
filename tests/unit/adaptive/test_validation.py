"""V1–V8, typed negatives, mutation coverage and derived VALIDATED (ADR-0017 §7)."""

import json
from dataclasses import replace
from typing import Any

import pytest

from app.collect.adaptive.capture import BLOCK_MAX_BYTES, ValidationSample, capture_sample
from app.collect.adaptive.hooks import HookManifest
from app.collect.adaptive.validation import (
    REQUIRED_MUTATIONS,
    NegativeClass,
    SampleRefused,
    ValidationRun,
    Verdict,
    freshness_tuple,
    is_validated,
    validate,
)
from tests.adaptive_support import (
    Negatives,
    bundle_of,
    expected,
    hook_manifest,
    hooked_bundle,
    page,
    sample,
    samples,
    scope_for,
    synmart_bundle,
    template,
)


def _run(negatives: Negatives, bundle: Any = None, **kwargs: Any) -> ValidationRun:
    return validate(bundle or synmart_bundle(), samples(), negatives=negatives, **kwargs)


def _resolved(run: ValidationRun) -> frozenset[str]:
    return frozenset(run.check("V3a").details)


def test_the_first_run_is_incomplete_only_for_the_unread_embedded_data(
    negatives: Negatives,
) -> None:
    run = _run(negatives)
    assert run.verdict is Verdict.INCOMPLETE
    assert [c.name for c in run.checks if c.outcome is not Verdict.PASS] == ["V3a"]
    assert [d.split(":")[1] for d in run.check("V3a").details] == ["EMBEDDED_PRODUCT_DATA@4"]


def test_resolving_the_finding_passes_and_validated_is_derived(negatives: Negatives) -> None:
    first = _run(negatives)
    run = _run(negatives, resolved_findings=_resolved(first))
    assert run.verdict is Verdict.PASS, [(c.name, c.details) for c in run.checks]
    current = freshness_tuple(synmart_bundle(), samples(), None)
    assert is_validated([run], current)
    for index in range(len(current)):
        changed = tuple(f"{part}-changed" if i == index else part for i, part in enumerate(current))
        assert not is_validated([run], changed), index
    assert not is_validated([replace(run, verdict=Verdict.INCOMPLETE)], current)


def test_all_five_mutations_run_and_fail_closed(negatives: Negatives) -> None:
    details = _run(negatives).check("V4").details
    for name in REQUIRED_MUTATIONS:
        assert f"EXERCISED:{name}x3" in details, name
    assert _run(negatives).check("V4").outcome is Verdict.PASS


def test_a_mutation_that_cannot_run_is_never_a_silent_pass(negatives: Negatives) -> None:
    plain, choice = template("plain", choice=False), template("choice", choice=True)
    for document in (plain, choice):
        document["fields"]["prices"] = {"primary": {"kind": "TEXT", "locator": "table.spec td"}}
    v4 = _run(negatives, bundle_of([plain, choice])).check("V4")
    assert v4.outcome is not Verdict.PASS
    assert "MUTATION_NOT_EXERCISED:duplicate_price_row" in v4.details


def test_an_embedded_first_identity_source_still_gets_its_conflict_mutation(
    negatives: Negatives,
) -> None:
    sources = [
        {"kind": "EMBEDDED", "source": "JSON_LD", "json_ld_type": "Product", "path": ["sku"]},
        {"kind": "ATTRIBUTE", "locator": "meta[name=goods-code]", "attribute": "content"},
    ]
    v4 = _run(negatives, synmart_bundle(identity={"sources": sources})).check("V4")
    assert v4.outcome is Verdict.PASS, v4.details


@pytest.mark.parametrize("missing", list(NegativeClass))
def test_each_negative_class_is_required(negatives: Negatives, missing: NegativeClass) -> None:
    resolved = _resolved(_run(negatives))
    partial = {k: v for k, v in negatives.items() if k is not missing}
    run = _run(partial, resolved_findings=resolved)
    assert run.check("V4").outcome is Verdict.INCOMPLETE
    assert f"NEGATIVE_CONTROL_MISSING:{missing.value}" in run.check("V4").details
    assert run.verdict is not Verdict.PASS


def test_negative_controls_are_typed() -> None:
    with pytest.raises(ValueError, match="NegativeClass"):
        validate(synmart_bundle(), samples(), negatives={"login": page("login")})  # type: ignore[dict-item]


def test_a_bundle_that_matches_a_login_page_fails(negatives: Negatives) -> None:
    loose = template("loose", choice=False)
    loose["signature"] = {"required": ["h2.goods-name"], "forbidden": []}
    run = validate(bundle_of([loose]), [sample("on_sale")], negatives=negatives)
    assert run.check("V4").outcome is Verdict.FAIL


def test_a_wrong_profile_fails_v3(negatives: Negatives) -> None:
    wrong = template("plain", choice=False)
    wrong["fields"]["brand"]["primary"]["vocabulary"] = "origin_labels"
    run = _run(negatives, bundle_of([wrong, template("choice", choice=True)]))
    assert run.verdict is Verdict.FAIL
    assert any("brand" in detail for detail in run.check("V3").details)


def test_one_sample_per_template_and_two_in_total(negatives: Negatives) -> None:
    details = (
        validate(synmart_bundle(), [sample("on_sale")], negatives=negatives).check("V3").details
    )
    assert "no complete sample for template choice" in details
    assert "at least two complete samples are required" in details


def test_v2_fails_without_an_image_region_or_a_representative_rule(negatives: Negatives) -> None:
    no_region = template("plain", choice=False)
    no_region["image_regions"] = []
    assert _run(negatives, bundle_of([no_region])).check("V2").outcome is Verdict.FAIL
    detail_only = synmart_bundle(
        image_roles=[{"region": "detail", "role": "DETAIL", "take": "ALL"}]
    )
    assert _run(negatives, detail_only).check("V2").outcome is Verdict.FAIL


def test_a_truncated_sample_makes_the_run_incomplete(negatives: Negatives) -> None:
    big = json.dumps({"sku": "SM-5001", "blob": "가" * BLOCK_MAX_BYTES})
    html = page("on_sale").replace(
        '<h2 class="goods-name">', f'<script>var huge = {big};</script><h2 class="goods-name">'
    )
    truncated = capture_sample(html, scope_for(html), expected("on_sale"))
    run = validate(synmart_bundle(), [*samples(), truncated], negatives=negatives)
    assert run.verdict is Verdict.INCOMPLETE and run.check("V8").outcome is Verdict.INCOMPLETE


def test_a_provenance_naming_a_profile_is_refused(negatives: Negatives) -> None:
    good = sample("on_sale")
    provenance = good.provenance
    provenance["scope"]["decided_by"] = "profile:candidate"
    forged = ValidationSample(
        good.structure_json, good.expected_json, json.dumps(provenance), False, "f" * 64
    )
    with pytest.raises(SampleRefused):
        validate(synmart_bundle(), [forged, sample("sold_out")], negatives=negatives)


def test_g7_fails_a_bound_hook_no_sample_exercises(negatives: Negatives) -> None:
    html = page("sold_out").replace("<tr><th>배송비</th><td>무료</td></tr>", "")
    unshipped = capture_sample(html, scope_for(html), expected("sold_out"))
    run = validate(hooked_bundle(), [unshipped], negatives=negatives, manifest=hook_manifest())
    assert run.check("V7").outcome is Verdict.FAIL
    assert any("G7" in d and "value_parse:shipping" in d for d in run.check("V7").details)


def test_g6_over_the_cap_waits_for_an_architecture_review(negatives: Negatives) -> None:
    bindings = [
        {
            "hook_point": "value_parse",
            "target": target,
            "format_class": "LABELLED_TEXT",
            "hook_name": "echo",
            "hook_revision": "r1",
        }
        for target in ("brand", "origin", "manufacturer")
    ]
    bundle = synmart_bundle(hooks=bindings)
    manifest = HookManifest("synmart", "r1", "c" * 64, {"echo": lambda text: {"text": text}})
    assert _run(negatives, bundle, manifest=manifest).check("V7").outcome is Verdict.INCOMPLETE
    reviewed = _run(negatives, bundle, manifest=manifest, architecture_review_recorded=True)
    assert reviewed.check("V7").outcome is Verdict.PASS


def test_v1_fails_when_a_hook_does_not_bind(negatives: Negatives) -> None:
    run = validate(hooked_bundle(), samples(), negatives=negatives, manifest=None)
    assert run.verdict is Verdict.FAIL and run.check("V1").outcome is Verdict.FAIL


def test_a_validation_run_is_deterministic(negatives: Negatives) -> None:
    assert _run(negatives).digest() == _run(negatives).digest()


def test_a_truncated_sample_never_satisfies_the_image_expectation(negatives: Negatives) -> None:
    big = json.dumps({"sku": "SM-5001", "blob": "가" * BLOCK_MAX_BYTES})
    html = page("on_sale").replace(
        '<h2 class="goods-name">', f'<script>var huge = {big};</script><h2 class="goods-name">'
    )
    truncated = capture_sample(html, scope_for(html), expected("on_sale"))
    assert truncated.truncated
    alone = validate(synmart_bundle(), [truncated], negatives=negatives).check("V2")
    assert alone.outcome is Verdict.INCOMPLETE
    assert "no complete sample states the expected images" in alone.details
    # A complete sample without the expectation still fails, whatever a truncated one states.
    lacking = sample("sold_out")
    stripped = ValidationSample(
        lacking.structure_json,
        json.dumps({**lacking.expected, "images": []}),
        lacking.provenance_json,
        False,
        "e" * 64,
    )
    mixed = validate(synmart_bundle(), [stripped, truncated], negatives=negatives).check("V2")
    assert mixed.outcome is Verdict.FAIL
