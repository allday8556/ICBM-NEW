"""Proof 7 — the same fixture gives byte-equivalent normalized output and digests."""

from prototypes.adaptive_collector.dom import from_snapshot, parse_html
from prototypes.adaptive_collector.engine import extract
from prototypes.adaptive_collector.profile import Bundle, ProfileStore, canonical
from prototypes.adaptive_collector.testsupport import (
    SAMPLE_PAGES,
    page,
    sample,
    synthetic_bundle,
)
from prototypes.adaptive_collector.validation import run_digest, validate


def test_repeated_extraction_is_byte_equivalent(bundle: Bundle) -> None:
    for name in SAMPLE_PAGES:
        captured = sample(name)
        outputs = {
            canonical(extract(bundle, from_snapshot(captured.snapshot)).normalized())
            for _ in range(3)
        }
        assert len(outputs) == 1, name
        assert (
            extract(bundle, from_snapshot(captured.snapshot)).digest()
            == extract(bundle, from_snapshot(sample(name).snapshot)).digest()
        )


def test_a_fresh_store_and_a_fresh_capture_give_the_same_digests() -> None:
    first = synthetic_bundle(ProfileStore())
    second = synthetic_bundle(ProfileStore())
    assert first.epr_digest == second.epr_digest
    assert [sample(n).digest for n in SAMPLE_PAGES] == [sample(n).digest for n in SAMPLE_PAGES]


def test_the_raw_page_and_its_sample_differ_only_where_the_capture_stripped(bundle: Bundle) -> None:
    # The sold-out page carries nothing the capture strips, so both readers agree exactly.
    raw = extract(bundle, parse_html(page("simple_sold_out")))
    replayed = extract(bundle, from_snapshot(sample("simple_sold_out").snapshot))
    assert raw.normalized()["fields"] == replayed.normalized()["fields"]


def test_a_validation_run_is_deterministic(bundle: Bundle, negatives: dict[str, str]) -> None:
    samples = [sample(n) for n in SAMPLE_PAGES]
    assert run_digest(validate(bundle, samples, negatives=negatives)) == run_digest(
        validate(bundle, samples, negatives=negatives)
    )
