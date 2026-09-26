"""Gate 3 area 3: the in-browser measurement catches every way a server-owned state is hidden.

A deterministic fixture page shaped like the shell (navigation, ``#content``, a titled page with
one help icon) is measured in a real browser. A clean page passes every check; each defect — a
truncated reason, a covered blocker, a hidden state, a horizontally clipped or off-screen state, a
document that scrolls sideways, a missing help icon — fails exactly the check that names it.
"""

import sys
from collections.abc import Iterator
from typing import Any

import pytest

from app.live import visual
from scripts.g3visual.checker import MEASURE, judge

pytestmark = pytest.mark.integration

BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
REGISTER = next(t for t in visual.REQUIRED_TARGETS if t.name == "register")
NAV = "".join(
    f'<button class="nav-item" data-page="{key}"'
    + (' aria-current="page"' if key == "register" else "")
    + f">{key}</button>"
    for key in (
        "dashboard",
        "collect",
        "db",
        "register",
        "orders",
        "inquiry",
        "soldout",
        "ai-insight",
        "analytics",
        "settings",
    )
)
POPULATED = """
<section class="register-unit" data-unit="u1">
  <div class="register-preflight" data-preflight="REVIEW_REQUIRED">
    <div class="mini" data-reason="AUTHORING_REVISIONS_UNOWNED">authoring revisions unowned</div>
  </div>
  <div class="register-scope" data-scope-state="PAUSED"><span class="chip">AUTH</span></div>
</section>
<section data-role="register-review">review</section>
<section class="register-canary" data-canary="BLOCKED">canary</section>
<div data-role="live-brake" data-brake-state="ENGAGED"><span class="chip">ENGAGED</span></div>
<div data-role="live-grants"><span data-grant-state="ACTIVE">ACTIVE</span></div>
<ul data-role="live-proofs"><li data-proof="visual_acceptance_recorded">no</li></ul>
<select><option data-account="a1" data-reason="">account</option></select>
"""
DEFECTS = {
    "truncated": (
        '<div style="width:60px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"'
        ' data-reason="X">a reason far too long to fit in sixty pixels</div>',
        "state_not_truncated",
    ),
    "covered": (
        '<div style="position:relative"><span class="chip" data-state="BLOCKED">BLOCKED</span>'
        '<div style="position:absolute;inset:0;background:#fff"></div></div>',
        "state_not_covered",
    ),
    "hidden": (
        '<span class="chip" data-state="BLOCKED" style="visibility:hidden">B</span>',
        "state_visible",
    ),
    "clipped": (
        '<div style="width:200px;overflow-x:auto"><div style="width:900px">'
        '<span data-reason="Y" style="margin-left:600px">reason</span></div></div>',
        "no_horizontal_overflow",
    ),
    "sideways": (
        '<div style="width:3000px" data-state="WIDE">wide</div>',
        "no_horizontal_overflow",
    ),
}


def page_html(body: str, *, help_icons: int = 1) -> str:
    icons = '<span class="help-icon" data-help="h" aria-label="help">i</span>' * help_icons
    return (
        "<html><body style='margin:0'>"
        f"<nav id='nav'>{NAV}</nav>"
        f"<main><div id='content' data-page='register'><h1>register{icons}</h1>"
        f"{POPULATED}{body}</div></main></body></html>"
    )


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
        except Exception as error:  # pragma: no cover - depends on the host
            pytest.skip(f"no {BROWSER_CHANNEL} browser: {error}")
        yield launched
        launched.close()


def measure(browser: Any, html: str) -> dict[str, dict[str, Any]]:
    page = browser.new_page(viewport={"width": 1080, "height": 1920})
    try:
        page.set_content(html)
        found = page.evaluate(
            MEASURE, [list(visual.STATE_SELECTORS), list(REGISTER.populated), "register"]
        )
    finally:
        page.close()
    return judge(found, REGISTER, console_errors=[], external_requests=0)


def failed(checks: dict[str, dict[str, Any]]) -> set[str]:
    return {name for name, outcome in checks.items() if not outcome["passed"]}


def test_a_clean_populated_page_passes_every_check(browser: Any) -> None:
    checks = measure(browser, page_html(""))
    assert failed(checks) == set(), checks


@pytest.mark.parametrize("defect", sorted(DEFECTS))
def test_each_way_of_hiding_a_state_fails_its_check(browser: Any, defect: str) -> None:
    body, check = DEFECTS[defect]
    assert check in failed(measure(browser, page_html(body)))


def test_a_title_without_exactly_one_help_icon_fails(browser: Any) -> None:
    assert "title_help_icon" in failed(measure(browser, page_html("", help_icons=0)))
    assert "title_help_icon" in failed(measure(browser, page_html("", help_icons=2)))
