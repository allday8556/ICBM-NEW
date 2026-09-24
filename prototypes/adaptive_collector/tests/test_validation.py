"""Proof 8 — V4 negative controls, plus the PASS path and derived VALIDATED (ADR-0017 §7)."""

from dataclasses import replace

import pytest

from prototypes.adaptive_collector.capture import capture_sample
from prototypes.adaptive_collector.fixtures import profiles
from prototypes.adaptive_collector.hooks import HookManifest
from prototypes.adaptive_collector.profile import Bundle, ProfileStore
from prototypes.adaptive_collector.testsupport import (
    SAMPLE_PAGES,
    Negatives,
    expected,
    hook_manifest,
    negative_pages,
    page,
    sample,
    scope_for,
)
from prototypes.adaptive_collector.validation import (
    REQUIRED_MUTATIONS,
    NegativeClass,
    ValidationRun,
    Verdict,
    freshness_tuple,
    is_validated,
    validate,
)


def _run(bundle: Bundle, negatives: Negatives, **kwargs: object) -> ValidationRun:
    return validate(bundle, [sample(n) for n in SAMPLE_PAGES], negatives=negatives, **kwargs)  # type: ignore[arg-type]


def _resolved(run: ValidationRun) -> frozenset[str]:
    return frozenset(run.check("V3a").details)


def test_every_negative_control_fails_closed(bundle: Bundle, negatives: Negatives) -> None:
    run = _run(bundle, negatives)
    assert run.check("V4").outcome is Verdict.PASS, run.check("V4").details


def test_an_open_v3a_finding_blocks_pass_until_the_operator_resolves_it(
    bundle: Bundle, negatives: Negatives
) -> None:
    first = _run(bundle, negatives)
    assert first.verdict is Verdict.INCOMPLETE
    assert first.check("V3a").outcome is Verdict.INCOMPLETE
    resolved = _run(bundle, negatives, resolved_findings=_resolved(first))
    assert resolved.verdict is Verdict.PASS, [(c.name, c.details) for c in resolved.checks]


def test_validated_is_derived_from_a_pass_for_the_exact_freshness_tuple(
    bundle: Bundle, negatives: Negatives
) -> None:
    samples = [sample(n) for n in SAMPLE_PAGES]
    first = validate(bundle, samples, negatives=negatives)
    run = validate(bundle, samples, negatives=negatives, resolved_findings=_resolved(first))
    current = freshness_tuple(bundle, samples, None)
    assert is_validated([run], current)
    # Any freshness member changing lapses VALIDATED; nothing stored has to be forgotten.
    stale = (*current[:3], "0" * 64, *current[4:])  # a new engine implementation fingerprint
    assert not is_validated([run], stale)
    assert not is_validated([replace(run, verdict=Verdict.INCOMPLETE)], current)


def test_a_wrong_profile_fails_v3_against_operator_expectations(
    store: ProfileStore, negatives: Negatives
) -> None:
    wrong = profiles.ptr("simple", optioned=False)
    wrong["fields"]["brand"]["primary"]["vocabulary"] = "origin_labels"  # reads the wrong row
    optioned = store.put(profiles.ptr("optioned", optioned=True))
    bundle = store.bundle(store.put(profiles.epr([store.put(wrong), optioned])))
    run = _run(bundle, negatives)
    assert run.verdict is Verdict.FAIL
    assert any("brand" in detail for detail in run.check("V3").details)


def test_a_bundle_that_matches_a_login_page_fails_v4(
    store: ProfileStore, negatives: Negatives
) -> None:
    loose = profiles.ptr("loose", optioned=False)
    loose["signature"] = {"required": ["h1"], "forbidden": []}
    bundle = store.bundle(store.put(profiles.epr([store.put(loose)])))
    run = validate(bundle, [sample("simple_on_sale")], negatives=negatives)
    assert run.check("V4").outcome is Verdict.FAIL


def test_one_sample_per_template_and_two_in_total_are_required(
    bundle: Bundle, negatives: Negatives
) -> None:
    run = validate(bundle, [sample("simple_on_sale")], negatives=negatives)
    details = run.check("V3").details
    assert "no complete sample for template optioned" in details
    assert "at least two complete samples are required" in details


def test_g7_fails_a_bound_hook_no_sample_exercises(store: ProfileStore) -> None:
    template = store.put(profiles.ptr("simple", optioned=False, supplier=profiles.HOOKED_SUPPLIER))
    bundle = store.bundle(store.put(profiles.hooked_epr([template], "synhook-hooks-1")))
    # A sample with no shipping row: a bound hook is always the reader of its target, so here the
    # shipping hook is never called and its path is proven by no sample.
    html = page("simple_sold_out").replace("<tr><th>배송비</th><td>무료</td></tr>", "")
    unshipped = capture_sample(html, scope_for(html), expected("simple_sold_out"))
    run = validate(bundle, [unshipped], negatives=negative_pages(), manifest=hook_manifest())
    assert run.check("V7").outcome is Verdict.FAIL
    assert any(
        "G7" in detail and "value_parse:shipping" in detail for detail in run.check("V7").details
    )


def test_g6_over_the_cap_blocks_validated_until_an_architecture_review(
    store: ProfileStore, negatives: Negatives
) -> None:
    simple = store.put(profiles.ptr("simple", optioned=False))
    optioned = store.put(profiles.ptr("optioned", optioned=True))
    bindings = [
        {
            "hook_point": "value_parse",
            "target": t,
            "format_class": "LABELLED_TEXT",
            "hook_name": "noop",
            "hook_revision": "r1",
        }
        for t in ("brand", "origin", "manufacturer")
    ]
    bundle = store.bundle(store.put(profiles.epr([simple, optioned], hooks=bindings)))
    manifest = HookManifest("synthetic", "r1", "f" * 64, {"noop": lambda text: {"text": text}})
    run = _run(bundle, negatives, manifest=manifest)
    assert run.check("V7").outcome is Verdict.INCOMPLETE
    reviewed = _run(bundle, negatives, manifest=manifest, architecture_review_recorded=True)
    assert reviewed.check("V7").outcome is Verdict.PASS


def test_all_five_required_mutations_run_on_the_synthetic_samples(
    bundle: Bundle, negatives: Negatives
) -> None:
    details = _run(bundle, negatives).check("V4").details
    for name in REQUIRED_MUTATIONS:
        counts = [d for d in details if d.startswith(f"EXERCISED:{name}x")]
        assert counts and not counts[0].endswith("x0"), name
    assert not any(d.startswith("MUTATION_NOT_EXERCISED") for d in details)


def test_a_mutation_that_cannot_be_constructed_is_never_a_silent_pass(
    store: ProfileStore, negatives: Negatives
) -> None:
    # Prices read by a TEXT rule: the duplicate-price-row mutation cannot be constructed.
    simple = profiles.ptr("simple", optioned=False)
    optioned = profiles.ptr("optioned", optioned=True)
    for template in (simple, optioned):
        template["fields"]["prices"] = {"primary": {"kind": "TEXT", "selector": "table.info td"}}
    bundle = store.bundle(store.put(profiles.epr([store.put(simple), store.put(optioned)])))
    v4 = _run(bundle, negatives).check("V4")
    assert v4.outcome is not Verdict.PASS
    assert "MUTATION_NOT_EXERCISED:duplicate_price_row" in v4.details


def test_an_embedded_first_identity_source_still_gets_its_conflict_mutation(
    store: ProfileStore, negatives: Negatives
) -> None:
    simple = store.put(profiles.ptr("simple", optioned=False))
    optioned = store.put(profiles.ptr("optioned", optioned=True))
    epr = profiles.epr([simple, optioned])
    epr["identity"]["sources"] = list(reversed(epr["identity"]["sources"]))  # JSON-LD first
    bundle = store.bundle(store.put(epr))
    v4 = _run(bundle, negatives).check("V4")
    assert v4.outcome is Verdict.PASS, v4.details


def test_both_negative_page_classes_are_required(bundle: Bundle, negatives: Negatives) -> None:
    resolved = _resolved(_run(bundle, negatives))
    assert _run(bundle, negatives, resolved_findings=resolved).verdict is Verdict.PASS
    for missing in NegativeClass:
        partial = {k: v for k, v in negatives.items() if k is not missing}
        run = _run(bundle, partial, resolved_findings=resolved)
        assert run.check("V4").outcome is Verdict.INCOMPLETE, missing
        assert f"NEGATIVE_CONTROL_MISSING:{missing.value}" in run.check("V4").details
        assert run.verdict is not Verdict.PASS
    none = _run(bundle, {}, resolved_findings=resolved)
    assert {d for d in none.check("V4").details if d.startswith("NEGATIVE")} == {
        "NEGATIVE_CONTROL_MISSING:LOGIN",
        "NEGATIVE_CONTROL_MISSING:NON_PRODUCT",
    }


def test_negative_controls_are_typed_not_free_strings(bundle: Bundle) -> None:
    with pytest.raises(ValueError, match="NegativeClass"):
        validate(
            bundle,
            [sample(n) for n in SAMPLE_PAGES],
            negatives={"login": page("login")},  # type: ignore[dict-item]
        )
