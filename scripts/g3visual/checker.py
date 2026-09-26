"""The in-browser measurement and its judgment (ADR-0018 §9; Gate 3 area 3).

``MEASURE`` runs in the rendered page. For every element that carries server-owned state
(``app.live.visual.STATE_SELECTORS``) inside ``#content`` it records whether the element is
**hidden** (no box, ``visibility``, ``opacity``), **truncated** (its own or a descendant's text cut
by an ``overflow`` clip or ellipsis), **clipped** (outside an ancestor's clip box horizontally, or
vertically by a non-scrolling clip), **off-screen** horizontally, **covered** (another element on
top of its visible centre once it is scrolled into view) or **overlapping** another state element
it is not nested with. It also records the document's horizontal overflow, the title help icon,
every help icon's label, the navigation and the populated selectors.

Every element is described by its tag, first class and ``data-*`` attributes only, never by its
text, so a report holds no product text, no URL and no secret. ``judge`` turns one measurement into
the contract's checks; it is pure and deterministic.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from app.live.visual import REQUIRED_CHECKS, STATE_SELECTORS, Target

NAV_ITEMS: Final = 10

# The function itself, without the file's leading comment lines (Playwright evaluates it as one
# function expression).
MEASURE: Final = "\n".join(
    line
    for line in Path(__file__).with_name("measure.js").read_text("utf-8").splitlines()
    if not line.startswith("//")
)


def _clipped(state: Mapping[str, Any], axis: str) -> bool:
    return str(state.get("clipped", "")).startswith(axis)


def _result(failures: Sequence[str]) -> dict[str, Any]:
    return {"passed": not failures, "failures": sorted(set(failures))}


def judge(
    measurement: Mapping[str, Any],
    target: Target,
    *,
    console_errors: Sequence[str],
    external_requests: int,
) -> dict[str, dict[str, Any]]:
    """The contract's checks for one surface at one viewport, from one measurement."""
    states = list(measurement.get("states") or [])
    populated = measurement.get("populated") or {}
    checks = {
        "rendered": _result([] if measurement.get("rendered") is True else ["not-rendered"]),
        "populated": _result(
            [
                s
                for s in target.populated
                if not isinstance(populated.get(s), int) or populated[s] < 1
            ]
        ),
        "no_console_error": _result(list(console_errors)),
        "no_external_request": _result(
            [] if external_requests == 0 else [f"external-requests:{external_requests}"]
        ),
        "no_horizontal_overflow": _result(
            ([] if measurement.get("document_overflow", 1) <= 0 else ["document"])
            + [s["at"] for s in states if s.get("offscreen")]
            + [
                f"{s['at']} {s['clipped']}"
                for s in states
                if str(s.get("clipped", "")).startswith("x:")
            ]
        ),
        "state_visible": _result(
            [f"{s['at']} {s['hidden']}" for s in states if s.get("hidden")]
            + [
                f"{s['at']} {s['clipped']}"
                for s in states
                if str(s.get("clipped", "")).startswith("y:")
            ]
        ),
        "state_not_truncated": _result([s["at"] for s in states if s.get("truncated")]),
        "state_not_covered": _result(
            [f"{s['at']} under {s['covered']}" for s in states if s.get("covered")]
            + list(measurement.get("overlaps") or [])
        ),
        "title_help_icon": _result(
            ([] if measurement.get("title_help_icons") == 1 else ["title-help-icon-count"])
            + list(measurement.get("unlabeled_help_icons") or [])
        ),
        "navigation_intact": _result(
            ([] if measurement.get("nav_items") == NAV_ITEMS else ["nav-items"])
            + ([] if measurement.get("active_nav") == target.screen else ["active-nav"])
        ),
    }
    assert tuple(checks) == REQUIRED_CHECKS
    return checks


__all__ = ["MEASURE", "STATE_SELECTORS", "judge"]
