"""B-UX2: the server-owned actionability table is total, honest and pure.

Every registration-preflight reason and every REGISTER execution review condition has an entry; a
fix names a real surface and only a fix does; an unknown code is NOT_IMPLEMENTED, never a fake fix.
"""

import ast
from pathlib import Path

import pytest

from app.capabilities.review import register_producer
from app.interface.screens import actionability
from app.interface.screens.actionability import (
    ACTIONABILITY_LABELS,
    REGISTER_ACTIONS,
    REVIEW_ACTIONS,
    SURFACE_LABELS,
    Actionability,
    FixSurface,
    m4_action,
    not_evaluated_action,
    preflight_action,
    review_action,
)
from app.stages.register.model import ScopePauseReason
from app.stages.register.preparation import REASON_CODES


def test_every_preflight_reason_has_an_actionability_and_no_more() -> None:
    assert set(REGISTER_ACTIONS) == set(REASON_CODES)


def test_every_execution_review_condition_has_an_actionability() -> None:
    codes = {
        register_producer.REGISTER_INTENT_UNKNOWN,
        register_producer.REGISTER_READBACK_MISMATCH,
        register_producer.REGISTER_VERIFICATION_OVERDUE,
        *(f"{register_producer.REGISTER_SCOPE_PAUSED_PREFIX}{r.value}" for r in ScopePauseReason),
    }
    assert codes == set(REVIEW_ACTIONS)


@pytest.mark.parametrize(
    "entry",
    [*REGISTER_ACTIONS.values(), *REVIEW_ACTIONS.values()],
)
def test_only_an_action_names_a_surface(entry: tuple[Actionability, FixSurface | None]) -> None:
    action, surface = entry
    if action in (Actionability.FIX_AVAILABLE, Actionability.RECHECK):
        assert surface is not None
    else:
        assert surface is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("A_REASON_NOBODY_MAPPED", (Actionability.NOT_IMPLEMENTED, None)),
        ("M4_BASE.SOURCE_STOCK_SOLD_OUT", (Actionability.NO_OPERATOR_ACTION, None)),
        # B-EDITOR: an image selection is made in the unit workspace's image section.
        (
            "M4_BASE.IMAGE_SELECTION_MISSING",
            (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_IMAGES),
        ),
        (
            "PUBLICATION_REPRESENTATIVE_MISSING",
            (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_IMAGES),
        ),
        ("CATEGORY_NOT_SELECTED", (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_PREPARATION)),
        ("PAYLOAD_EXTERNAL_URL", (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_PREPARATION)),
        # 2026-10-16: saving the preparation again takes the policy's current composition, which
        # places the detail images and the notices.
        (
            "PUBLICATION_DETAIL_IMAGES_UNPLACED",
            (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_PREPARATION),
        ),
        (
            "PUBLICATION_GUIDANCE_UNPLACED",
            (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_PREPARATION),
        ),
        (
            "PUBLICATION_ASSET_QA_NOT_PASSED",
            (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_IMAGES),
        ),
        # The write scope is the operator's own 등록 권한 확인 record in API 관리.
        ("CONNECT_WRITE_SCOPE_MISSING", (Actionability.FIX_AVAILABLE, FixSurface.SETTINGS_CONNECT)),
        # The option limits are the category's reviewed metadata.
        (
            "OPTIONS_NOT_SUPPORTED",
            (Actionability.FIX_AVAILABLE, FixSurface.SETTINGS_CATEGORY_METADATA),
        ),
        (
            "OPTION_COUNT_EXCEEDED",
            (Actionability.FIX_AVAILABLE, FixSurface.SETTINGS_CATEGORY_METADATA),
        ),
        (
            "OPTION_DIMENSIONS_EXCEEDED",
            (Actionability.FIX_AVAILABLE, FixSurface.SETTINGS_CATEGORY_METADATA),
        ),
        # A 구매대행 cost input is refreshed or set in the overseas settings.
        (
            "M4_PRICING.PRICING_FX_STALE",
            (Actionability.FIX_AVAILABLE, FixSurface.SETTINGS_OVERSEAS),
        ),
        (
            "M4_PRICING.PRICING_FX_UNAVAILABLE",
            (Actionability.FIX_AVAILABLE, FixSurface.SETTINGS_OVERSEAS),
        ),
        (
            "M4_PRICING.PRICING_OVERSEAS_SHIPPING_UNSET",
            (Actionability.FIX_AVAILABLE, FixSurface.SETTINGS_OVERSEAS),
        ),
        # B-PRICE1: a moved price is re-pinned by the unit's re-pin action.
        ("DRAFT_PRICE_PIN_SUPERSEDED", (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_ACTION)),
        (
            "M4_PRICING.PRICING_SNAPSHOT_SUPERSEDED",
            (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_ACTION),
        ),
        ("DRAFT_PRICE_PIN_MISSING", (Actionability.NOT_IMPLEMENTED, None)),
    ],
)
def test_preflight_actions(code: str, expected: tuple[Actionability, FixSurface | None]) -> None:
    assert preflight_action(code) == expected


def test_review_and_not_evaluated_actions() -> None:
    assert review_action("REGISTER_READ_VERIFICATION_OVERDUE") == (
        Actionability.RECHECK,
        FixSurface.REGISTER_ACTION,
    )
    # A preparation producer's condition is a preflight code.
    assert review_action("CATEGORY_NOT_SELECTED") == preflight_action("CATEGORY_NOT_SELECTED")
    assert not_evaluated_action("REGISTER_PREPARATION_ABSENT") == (
        Actionability.FIX_AVAILABLE,
        FixSurface.REGISTER_PREPARATION,
    )
    assert not_evaluated_action("SOMETHING_ELSE") == (Actionability.NOT_IMPLEMENTED, None)
    assert m4_action("SOURCE_STOCK_SOLD_OUT")[0] is Actionability.NO_OPERATOR_ACTION


def test_the_overseas_fixes_name_the_landed_cost_owners_own_codes() -> None:
    # The codes are the owners' own, never re-spelled here (ADR-0038 §3, §4).
    from app.stages.products import landed_cost

    for code in (landed_cost.FX_STALE, landed_cost.PRICING_FX_UNAVAILABLE):
        assert m4_action(code) == (Actionability.FIX_AVAILABLE, FixSurface.SETTINGS_OVERSEAS)
    assert m4_action(landed_cost.OVERSEAS_SHIPPING_UNSET) == (
        Actionability.FIX_AVAILABLE,
        FixSurface.SETTINGS_OVERSEAS,
    )


def test_every_value_has_a_label() -> None:
    assert set(ACTIONABILITY_LABELS) == set(Actionability)
    assert set(SURFACE_LABELS) == set(FixSurface)


def test_every_place_is_named_as_the_seller_finds_it() -> None:
    # 2026-10-15 jargon sweep: each place is the screen and tab the seller opens, never an
    # internal term ("단위", "메타데이터") and never a place the settings screen does not have.
    assert SURFACE_LABELS[FixSurface.REGISTER_ACTION] == "등록관리 › 상품 등록 작업"
    assert SURFACE_LABELS[FixSurface.SETTINGS_CONNECT] == "설정 › API 관리"
    assert SURFACE_LABELS[FixSurface.SETTINGS_CATEGORY_METADATA] == "설정 › 상품 / 카테고리"
    assert SURFACE_LABELS[FixSurface.SETTINGS_OVERSEAS] == "설정 › 공통 › 해외구매대행"
    for label in SURFACE_LABELS.values():
        assert "단위" not in label and "메타데이터" not in label, label
        assert label.isascii() is False, label


def test_the_table_reads_no_owner() -> None:
    tree = ast.parse(Path(actionability.__file__).read_text("utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert all(not name.startswith("app.") for name in imported), imported
