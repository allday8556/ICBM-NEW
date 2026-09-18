"""The generic image acceptance model (Issue #52 ruling 5723016554, R1–R14): pure, no database.

Every reference is decided on two axes — how certain its outcome is, and what acceptance does with
it — from its persisted diagnostics alone, by a closed table. The images field is then CONFIRMED
only when nothing is unresolved, the role minima hold on the references the source itself exposed,
and excluded references are not more than a third of them, each one counted.

Nothing here is supplier-specific: the references are synthetic, on an invented host.
"""

import inspect
import itertools
import json
from collections.abc import Iterable
from fractions import Fraction

import pytest
from pydantic import ValidationError

from app.collect import facts as facts_module
from app.collect.facts import (
    EXCLUDED_SHARE_LIMIT,
    EXCLUDED_SHARE_LOCATOR,
    EXCLUSION_TABLE,
    IMAGE_ACCEPTANCE_MODEL,
    MISSING_DETAIL_LOCATOR,
    MISSING_REPRESENTATIVE_LOCATOR,
    EvaluatedField,
    FactsStatus,
    FetchTargetRefusal,
    FieldStatus,
    ImageCertainty,
    ImageDecision,
    ImageDisposition,
    ImageExclusion,
    ImageIssue,
    ImageReference,
    ImageRole,
    ImageSummary,
    ImagesValue,
    LocatorForm,
    decide_image,
    evaluate,
)
from tests.collect_support import SHA, collected

HOST = "img.shop.example"
REP, DETAIL = ImageRole.REPRESENTATIVE, ImageRole.DETAIL
CONFIRMED, ABSENT, REVIEW = FieldStatus.CONFIRMED, FieldStatus.ABSENT, FieldStatus.REVIEW_REQUIRED
DETERMINATE, INDETERMINATE = ImageCertainty.DETERMINATE, ImageCertainty.INDETERMINATE
INCLUDED, EXCLUDED, UNRESOLVED = (
    ImageDisposition.INCLUDED,
    ImageDisposition.EXCLUDED,
    ImageDisposition.UNRESOLVED,
)
NON_HTTPS_EXCLUSION = ImageDecision(DETERMINATE, EXCLUDED, ImageExclusion.SOURCE_AUTHORED_NON_HTTPS)
STILL_UNRESOLVED = ImageDecision(INDETERMINATE, UNRESOLVED)


def seen(role: ImageRole, ordinal: int) -> ImageReference:
    """A reference whose bytes were observed and stored."""
    return ImageReference(
        role=role,
        ordinal=ordinal,
        host=HOST,
        provenance=f".img:nth-of-type({ordinal})",
        status=CONFIRMED,
        sha256=SHA,
        locator=f"https://{HOST}/p/{ordinal}.png",
        source_form=LocatorForm.ABSOLUTE,
        source_trimmed=False,
    )


def refused(
    role: ImageRole,
    ordinal: int,
    refusal: FetchTargetRefusal = FetchTargetRefusal.NON_HTTPS,
    *,
    form: LocatorForm | None = LocatorForm.ABSOLUTE,
    trimmed: bool | None = False,
    issue: ImageIssue = ImageIssue.FETCH_FAILED,
) -> ImageReference:
    """A reference the transport's target check refused before anything was reserved or sent."""
    return ImageReference(
        role=role,
        ordinal=ordinal,
        host=HOST,
        provenance=f".img:nth-of-type({ordinal})",
        status=REVIEW,
        issue=issue,
        source_form=form,
        source_trimmed=None if form is None else trimmed,
        target_refusal=refusal,
    )


def failed(role: ImageRole, ordinal: int, issue: ImageIssue) -> ImageReference:
    """A reference whose target was allowed, but whose bytes did not become an image."""
    return ImageReference(
        role=role,
        ordinal=ordinal,
        host=HOST,
        provenance=f".img:nth-of-type({ordinal})",
        status=REVIEW,
        issue=issue,
        locator=f"https://{HOST}/p/{ordinal}.png",
        source_form=LocatorForm.ABSOLUTE,
        source_trimmed=False,
    )


def images_of(references: Iterable[ImageReference]) -> EvaluatedField:
    facts = evaluate(collected(images=tuple(references)))
    return next(field for field in facts.fields if field.key == "images")


def guards(field: EvaluatedField) -> list[str]:
    derived = (MISSING_REPRESENTATIVE_LOCATOR, MISSING_DETAIL_LOCATOR, EXCLUDED_SHARE_LOCATOR)
    return [e.evidence.locator for e in field.evidence if e.evidence.locator in derived]


# ---------------------------------------------------------------- the closed decision table


@pytest.mark.parametrize("trimmed", [False, True])
def test_1_an_absolute_http_reference_is_a_determinate_source_exclusion(trimmed: bool) -> None:
    # R2: the page wrote the scheme itself; the transport refused it before anything was sent.
    assert decide_image(refused(DETAIL, 16, trimmed=trimmed)) == NON_HTTPS_EXCLUSION


@pytest.mark.parametrize("form", [*LocatorForm, None])
@pytest.mark.parametrize("trimmed", [False, True])
def test_2_not_absolute_at_the_transport_boundary_is_never_excluded(
    form: LocatorForm | None, trimmed: bool
) -> None:
    # R3: a reference left unresolved at the transport can be a parser regression.
    reference = refused(DETAIL, 3, FetchTargetRefusal.NOT_ABSOLUTE, form=form, trimmed=trimmed)
    assert decide_image(reference) == STILL_UNRESOLVED


@pytest.mark.parametrize("form", [LocatorForm.RELATIVE, LocatorForm.PROTOCOL_RELATIVE, None])
def test_2_http_after_icbm_resolved_the_reference_is_not_the_sources_own(
    form: LocatorForm | None,
) -> None:
    # R3: a relative or protocol-relative reference was resolved by ICBM, and an unreported form
    # proves nothing about who wrote the scheme.
    assert decide_image(refused(DETAIL, 3, form=form)) == STILL_UNRESOLVED


@pytest.mark.parametrize(
    "reference",
    [
        refused(DETAIL, 4, issue=ImageIssue.BUDGET_EXHAUSTED),  # would have been refused, too
        failed(DETAIL, 4, ImageIssue.BUDGET_EXHAUSTED),
    ],
    ids=["refusable-but-budget-first", "plain"],
)
def test_3_budget_exhaustion_is_never_an_exclusion(reference: ImageReference) -> None:
    # R4: ICBM chose not to finish looking; that is never the source's defect.
    assert decide_image(reference) == STILL_UNRESOLVED


@pytest.mark.parametrize(
    "issue",
    [
        ImageIssue.FETCH_FAILED,  # network, timeout, server error: no refusal recorded
        ImageIssue.BAD_HOST,
        ImageIssue.BAD_CONTENT_TYPE,
        ImageIssue.OVERSIZE,
        ImageIssue.UNSUPPORTED_FORMAT,
    ],
)
def test_4_transient_network_and_content_failures_stay_unresolved(issue: ImageIssue) -> None:
    # R5: no response failure is proven to be the object's own under the current validators.
    assert decide_image(failed(DETAIL, 5, issue)) == STILL_UNRESOLVED


def test_observed_bytes_are_included() -> None:
    assert decide_image(seen(REP, 0)) == ImageDecision(DETERMINATE, INCLUDED)


def test_13_the_exclusion_table_is_closed_and_every_other_pair_stays_unresolved() -> None:
    # R11: one closed reason per row, no catch-all, and nothing outside the table is excluded.
    assert len(ImageExclusion) == len(EXCLUSION_TABLE)
    assert set(EXCLUSION_TABLE.values()) == set(ImageExclusion)
    assert not {"OTHER", "UNKNOWN", "MISC", "GENERIC"} & {member.name for member in ImageExclusion}
    assert dict(EXCLUSION_TABLE) == {
        (LocatorForm.ABSOLUTE, FetchTargetRefusal.NON_HTTPS): (
            ImageExclusion.SOURCE_AUTHORED_NON_HTTPS
        )
    }
    for form, refusal, trimmed in itertools.product(
        [*LocatorForm, None], FetchTargetRefusal, [False, True]
    ):
        decision = decide_image(refused(DETAIL, 1, refusal, form=form, trimmed=trimmed))
        expected = (form, refusal) in EXCLUSION_TABLE
        assert (decision.disposition is EXCLUDED) is expected, (form, refusal, trimmed)
        if not expected:
            assert decision == STILL_UNRESOLVED, (form, refusal, trimmed)


def test_13_acceptance_never_judges_a_url_itself() -> None:
    # R13: the transport is the one judge of a fetch target; acceptance reads what it recorded.
    source = inspect.getsource(facts_module.decide_image) + inspect.getsource(
        facts_module._images_field
    )
    for forbidden in ("urlsplit", "urljoin", "urlparse", "sanitize", "check_target", "image_fetch"):
        assert forbidden not in source


# ---------------------------------------------------------------- the images field


def test_5_one_excluded_detail_among_confirmed_images_may_confirm() -> None:
    # A 13-reference page, prospectively: 2 representative + 10 detail observed, 1 detail written
    # as http. 1/13 excluded, nothing unresolved, both roles included: R8.
    details = [seen(DETAIL, ordinal) for ordinal in (10, 17, 18, 19, 20, 21, 22, 23, 24, 25)]
    references = [seen(REP, 0), seen(REP, 9), *details, refused(DETAIL, 16)]
    assert len(references) == 13
    field = images_of(references)
    assert field.status is CONFIRMED and guards(field) == []
    excluded = [e.evidence for e in field.evidence if e.evidence.status is ABSENT]
    assert [(e.normalized, e.observed) for e in excluded] == [
        ("DETAIL:16:EXCLUDED:SOURCE_AUTHORED_NON_HTTPS", None)
    ]
    value = ImagesValue.model_validate_json(field.value_json or "")
    assert value.acceptance == IMAGE_ACCEPTANCE_MODEL
    by_position = {(s.role, s.ordinal): s for s in value.references}
    assert by_position[(DETAIL, 16)].disposition is EXCLUDED
    assert by_position[(DETAIL, 16)].status is REVIEW, "the observation itself is not rewritten"
    assert evaluate(collected(images=tuple(references))).facts_status is FactsStatus.CONFIRMED


@pytest.mark.parametrize(
    "references",
    [
        [seen(REP, 0), seen(DETAIL, 1), refused(DETAIL, 2), refused(DETAIL, 3)],  # 2/4
        [seen(REP, 0), seen(DETAIL, 1), seen(DETAIL, 2), refused(DETAIL, 3), refused(DETAIL, 4)],
    ],
    ids=["two-of-four", "two-of-five"],
)
def test_6_more_than_a_third_excluded_keeps_images_under_review(
    references: list[ImageReference],
) -> None:
    # R9: two of five is 0.4 — over a third, and under a half.
    field = images_of(references)
    assert field.status is REVIEW
    assert guards(field) == [EXCLUDED_SHARE_LOCATOR]
    marker = next(
        e.evidence for e in field.evidence if e.evidence.locator == EXCLUDED_SHARE_LOCATOR
    )
    assert marker.normalized == f"EXCLUDED:2/{len(references)}"


@pytest.mark.parametrize(
    "references",
    [
        [seen(REP, 0), seen(DETAIL, 1), refused(DETAIL, 2)],  # 1/3
        [
            seen(REP, 0),
            seen(DETAIL, 1),
            seen(DETAIL, 2),
            seen(DETAIL, 3),
            refused(DETAIL, 4),
            refused(DETAIL, 5),
        ],  # 2/6
    ],
    ids=["one-of-three", "two-of-six"],
)
def test_7_exactly_a_third_does_not_fire_the_drift_guard(references: list[ImageReference]) -> None:
    assert Fraction(1, 3) == EXCLUDED_SHARE_LIMIT
    field = images_of(references)
    assert field.status is CONFIRMED and guards(field) == []


@pytest.mark.parametrize(
    "unresolved",
    [
        failed(DETAIL, 2, ImageIssue.FETCH_FAILED),
        failed(DETAIL, 2, ImageIssue.BUDGET_EXHAUSTED),
        refused(DETAIL, 2, FetchTargetRefusal.NOT_ABSOLUTE, form=LocatorForm.RELATIVE),
        refused(DETAIL, 2, FetchTargetRefusal.HOST_NOT_ALLOWLISTED),
    ],
    ids=["network", "budget", "not-absolute", "host-not-allowlisted"],
)
def test_8_any_unresolved_reference_keeps_images_under_review(
    unresolved: ImageReference,
) -> None:
    # R7: every other guard holds — both roles included, nothing excluded — and it is still not
    # enough, because one reference could not be settled.
    field = images_of([seen(REP, 0), seen(DETAIL, 1), unresolved, seen(DETAIL, 3)])
    assert field.status is REVIEW
    assert guards(field) == []
    assert [e.evidence.normalized for e in field.evidence if e.evidence.status is REVIEW] == [
        "DETAIL:2"
    ]


def test_9_without_an_included_representative_images_stay_under_review() -> None:
    # R6: the only representative was excluded. Nothing is unresolved and 1/4 is under the guard.
    field = images_of([refused(REP, 0), seen(DETAIL, 1), seen(DETAIL, 2), seen(DETAIL, 3)])
    assert field.status is REVIEW
    assert guards(field) == [MISSING_REPRESENTATIVE_LOCATOR]


def test_10_a_source_that_exposed_detail_needs_an_included_detail() -> None:
    # R6/R12: the source exposed a detail reference; its exclusion does not erase that it did.
    field = images_of([seen(REP, 0), seen(REP, 1), seen(REP, 2), refused(DETAIL, 3)])
    assert field.status is REVIEW
    assert guards(field) == [MISSING_DETAIL_LOCATOR]


@pytest.mark.parametrize(
    "references",
    [
        [seen(REP, 0)],
        [seen(REP, 0), seen(REP, 1), refused(REP, 2)],  # 1/3 excluded, still no detail at all
    ],
    ids=["representative-only", "with-an-excluded-representative"],
)
def test_11_a_source_with_no_detail_at_all_may_still_confirm(
    references: list[ImageReference],
) -> None:
    field = images_of(references)
    assert field.status is CONFIRMED and guards(field) == []


def test_12_every_excluded_position_counts_even_with_one_shared_reason() -> None:
    # R10: two references refused for the same reason are two exclusions — 2/5, not 1/5.
    references = [
        seen(REP, 0),
        seen(DETAIL, 1),
        seen(DETAIL, 2),
        refused(DETAIL, 3),
        refused(DETAIL, 4),
    ]
    reasons = {decide_image(ref).exclusion for ref in references if not ref.sha256}
    assert reasons == {ImageExclusion.SOURCE_AUTHORED_NON_HTTPS}, "one reason, repeated"
    field = images_of(references)
    assert field.status is REVIEW
    assert guards(field) == [EXCLUDED_SHARE_LOCATOR]
    marker = next(
        e.evidence for e in field.evidence if e.evidence.locator == EXCLUDED_SHARE_LOCATOR
    )
    assert marker.normalized == "EXCLUDED:2/5"


def test_a_source_with_no_image_at_all_is_still_absent() -> None:
    field = images_of([])
    assert (field.status, field.value_json, field.evidence) == (ABSENT, None, ())


# ---------------------------------------------------------------- the recorded decision


@pytest.mark.parametrize(
    ("summary", "valid"),
    [
        (
            {"status": CONFIRMED, "sha256": SHA, "certainty": DETERMINATE, "disposition": INCLUDED},
            True,
        ),
        (
            {
                "status": REVIEW,
                "sha256": None,
                "certainty": DETERMINATE,
                "disposition": EXCLUDED,
                "exclusion": ImageExclusion.SOURCE_AUTHORED_NON_HTTPS,
            },
            True,
        ),
        (
            {
                "status": REVIEW,
                "sha256": None,
                "certainty": INDETERMINATE,
                "disposition": UNRESOLVED,
            },
            True,
        ),
        ({"status": REVIEW, "sha256": None}, True),  # recorded before the model: never classified
        ({"status": REVIEW, "sha256": None, "certainty": INDETERMINATE}, False),  # half a decision
        (
            {"status": REVIEW, "sha256": None, "certainty": DETERMINATE, "disposition": UNRESOLVED},
            False,
        ),
        (
            {"status": REVIEW, "sha256": None, "certainty": INDETERMINATE, "disposition": EXCLUDED},
            False,
        ),
        (
            {"status": REVIEW, "sha256": None, "certainty": DETERMINATE, "disposition": EXCLUDED},
            False,
        ),
        (
            {"status": REVIEW, "sha256": None, "certainty": DETERMINATE, "disposition": INCLUDED},
            False,
        ),
        (
            {
                "status": CONFIRMED,
                "sha256": SHA,
                "certainty": DETERMINATE,
                "disposition": INCLUDED,
                "exclusion": ImageExclusion.SOURCE_AUTHORED_NON_HTTPS,
            },
            False,
        ),
        (
            {
                "status": REVIEW,
                "sha256": None,
                "exclusion": ImageExclusion.SOURCE_AUTHORED_NON_HTTPS,
            },
            False,
        ),
    ],
)
def test_a_recorded_decision_is_one_the_table_could_have_made(
    summary: dict[str, object], valid: bool
) -> None:
    build = {"role": DETAIL, "ordinal": 1, **summary}
    if valid:
        ImageSummary(**build)  # type: ignore[arg-type]
    else:
        with pytest.raises(ValidationError):
            ImageSummary(**build)  # type: ignore[arg-type]


def test_a_revision_recorded_before_the_model_reads_back_unclassified() -> None:
    # Ruling test 15: history is read as it was written, never reclassified.
    legacy = json.dumps(
        {
            "references": [
                {"role": "REPRESENTATIVE", "ordinal": 0, "sha256": SHA, "status": "CONFIRMED"},
                {"role": "DETAIL", "ordinal": 16, "sha256": None, "status": "REVIEW_REQUIRED"},
            ]
        }
    )
    value = ImagesValue.model_validate_json(legacy)
    assert value.acceptance is None
    assert {(s.certainty, s.disposition, s.exclusion) for s in value.references} == {
        (None, None, None)
    }


@pytest.mark.parametrize(
    "value",
    [
        {  # decided references, no model named
            "references": [
                {
                    "role": "REPRESENTATIVE",
                    "ordinal": 0,
                    "sha256": SHA,
                    "status": "CONFIRMED",
                    "certainty": "DETERMINATE",
                    "disposition": "INCLUDED",
                },
            ]
        },
        {  # a model named over references it did not decide
            "acceptance": "image-acceptance/v1",
            "references": [
                {"role": "REPRESENTATIVE", "ordinal": 0, "sha256": SHA, "status": "CONFIRMED"}
            ],
        },
        {  # only part of the references decided
            "acceptance": "image-acceptance/v1",
            "references": [
                {
                    "role": "REPRESENTATIVE",
                    "ordinal": 0,
                    "sha256": SHA,
                    "status": "CONFIRMED",
                    "certainty": "DETERMINATE",
                    "disposition": "INCLUDED",
                },
                {"role": "DETAIL", "ordinal": 1, "sha256": None, "status": "REVIEW_REQUIRED"},
            ],
        },
        {  # an unknown model
            "acceptance": "image-acceptance/v0",
            "references": [
                {"role": "REPRESENTATIVE", "ordinal": 0, "sha256": SHA, "status": "CONFIRMED"}
            ],
        },
    ],
    ids=["no-model", "model-without-decisions", "half-decided", "unknown-model"],
)
def test_an_images_value_is_decided_under_one_model_or_none(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ImagesValue.model_validate_json(json.dumps(value))
