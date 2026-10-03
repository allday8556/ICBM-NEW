"""B-UX1: the area classification of a readiness reason is total and server-owned.

Every registration-preflight reason and every M4 reason the preflight propagates has an area; a
code the table has not caught up with is UNCLASSIFIED with its real code, never dropped.
"""

import ast
import re
from pathlib import Path

import pytest

import app.stages.products.images as images
import app.stages.products.pricing as pricing
import app.stages.products.pricing_service as pricing_service
import app.stages.products.readiness as readiness
from app.stages.products.pricing import GuardReason
from app.stages.register import reason_areas
from app.stages.register.preparation import M4_BASE_PREFIX, M4_PRICING_PREFIX, REASON_CODES
from app.stages.register.reason_areas import (
    AREA_LABELS,
    M4_AREAS,
    REGISTER_AREAS,
    ReasonArea,
    areas,
    m4_areas,
)

REPO = Path(__file__).resolve().parents[3]


def test_every_registration_reason_has_an_area_and_no_more() -> None:
    assert set(REGISTER_AREAS) == set(REASON_CODES)
    for code, found in REGISTER_AREAS.items():
        assert found and ReasonArea.UNCLASSIFIED not in found, code
        assert areas(code) == found


def _m4_codes() -> set[str]:
    """A source scan of M4: every reason it can construct, by the constant it names."""
    constants = {**vars(images), **vars(pricing_service), **vars(readiness), **vars(pricing)}
    literal = re.compile(r"(?:Reason\(|_review\()\s*([A-Z][A-Z0-9_]*)\s*,?")
    found: set[str] = set()
    for path in (REPO / "app" / "stages" / "products").glob("*.py"):
        for name in literal.findall(path.read_text("utf-8")):
            value = constants.get(name)
            if isinstance(value, str):
                found.add(value)
    # Stated through a variable: a QA verdict, and the price guards.
    found.add(images.IMAGE_QA_REVIEW_REQUIRED)
    found |= {reason.value for reason in GuardReason}
    return found


def test_every_m4_reason_has_an_area() -> None:
    codes = _m4_codes()
    assert codes, "the scan found nothing"
    assert codes <= set(M4_AREAS), codes - set(M4_AREAS)
    for code in codes:
        for prefix in (M4_BASE_PREFIX, M4_PRICING_PREFIX):
            assert ReasonArea.UNCLASSIFIED not in areas(prefix + code), prefix + code


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("A_REASON_NOBODY_CLASSIFIED", (ReasonArea.UNCLASSIFIED,)),
        ("M4_BASE.A_NEW_M4_REASON", (ReasonArea.UNCLASSIFIED,)),
        ("M4_BASE.IMAGE_FINDING_BLURRY", (ReasonArea.IMAGES,)),
        ("M4_BASE.BLOCKED", (ReasonArea.SOURCE,)),
        ("M4_PRICING.STALE", (ReasonArea.PRICE,)),
        ("PUBLICATION_DETAIL_IMAGES_UNPLACED", (ReasonArea.IMAGES, ReasonArea.DETAIL)),
        ("AUTHORING_REVISIONS_UNOWNED", (ReasonArea.CATEGORY, ReasonArea.DETAIL)),
    ],
)
def test_the_classification_is_total_and_keeps_unknown_codes(
    code: str, expected: tuple[ReasonArea, ...]
) -> None:
    assert areas(code) == expected


def test_an_unprefixed_m4_reason_is_classified_by_its_layer() -> None:
    assert m4_areas("SOURCE_STOCK_SOLD_OUT") == (ReasonArea.SOURCE,)
    assert m4_areas("REVIEW_REQUIRED", layer_prefix=M4_PRICING_PREFIX) == (ReasonArea.PRICE,)
    assert m4_areas("REVIEW_REQUIRED") == (ReasonArea.UNCLASSIFIED,)


def test_every_area_has_a_label() -> None:
    assert set(AREA_LABELS) == set(ReasonArea)
    assert all(label.strip() for label in AREA_LABELS.values())


def test_the_table_imports_no_evaluating_owner() -> None:
    tree = ast.parse(Path(reason_areas.__file__).read_text("utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert all(not name.startswith("app.") for name in imported), imported
