"""Proof 4 — V2 image-region and image-role coverage without fetching bytes (ADR-0017 §7.2)."""

import json

from prototypes.adaptive_collector.dom import from_snapshot
from prototypes.adaptive_collector.engine import ImageCoverage, extract
from prototypes.adaptive_collector.fixtures import profiles
from prototypes.adaptive_collector.profile import Bundle, ProfileStore
from prototypes.adaptive_collector.tests.conftest import SAMPLE_PAGES, sample
from prototypes.adaptive_collector.validation import Verdict, _v2, validate


def test_image_references_keep_role_and_ordinal_and_no_bytes_are_read(bundle: Bundle) -> None:
    # The network is refused for this test (conftest); references are candidates, not fetches.
    for name in SAMPLE_PAGES:
        captured = sample(name)
        got = extract(bundle, from_snapshot(captured.snapshot))
        assert [[i.role, i.ordinal, i.reference] for i in got.images] == captured.expected["images"]
        assert got.images_status is ImageCoverage.COMPLETE


def test_a_signed_query_never_reaches_the_sample_or_the_reference(bundle: Bundle) -> None:
    captured = sample("simple_on_sale")
    assert "sig=" not in captured.snapshot_json
    got = extract(bundle, from_snapshot(captured.snapshot))
    assert all("?" not in image.reference for image in got.images)


def test_v2_fails_for_a_template_without_an_image_region(store: ProfileStore) -> None:
    template = profiles.ptr("simple", optioned=False)
    template["image_regions"] = []
    bundle = store.bundle(store.put(profiles.epr([store.put(template)])))
    check = _v2(bundle, [sample("simple_on_sale")])
    assert check.outcome is Verdict.FAIL
    assert any("no image region" in detail for detail in check.details)


def test_v2_fails_for_an_epr_that_cannot_assign_a_representative_image(store: ProfileStore) -> None:
    simple = store.put(profiles.ptr("simple", optioned=False))
    epr = profiles.epr([simple])
    epr["image_roles"] = [{"region": "detail", "role": "DETAIL", "take": "ALL"}]
    check = _v2(store.bundle(store.put(epr)), [sample("simple_on_sale")])
    assert check.outcome is Verdict.FAIL
    assert any("representative" in detail for detail in check.details)


def test_images_is_core_but_no_parser_field_produces_it(bundle: Bundle) -> None:
    got = extract(bundle, from_snapshot(sample("simple_on_sale").snapshot))
    assert "images" not in got.fields  # the image pipeline produces it, never a field rule


def test_a_missing_representative_is_never_a_complete_image_set(bundle: Bundle) -> None:
    html_sample = sample("simple_sold_out")
    snapshot = html_sample.snapshot_json.replace('"class":"main"', '"class":"other"')
    got = extract(bundle, from_snapshot(json.loads(snapshot)))
    assert got.images_status is ImageCoverage.REVIEW_REQUIRED


def test_validation_passes_image_coverage_on_the_synthetic_bundle(
    bundle: Bundle, negatives: dict[str, str]
) -> None:
    run = validate(bundle, [sample(n) for n in SAMPLE_PAGES], negatives=negatives)
    assert run.check("V2").outcome is Verdict.PASS
