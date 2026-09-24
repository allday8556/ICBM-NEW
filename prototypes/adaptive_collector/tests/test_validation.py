"""Proof 8 — V4 negative controls, plus the PASS path and derived VALIDATED (ADR-0017 §7)."""

from dataclasses import replace

from prototypes.adaptive_collector.capture import capture_sample
from prototypes.adaptive_collector.fixtures import profiles
from prototypes.adaptive_collector.hooks import HookManifest
from prototypes.adaptive_collector.profile import Bundle, ProfileStore
from prototypes.adaptive_collector.tests.conftest import (
    SAMPLE_PAGES,
    expected,
    hook_manifest,
    page,
    sample,
    scope_for,
)
from prototypes.adaptive_collector.validation import (
    ValidationRun,
    Verdict,
    freshness_tuple,
    is_validated,
    validate,
)


def _run(bundle: Bundle, negatives: dict[str, str], **kwargs: object) -> ValidationRun:
    return validate(bundle, [sample(n) for n in SAMPLE_PAGES], negatives=negatives, **kwargs)  # type: ignore[arg-type]


def _resolved(run: ValidationRun) -> frozenset[str]:
    return frozenset(run.check("V3a").details)


def test_every_negative_control_fails_closed(bundle: Bundle, negatives: dict[str, str]) -> None:
    run = _run(bundle, negatives)
    assert run.check("V4").outcome is Verdict.PASS, run.check("V4").details


def test_an_open_v3a_finding_blocks_pass_until_the_operator_resolves_it(
    bundle: Bundle, negatives: dict[str, str]
) -> None:
    first = _run(bundle, negatives)
    assert first.verdict is Verdict.INCOMPLETE
    assert first.check("V3a").outcome is Verdict.INCOMPLETE
    resolved = _run(bundle, negatives, resolved_findings=_resolved(first))
    assert resolved.verdict is Verdict.PASS, [(c.name, c.details) for c in resolved.checks]


def test_validated_is_derived_from_a_pass_for_the_exact_freshness_tuple(
    bundle: Bundle, negatives: dict[str, str]
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
    store: ProfileStore, negatives: dict[str, str]
) -> None:
    wrong = profiles.ptr("simple", optioned=False)
    wrong["fields"]["brand"]["primary"]["vocabulary"] = "origin_labels"  # reads the wrong row
    optioned = store.put(profiles.ptr("optioned", optioned=True))
    bundle = store.bundle(store.put(profiles.epr([store.put(wrong), optioned])))
    run = _run(bundle, negatives)
    assert run.verdict is Verdict.FAIL
    assert any("brand" in detail for detail in run.check("V3").details)


def test_a_bundle_that_matches_a_login_page_fails_v4(
    store: ProfileStore, negatives: dict[str, str]
) -> None:
    loose = profiles.ptr("loose", optioned=False)
    loose["signature"] = {"required": ["h1"], "forbidden": []}
    bundle = store.bundle(store.put(profiles.epr([store.put(loose)])))
    run = validate(bundle, [sample("simple_on_sale")], negatives=negatives)
    assert run.check("V4").outcome is Verdict.FAIL


def test_one_sample_per_template_and_two_in_total_are_required(
    bundle: Bundle, negatives: dict[str, str]
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
    run = validate(
        bundle, [unshipped], negatives={"login": page("login")}, manifest=hook_manifest()
    )
    assert run.check("V7").outcome is Verdict.FAIL
    assert any(
        "G7" in detail and "value_parse:shipping" in detail for detail in run.check("V7").details
    )


def test_g6_over_the_cap_blocks_validated_until_an_architecture_review(
    store: ProfileStore, negatives: dict[str, str]
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
