"""The image auto-selection rule, pure (Issue #219, owner decision 2026-10-03).

The reference case is product 350 of the E3 KM collection: the owner named exactly the images it
must produce — the representative image, the brand banner and the five product detail images, in
page order — and every other image is left out for a stated reason.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.container import supplier_image_roles
from app.stages.collect.facts import ImageRole
from app.stages.products.auto_images import (
    AUTO_QA_MAX_BYTES,
    MAX_ADDITIONAL_IMAGES,
    AutoSelectionBlocked,
    ImageSlot,
    PlacementNote,
    auto_qa,
    plan,
)
from app.stages.products.common_images import CommonImageVerdict
from app.stages.products.image_model import (
    OutputRole,
    QaVerdict,
    SourceDecisionKind,
    SourceRef,
)

_R, _D = ImageRole.REPRESENTATIVE, ImageRole.DETAIL
SLOTS = {
    "s.primary": ImageSlot.REPRESENTATIVE,
    "s.additional": ImageSlot.ADDITIONAL,
    "s.detail": ImageSlot.DETAIL,
    "s.aux": ImageSlot.AUXILIARY,
}


def sha(name: str) -> str:
    return (name.encode().hex() * 64)[:64]


PRODUCT = sha("product-photo")
NOTICE, BANNER, CONTACT = sha("notice"), sha("banner"), sha("contact")
DETAILS = [sha(f"detail-{n}") for n in range(5)]


def _case(*rows: tuple[ImageRole, int, str, str]) -> tuple[list[SourceRef], dict[Any, str]]:
    refs = [SourceRef(role, ordinal, digest) for role, ordinal, _name, digest in rows]
    names = {(role, ordinal): name for role, ordinal, name, _digest in rows}
    return refs, names


def product_350() -> tuple[list[SourceRef], dict[Any, str]]:
    """Product 350's CONFIRMED images: the key image twice, its additional copy, then the detail."""
    return _case(
        (_R, 0, "s.primary", PRODUCT),
        (_R, 2, "s.primary", PRODUCT),
        (_D, 3, "s.additional", PRODUCT),
        (_D, 10, "s.detail", NOTICE),
        (_D, 11, "s.detail", BANNER),
        *((_D, 12 + n, "s.detail", DETAILS[n]) for n in range(5)),
        (_D, 17, "s.detail", CONTACT),
    )


COMMON = {
    NOTICE: CommonImageVerdict.BLOCK,
    BANNER: CommonImageVerdict.KEEP,
    CONTACT: CommonImageVerdict.BLOCK,
}


def test_product_350_selects_exactly_the_images_the_owner_named() -> None:
    refs, names = product_350()
    found = plan(refs, names, SLOTS, COMMON)
    assert found.blocked is None
    assert [(o.role, o.source_role, o.source_ordinal) for o in found.outputs] == [
        (OutputRole.REPRESENTATIVE, _R, 0),
        (OutputRole.DETAIL, _D, 11),
        *((OutputRole.DETAIL, _D, 12 + n) for n in range(5)),
    ]
    notes = {(role, ordinal): note for role, ordinal, note in found.notes}
    assert notes[("REPRESENTATIVE", 2)] == PlacementNote.DUPLICATE_BYTES
    assert notes[("DETAIL", 3)] == PlacementNote.DUPLICATE_BYTES
    assert notes[("DETAIL", 10)] == PlacementNote.COMMON_IMAGE_BLOCKED
    assert notes[("DETAIL", 17)] == PlacementNote.COMMON_IMAGE_BLOCKED
    # Every CONFIRMED image is decided, and only the placed ones are used.
    assert len(found.decisions) == len(refs)
    used = [d for d in found.decisions if d.decision is SourceDecisionKind.USE_SOURCE]
    assert len(used) == len(found.outputs)


def test_an_undecided_common_image_is_left_out_like_a_blocked_one() -> None:
    refs, names = product_350()
    found = plan(refs, names, SLOTS, {**COMMON, BANNER: CommonImageVerdict.REVIEW})
    notes = {(role, ordinal): note for role, ordinal, note in found.notes}
    assert notes[("DETAIL", 11)] == PlacementNote.COMMON_IMAGE_UNDECIDED
    assert all(o.source_ordinal != 11 for o in found.outputs)


def test_additional_images_are_byte_distinct_in_page_order_up_to_the_gallery_bound() -> None:
    extra = [sha(f"additional-{n}") for n in range(MAX_ADDITIONAL_IMAGES + 2)]
    refs, names = _case(
        (_R, 0, "s.primary", PRODUCT),
        *((_D, 1 + n, "s.additional", digest) for n, digest in enumerate(extra)),
        (_D, 50, "s.additional", extra[0]),
    )
    found = plan(refs, names, SLOTS, {})
    additional = [o.source_ordinal for o in found.outputs if o.role is OutputRole.ADDITIONAL]
    assert additional == list(range(1, MAX_ADDITIONAL_IMAGES + 1))
    notes = {(role, ordinal): note for role, ordinal, note in found.notes}
    assert notes[("DETAIL", MAX_ADDITIONAL_IMAGES + 1)] == PlacementNote.OVER_LIMIT
    assert notes[("DETAIL", 50)] == PlacementNote.DUPLICATE_BYTES


def test_another_representative_and_an_auxiliary_image_are_left_for_the_operator() -> None:
    refs, names = _case(
        (_R, 0, "s.primary", PRODUCT),
        (_R, 1, "s.primary", sha("another-photo")),
        (_D, 2, "s.aux", sha("slide")),
    )
    found = plan(refs, names, SLOTS, {})
    assert [o.source_ordinal for o in found.outputs] == [0]
    notes = {(role, ordinal): note for role, ordinal, note in found.notes}
    assert notes[("REPRESENTATIVE", 1)] == PlacementNote.OTHER_SLOT
    assert notes[("DETAIL", 2)] == PlacementNote.OTHER_SLOT


@pytest.mark.parametrize(
    ("rows", "slots", "blocked"),
    [
        ([(_R, 0, "s.primary", PRODUCT)], None, AutoSelectionBlocked.ROLE_RULES_UNAVAILABLE),
        (
            [(_R, 0, "s.primary", PRODUCT), (_D, 1, "s.unknown", sha("x"))],
            SLOTS,
            AutoSelectionBlocked.ROLE_RULE_UNKNOWN,
        ),
        ([(_D, 1, "s.detail", sha("x"))], SLOTS, AutoSelectionBlocked.REPRESENTATIVE_MISSING),
    ],
)
def test_the_rule_selects_nothing_and_says_why(
    rows: list[tuple[ImageRole, int, str, str]],
    slots: dict[str, ImageSlot] | None,
    blocked: AutoSelectionBlocked,
) -> None:
    refs, names = _case(*rows)
    found = plan(refs, names, slots, {})
    assert found.blocked is blocked and found.outputs == () and found.decisions == ()


def test_the_km_table_is_the_suppliers_own_published_role_rules() -> None:
    table = supplier_image_roles()["kmretail"]
    assert table["km.primary.key_image"] is ImageSlot.REPRESENTATIVE
    assert table["km.primary.og_image"] is ImageSlot.REPRESENTATIVE
    assert table["km.thumbnail.additional"] is ImageSlot.ADDITIONAL
    assert table["km.detail.prd_detail"] is ImageSlot.DETAIL
    assert table["km.aux.photoslide"] is ImageSlot.AUXILIARY
    # A layout asset is no product image.
    assert "km.ui.footer" not in table


def _asset(**overrides: object) -> Any:
    values: dict[str, object] = {
        "mime_type": "image/jpeg",
        "byte_size": 1024,
        "width": 800,
        "height": 800,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("asset", "verdict", "findings"),
    [
        (_asset(), QaVerdict.PASS, ()),
        (None, QaVerdict.REVIEW_REQUIRED, ("AUTO_QA_BYTES_NOT_STORED",)),
        (_asset(mime_type="image/bmp"), QaVerdict.REVIEW_REQUIRED, ("AUTO_QA_MEDIA_TYPE",)),
        (_asset(byte_size=AUTO_QA_MAX_BYTES), QaVerdict.PASS, ()),
        (
            _asset(byte_size=AUTO_QA_MAX_BYTES + 1),
            QaVerdict.REVIEW_REQUIRED,
            ("AUTO_QA_BYTE_SIZE",),
        ),
        (_asset(width=0), QaVerdict.REVIEW_REQUIRED, ("AUTO_QA_DIMENSIONS",)),
    ],
)
def test_the_automatic_qa_reads_file_facts_only(
    asset: Any, verdict: QaVerdict, findings: tuple[str, ...]
) -> None:
    assert auto_qa(asset) == (verdict, findings)
