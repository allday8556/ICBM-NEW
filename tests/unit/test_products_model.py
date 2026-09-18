"""The M4 PRODUCT DB vocabulary and the canonical composition signature (ADR-0013 §5)."""

import re
from pathlib import Path

import pytest

from app.products.model import (
    DEFAULT_SINGLE_UNIT,
    DEFAULT_SINGLE_UNIT_SIGNATURE,
    MEMBER_TRANSITIONS,
    SIGNATURE_VERSION,
    BindingKind,
    CompositionError,
    CompositionSpec,
    MemberStatus,
    MoveReason,
    canonical_structure,
    composition_signature,
)


def test_the_signature_is_structural_never_display_text() -> None:
    # "2x500ml", "500ml 2개" and "2 × 500.0 ml" are one structure, so one signature.
    spellings = [
        CompositionSpec(quantity=2, unit_amount="500", unit_code="ml"),
        CompositionSpec(quantity=2, unit_amount="500.0", unit_code="ml"),
        CompositionSpec(quantity=2, unit_amount="0500.000", unit_code="ml"),
    ]
    assert len({composition_signature(spec) for spec in spellings}) == 1
    assert re.fullmatch(r"[0-9a-f]{64}", composition_signature(spellings[0]))


def test_count_pack_and_unit_differences_stay_distinct_signatures() -> None:
    # Same total weight never flattens different count or pack structure (CLAUDE.md §6.2).
    distinct = [
        CompositionSpec(quantity=2, unit_amount="500", unit_code="ml"),
        CompositionSpec(quantity=1, unit_amount="1000", unit_code="ml"),
        CompositionSpec(quantity=1, unit_amount="500", unit_code="ml", pack_count=2),
        CompositionSpec(
            quantity=1, unit_amount="500", unit_code="ml", pack_count=1, units_per_pack=2
        ),
        CompositionSpec(quantity=2, unit_amount="500", unit_code="g"),
    ]
    assert len({composition_signature(spec) for spec in distinct}) == len(distinct)


def test_unknown_is_part_of_the_signature_and_nothing_is_guessed() -> None:
    default = CompositionSpec.default_single_unit()
    assert default == CompositionSpec(quantity=1)
    structure = canonical_structure(default)
    assert structure["unit_amount"] is None and structure["unit_code"] is None
    stated = CompositionSpec(quantity=1, unit_amount="90", unit_code="tablet")
    assert composition_signature(default) != composition_signature(stated)


def test_the_migration_freezes_the_default_single_unit_signature() -> None:
    # PR #82 review 5247426764 blocker 1: migration 0012 compares a BASE_PRODUCT Item with this
    # literal, so the literal and the computed signature can never drift apart.
    migration = (
        Path(__file__).resolve().parents[2]
        / "app/db/migrations/versions/0012_m4_product_foundation.py"
    ).read_text("utf-8")
    frozen = re.search(r'^_DEFAULT_SINGLE_UNIT_SIGNATURE = "([0-9a-f]{64})"$', migration, re.M)
    assert frozen is not None
    assert frozen.group(1) == DEFAULT_SINGLE_UNIT_SIGNATURE
    assert composition_signature(CompositionSpec(quantity=1)) == DEFAULT_SINGLE_UNIT_SIGNATURE
    assert CompositionSpec.default_single_unit() == DEFAULT_SINGLE_UNIT


def test_the_signature_version_is_recorded_in_the_structure() -> None:
    assert canonical_structure(CompositionSpec(quantity=1))["version"] == SIGNATURE_VERSION
    assert SIGNATURE_VERSION == "composition-signature/v1"


@pytest.mark.parametrize(
    "spec",
    [
        CompositionSpec(quantity=0),
        CompositionSpec(quantity=True),  # a bool is not a count
        CompositionSpec(quantity=1, unit_amount="500", unit_code="500 ml"),  # free text
        CompositionSpec(quantity=1, unit_amount="500", unit_code="ML"),
        CompositionSpec(quantity=1, unit_amount="500"),  # an amount without its unit
        CompositionSpec(quantity=1, unit_code="ml"),  # a unit without its amount
        CompositionSpec(quantity=1, unit_amount="-5", unit_code="ml"),
        CompositionSpec(quantity=1, unit_amount="abc", unit_code="ml"),
        CompositionSpec(quantity=1, unit_amount="NaN", unit_code="ml"),
        CompositionSpec(quantity=1, total_amount="1000"),  # a total needs its unit
        CompositionSpec(quantity=1, pack_count=0),
    ],
)
def test_a_structure_that_cannot_be_signed_is_refused(spec: CompositionSpec) -> None:
    with pytest.raises(CompositionError):
        composition_signature(spec)


def test_rejected_membership_is_final() -> None:
    assert (MemberStatus.CANDIDATE, MemberStatus.CONFIRMED) in MEMBER_TRANSITIONS
    assert (MemberStatus.CONFIRMED, MemberStatus.REJECTED) in MEMBER_TRANSITIONS
    assert not [t for t in MEMBER_TRANSITIONS if t[0] is MemberStatus.REJECTED]
    assert (MemberStatus.CONFIRMED, MemberStatus.CANDIDATE) not in MEMBER_TRANSITIONS


def test_the_vocabularies_hold_what_adr_0013_names() -> None:
    assert {kind.value for kind in BindingKind} == {"SOURCE_OFFER", "BASE_PRODUCT"}
    assert {reason.value for reason in MoveReason} == {
        "INITIAL",
        "NEWER_REVISION",
        "EXTRACTOR_CHANGED",
        "EXPLICIT_DECISION",
    }
