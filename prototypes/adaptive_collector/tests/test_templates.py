"""Proof 2 — exactly-one template matching; unmatched and ambiguous fail closed (ADR-0017 §8.1)."""

from prototypes.adaptive_collector.dom import from_snapshot, parse_html
from prototypes.adaptive_collector.engine import TemplateVerdict, extract, match_template
from prototypes.adaptive_collector.fixtures import profiles
from prototypes.adaptive_collector.profile import Bundle, ProfileStore
from prototypes.adaptive_collector.tests.conftest import SAMPLE_PAGES, page, sample


def test_every_sample_matches_exactly_its_template(bundle: Bundle) -> None:
    for name in SAMPLE_PAGES:
        captured = sample(name)
        verdict, template = match_template(bundle, from_snapshot(captured.snapshot))
        assert verdict is TemplateVerdict.MATCHED
        assert template is not None and template.template_key == captured.expected["template"]


def test_a_login_page_and_a_listing_page_match_no_template(bundle: Bundle) -> None:
    for name in ("login", "not_product"):
        extraction = extract(bundle, parse_html(page(name)))
        assert extraction.verdict is TemplateVerdict.TEMPLATE_UNMATCHED
        # Fail closed: no identity, no field and no image is produced for an unmatched page.
        assert extraction.identity is None and not extraction.fields and not extraction.images


def test_two_matching_templates_are_ambiguous_never_the_closest(store: ProfileStore) -> None:
    loose = profiles.ptr("loose", optioned=False)
    loose["signature"] = {"required": ["h1.product-title"], "forbidden": []}
    simple = store.put(profiles.ptr("simple", optioned=False))
    bundle = store.bundle(store.put(profiles.epr([simple, store.put(loose)])))
    extraction = extract(bundle, from_snapshot(sample("simple_on_sale").snapshot))
    assert extraction.verdict is TemplateVerdict.TEMPLATE_AMBIGUOUS
    assert extraction.template_key is None and not extraction.fields


def test_a_forbidden_anchor_excludes_a_template(bundle: Bundle) -> None:
    # The optioned page carries an option select, which the simple template forbids.
    _, template = match_template(bundle, from_snapshot(sample("optioned").snapshot))
    assert template is not None and template.template_key == "optioned"
