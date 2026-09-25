"""The shadow comparison: fields, identity and M1/M2 image pairing (ADR-0017 §10.3, §10.4).

The Adaptive side is the real engine over the synthetic ``synmart`` pages; the canonical side is
built beside it so each case changes exactly one thing. Nothing here reaches a network.
"""

import json
from dataclasses import replace
from typing import Any
from urllib.parse import urljoin

import pytest

from app.collect.adaptive.document import read_html
from app.collect.adaptive.engine import Extraction, extract
from app.collect.adaptive_shadow.compare import (
    FieldVerdict,
    ImageOutcome,
    Severity,
    ShadowEvaluationFailed,
    compare,
    compare_fields,
)
from app.collect.adaptive_shadow.evidence import RunVerdict
from app.collect.facts import (
    EvaluatedFacts,
    EvaluatedField,
    FactsStatus,
    FetchTargetRefusal,
    FieldFact,
    FieldLevel,
    FieldStatus,
    ImageReference,
    ImageRole,
)
from app.collect.urls import UrlPolicy
from integrations.suppliers.collection import ImageCandidate, SourceIdentity, UnresolvedIdentity
from integrations.suppliers.collection import ImageRole as SourceRole
from tests.adaptive_support import page, synmart_bundle
from tests.collect_support import collected

DOC = "https://shop.synmart.example/goods/view/sm-5001"
SHA = "a" * 64


def extraction(name: str = "on_sale") -> Extraction:
    return extract(synmart_bundle(), read_html(page(name)))


def canonical_images(
    found: Extraction, *, sha: str | None = SHA
) -> tuple[list[ImageCandidate], list[ImageReference]]:
    """The canonical side's candidates and references, one for each Adaptive reference."""
    candidates, references = [], []
    for order, image in enumerate(found.images):
        role = SourceRole.PRIMARY if image.role == "REPRESENTATIVE" else SourceRole.DETAIL
        candidates.append(
            ImageCandidate(urljoin(DOC, image.reference), role, order, "rule", image.reference)
        )
        references.append(
            ImageReference(
                role=ImageRole(image.role),
                ordinal=order,
                host="shop.synmart.example",
                provenance="rule",
                status=FieldStatus.CONFIRMED if sha else FieldStatus.REVIEW_REQUIRED,
                sha256=sha,
            )
        )
    return candidates, references


def run(
    found: Extraction,
    *,
    candidates: list[ImageCandidate] | None = None,
    images: list[ImageReference] | None = None,
    identity: Any = None,
    fields: dict[str, FieldFact] | None = None,
) -> Any:
    default_candidates, default_images = canonical_images(found)
    source_id = found.source_product_id or "SM-5001"
    return compare(
        source_url=DOC,
        identity=identity or SourceIdentity(source_id),
        collected=collected(
            fields=dict(found.fields) if fields is None else fields,
            images=(),
            supplier_key="synmart",
            source_product_id=source_id,
        ),
        url_policy=UrlPolicy({}),
        candidates=default_candidates if candidates is None else candidates,
        images=default_images if images is None else images,
        extraction=found,
    )


def test_identical_sides_match_with_inherited_checksums() -> None:
    found = extraction()
    assert found.images, "the fixture states image references"
    result = run(found)
    assert result.verdict is RunVerdict.MATCH and result.severity is None
    assert all(f.verdict is FieldVerdict.MATCH for f in result.fields)
    assert {i.outcome for i in result.images} == {ImageOutcome.MATCHED}
    assert all(i.sha256 == SHA for i in result.images)


def test_a_reference_the_canonical_run_observed_no_bytes_for_matches_without_a_checksum() -> None:
    found = extraction()
    candidates, images = canonical_images(found, sha=None)
    result = run(found, candidates=candidates, images=images)
    assert {i.outcome for i in result.images} == {ImageOutcome.MATCHED_NO_BYTES}
    assert result.verdict is RunVerdict.MATCH


def test_m2_pairs_by_the_written_reference_when_the_canonical_target_was_refused() -> None:
    found = extraction()
    candidates, images = canonical_images(found)
    images[0] = replace(images[0], target_refusal=next(iter(FetchTargetRefusal)))
    result = run(found, candidates=candidates, images=images)
    assert result.verdict is RunVerdict.MATCH
    assert len([i for i in result.images if i.outcome is ImageOutcome.MATCHED]) == len(images)


def test_unpaired_references_are_missed_or_unobserved_and_never_matched() -> None:
    found = extraction()
    candidates, images = canonical_images(found)
    extra = ImageCandidate(urljoin(DOC, "/goods/other.jpg"), SourceRole.DETAIL, 99, "r", "x.jpg")
    candidates.append(extra)
    images.append(replace(images[-1], ordinal=99))
    missing_adaptive = replace(found, images=found.images[1:])
    result = run(missing_adaptive, candidates=candidates, images=images)
    outcomes = [i.outcome for i in result.images]
    assert outcomes.count(ImageOutcome.MISSED) == 2
    assert result.verdict is RunVerdict.MISMATCH
    over = run(found, candidates=candidates[:1], images=images[:1])
    assert ImageOutcome.UNOBSERVED in {i.outcome for i in over.images}
    assert over.severity is Severity.ADAPTIVE_OVERCONFIDENT


def test_equal_duplicate_references_pair_by_occurrence() -> None:
    found = extraction()
    twice = replace(found, images=(found.images[0], replace(found.images[0], ordinal=1)))
    candidates, images = canonical_images(twice)
    result = run(twice, candidates=candidates, images=images)
    assert [i.outcome for i in result.images] == [ImageOutcome.MATCHED, ImageOutcome.MATCHED]
    assert result.verdict is RunVerdict.MATCH


def test_uneven_duplicates_with_differing_roles_are_unmatchable_and_never_a_success() -> None:
    found = extraction()
    first = found.images[0]
    # The page repeats one reference: twice as the representative image on the Adaptive side,
    # once as a detail image on the canonical side. No unique pairing exists.
    adaptive = replace(found, images=(first, replace(first, ordinal=1)))
    candidate = ImageCandidate(urljoin(DOC, first.reference), SourceRole.DETAIL, 0, "r", None)
    reference = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=0,
        host="shop.synmart.example",
        provenance="r",
        status=FieldStatus.CONFIRMED,
        sha256=SHA,
    )
    result = run(adaptive, candidates=[candidate], images=[reference])
    assert {i.outcome for i in result.images} == {ImageOutcome.UNMATCHABLE}
    assert result.verdict is RunVerdict.IMAGE_UNMATCHABLE


def test_a_reference_stated_neither_resolved_nor_written_is_unmatchable() -> None:
    found = replace(extraction(), images=())
    candidate = ImageCandidate("https://img.example/x.jpg", SourceRole.PRIMARY, 0, "r", None)
    reference = ImageReference(
        role=ImageRole.REPRESENTATIVE,
        ordinal=0,
        host="img.example",
        provenance="r",
        status=FieldStatus.REVIEW_REQUIRED,
        target_refusal=next(iter(FetchTargetRefusal)),
    )
    result = run(found, candidates=[candidate], images=[reference])
    assert [i.outcome for i in result.images] == [ImageOutcome.UNMATCHABLE]
    assert result.verdict is RunVerdict.IMAGE_UNMATCHABLE


def test_a_role_disagreement_is_a_confident_mismatch() -> None:
    found = extraction()
    candidates, images = canonical_images(found)
    images[-1] = replace(
        images[-1],
        role=ImageRole.REPRESENTATIVE if images[-1].role is ImageRole.DETAIL else ImageRole.DETAIL,
    )
    result = run(found, candidates=candidates, images=images)
    assert result.verdict is RunVerdict.MISMATCH
    assert result.severity is Severity.CONFIDENT_DISAGREEMENT


def _evaluated(**statuses: tuple[FieldStatus, str | None]) -> EvaluatedFacts:
    fields = tuple(
        EvaluatedField(key, FieldLevel.CORE, status, value, (), "f" * 64)
        for key, (status, value) in statuses.items()
    )
    return EvaluatedFacts(collected(), fields, (), "s" * 64, FactsStatus.CONFIRMED)


@pytest.mark.parametrize(
    ("canonical", "adaptive", "verdict", "severity"),
    [
        (
            (FieldStatus.CONFIRMED, '"a"'),
            (FieldStatus.CONFIRMED, '"b"'),
            "VALUE_MISMATCH",
            "CONFIDENT_DISAGREEMENT",
        ),
        (
            (FieldStatus.ABSENT, None),
            (FieldStatus.CONFIRMED, '"b"'),
            "STATUS_MISMATCH",
            "CONFIDENT_DISAGREEMENT",
        ),
        (
            (FieldStatus.REVIEW_REQUIRED, None),
            (FieldStatus.CONFIRMED, '"b"'),
            "STATUS_MISMATCH",
            "ADAPTIVE_OVERCONFIDENT",
        ),
        (
            (FieldStatus.CONFIRMED, '"a"'),
            (FieldStatus.REVIEW_REQUIRED, None),
            "STATUS_MISMATCH",
            "ADAPTIVE_CONSERVATIVE",
        ),
        (
            (FieldStatus.ABSENT, None),
            (FieldStatus.REVIEW_REQUIRED, None),
            "STATUS_MISMATCH",
            "ADAPTIVE_CONSERVATIVE",
        ),
    ],
)
def test_field_verdicts_and_their_severity(
    canonical: tuple[FieldStatus, str | None],
    adaptive: tuple[FieldStatus, str | None],
    verdict: str,
    severity: str,
) -> None:
    (compared,) = compare_fields(_evaluated(brand=canonical), _evaluated(brand=adaptive))
    assert (compared.verdict.value, compared.severity) == (verdict, Severity(severity))


@pytest.mark.parametrize("key", ["options", "quantity_tiers"])
def test_a_positive_adaptive_option_or_tier_is_overconfident_under_the_m3_boundary(
    key: str,
) -> None:
    same = {key: (FieldStatus.CONFIRMED, '{"configurations":[{"x":1}],"tiers":[{"x":1}]}')}
    (compared,) = compare_fields(_evaluated(**same), _evaluated(**same))
    assert compared.verdict is FieldVerdict.STATUS_MISMATCH
    assert compared.severity is Severity.ADAPTIVE_OVERCONFIDENT
    # Stating that there is none is not a positive claim.
    none = {key: (FieldStatus.CONFIRMED, '{"axes":[],"configurations":[]}')}
    (compared,) = compare_fields(_evaluated(**none), _evaluated(**none))
    assert compared.verdict is FieldVerdict.MATCH and compared.severity is None


def test_identity_disagreements_and_their_severity() -> None:
    found = extraction()
    other = run(found, identity=SourceIdentity("SM-9999"))
    assert other.verdict is RunVerdict.IDENTITY_MISMATCH
    assert other.severity is Severity.CONFIDENT_DISAGREEMENT
    unresolved = run(found, identity=UnresolvedIdentity("the page states none"))
    assert unresolved.identity == {
        "canonical": "UNRESOLVED",
        "adaptive": "RESOLVED",
        "agrees": False,
    }
    assert unresolved.severity is Severity.ADAPTIVE_OVERCONFIDENT


def test_an_unmatched_template_is_its_own_verdict() -> None:
    found = extraction("login")
    result = run(found, candidates=[], images=[])
    assert result.verdict is RunVerdict.TEMPLATE_UNMATCHED
    assert result.severity is Severity.ADAPTIVE_CONSERVATIVE


def test_adaptive_facts_the_evaluation_refuses_are_a_shadow_failure() -> None:
    found = extraction()
    broken = replace(found, fields={k: v for k, v in found.fields.items() if k != "prices"})
    with pytest.raises(ShadowEvaluationFailed):
        run(broken, fields=dict(found.fields))


def test_the_comparison_keeps_no_url_written_reference_or_digest_of_one() -> None:
    found = extraction()
    candidates, images = canonical_images(found)
    images[0] = replace(images[0], target_refusal=next(iter(FetchTargetRefusal)))
    text = json.dumps(run(found, candidates=candidates, images=images).as_json())
    for image in found.images:
        assert image.reference not in text and urljoin(DOC, image.reference) not in text
    assert "http" not in text and "/goods/" not in text
