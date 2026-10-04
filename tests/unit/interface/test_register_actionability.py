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
        ("M4_BASE.IMAGE_SELECTION_MISSING", (Actionability.NOT_IMPLEMENTED, None)),
        ("CATEGORY_NOT_SELECTED", (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_PREPARATION)),
        ("PAYLOAD_EXTERNAL_URL", (Actionability.FIX_AVAILABLE, FixSurface.REGISTER_PREPARATION)),
        # B-DETAIL's reason is consumed as it is: no screen places a detail image.
        ("PUBLICATION_DETAIL_IMAGES_UNPLACED", (Actionability.NOT_IMPLEMENTED, None)),
        # A price pin is re-pinned only by the pricing owner (B-PRICE1 has no command yet).
        ("DRAFT_PRICE_PIN_SUPERSEDED", (Actionability.NOT_IMPLEMENTED, None)),
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


def test_every_value_has_a_label() -> None:
    assert set(ACTIONABILITY_LABELS) == set(Actionability)
    assert set(SURFACE_LABELS) == set(FixSurface)


def test_the_table_reads_no_owner() -> None:
    tree = ast.parse(Path(actionability.__file__).read_text("utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert all(not name.startswith("app.") for name in imported), imported
