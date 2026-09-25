"""DRAFT lint (ADR-0017 §7.1): deterministic findings that never refuse a DRAFT."""

from typing import Any

from app.collect.adaptive.lint import (
    COVERAGE_FIELD_WITHOUT_RULE,
    LINT_REVISION,
    NO_REPRESENTATIVE_IMAGE_ROLE,
    REPRESENTATIVE_REGION_MISSING,
    UNMAPPED_CORE_FIELD,
    draft_lint,
    lint_digest,
)
from tests.adaptive_support import bundle_of, synmart_bundle, template


def _without(key: str, *fields: str, choice: bool = False) -> dict[str, Any]:
    document = template(key, choice=choice)
    for field in fields:
        del document["fields"][field]
    return document


def test_a_fully_ruled_bundle_has_no_findings() -> None:
    assert draft_lint(synmart_bundle()) == ()


def test_findings_name_each_template_and_field_sorted_and_once() -> None:
    bundle = bundle_of(
        [_without("plain", "prices", "brand"), _without("choice", "brand", choice=True)]
    )
    assert draft_lint(bundle) == (
        f"{COVERAGE_FIELD_WITHOUT_RULE}:choice:brand",
        f"{COVERAGE_FIELD_WITHOUT_RULE}:plain:brand",
        f"{UNMAPPED_CORE_FIELD}:plain:prices",
    )
    assert draft_lint(bundle) == draft_lint(bundle)


def test_image_role_gaps_are_findings() -> None:
    detail_only = [{"region": "detail", "role": "DETAIL", "take": "ALL"}]
    assert draft_lint(bundle_of([template("plain", choice=False)], image_roles=detail_only)) == (
        NO_REPRESENTATIVE_IMAGE_ROLE,
    )
    elsewhere = [{"region": "gallery", "role": "REPRESENTATIVE", "take": "FIRST"}]
    assert draft_lint(bundle_of([template("plain", choice=False)], image_roles=elsewhere)) == (
        f"{REPRESENTATIVE_REGION_MISSING}:plain:gallery",
    )


def test_the_lint_digest_binds_the_rule_set() -> None:
    findings = (f"{UNMAPPED_CORE_FIELD}:plain:prices",)
    assert lint_digest(LINT_REVISION, findings) != lint_digest("adaptive-lint-0", findings)
    assert lint_digest(LINT_REVISION, findings) != lint_digest(LINT_REVISION, ())
