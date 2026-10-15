"""Gate 3 area 3: the in-browser measurement catches every way a server-owned state is hidden.

A deterministic fixture page shaped like the shell (navigation, ``#content``, a titled page with
one help icon) is measured in a real browser. A clean page passes every check; each defect — a
truncated reason, a covered blocker, a hidden state, a horizontally clipped or off-screen state, a
document that scrolls sideways, a missing help icon — fails exactly the check that names it.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from app.capabilities.live_safety import visual
from automation.acceptance.gate3_visual.harness.checker import MEASURE, judge
from tests.support.browser import BROWSER_CHANNEL, launch_browser

pytestmark = pytest.mark.integration

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
<section data-role="register-status"><table><tbody>
<tr data-intent="i1"><td><span class="chip" data-read-state="REGISTERING">등록중</span></td></tr>
<tr data-intent="i2"><td><span class="chip" data-read-state="REGISTERED">등록성공</span></td></tr>
<tr data-intent="i3"><td><b class="chip" data-read-state="RECHECK_REQUIRED">re</b></td></tr>
<tr data-intent="i4"><td><span class="chip" data-read-state="FAILED">등록실패</span></td></tr>
</tbody></table></section>
"""
# The global registration status card (ADR-0014 §28.5): outside #content, fixed at the lower right.
CARD = (
    "<aside id='registrationStatus' style='position:fixed;right:0;bottom:0;width:300px;"
    "height:90px;background:#fff;z-index:10'>"
    "<span class='chip' data-read-state='RECHECK_REQUIRED'>재확인필요 1</span></aside>"
)
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
    "under_card": (
        '<span class="chip" data-state="UNDER" style="position:fixed;right:20px;bottom:20px">'
        "UNDER</span>",
        "state_not_covered",
    ),
    "sideways": (
        '<div style="width:3000px" data-state="WIDE">wide</div>',
        "no_horizontal_overflow",
    ),
    "display_none": (
        '<span class="chip" data-state="BLOCKED" style="display:none">B</span>',
        "state_visible",
    ),
    # ADR-0018 §9 amendment (Issue #219 6185598957): a closed <details> is opened only when its own
    # summary is visible and uncovered; otherwise every state it collapses stays hidden.
    "details_summary_hidden": (
        '<details><summary style="display:none">more</summary>'
        '<span class="chip" data-state="BLOCKED">B</span></details>',
        "state_visible",
    ),
    "details_summary_invisible": (
        '<details><summary style="visibility:hidden">more</summary>'
        '<span class="chip" data-state="BLOCKED">B</span></details>',
        "state_visible",
    ),
    "details_summary_covered": (
        '<div style="position:relative"><details><summary>more</summary>'
        '<span class="chip" data-state="BLOCKED">B</span></details>'
        '<div style="position:absolute;inset:0;background:#fff"></div></div>',
        "state_visible",
    ),
    "details_inside_collapsed_details": (
        '<details><summary style="display:none">outer</summary><details><summary>inner</summary>'
        '<span class="chip" data-state="BLOCKED">B</span></details></details>',
        "state_visible",
    ),
    "display_none_inside_details": (
        "<details><summary>more</summary>"
        '<span class="chip" data-state="BLOCKED" style="display:none">B</span></details>',
        "state_visible",
    ),
}
# The register page's LIVE-safety states inside a collapsed details (PR #352): its summary is
# visible and uncovered, so the states are visible on demand and judged with the details open.
COLLAPSED = (
    "<details class='register-safety-details' style='overflow:hidden'><summary>세부 점검</summary>"
    "<ul class='requirement-list'>"
    "<li data-requirement='AUTH_READY'><span>auth</span><span class='chip'>미충족</span></li>"
    "<li data-missing='LIVE_SENDER_NOT_WIRED'>sender</li></ul>"
    "<table><tbody><tr data-grant='g1' data-grant-state='ACTIVE'><td>ASSET</td></tr></tbody>"
    "</table></details>"
)


def page_html(body: str, *, help_icons: int = 1) -> str:
    icons = '<span class="help-icon" data-help="h" aria-label="help">i</span>' * help_icons
    return (
        "<html><body style='margin:0'>"
        f"<nav id='nav'>{NAV}</nav>"
        f"<main><div id='content' data-page='register'><h1>register{icons}</h1>"
        f"{POPULATED}{body}</div></main>{CARD}</body></html>"
    )


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            launched = launch_browser(playwright)
        except Exception as error:  # pragma: no cover - depends on the host
            pytest.skip(f"no {BROWSER_CHANNEL} browser: {error}")
        yield launched
        launched.close()


def measure(browser: Any, html: str, *, probe: str = "null") -> dict[str, dict[str, Any]]:
    """The judged checks, plus ``probe`` (a page expression) evaluated after the measurement."""
    page = browser.new_page(viewport={"width": 1080, "height": 1920})
    try:
        page.set_content(html)
        found = page.evaluate(
            MEASURE, [list(visual.STATE_SELECTORS), list(REGISTER.populated), "register"]
        )
        after = page.evaluate(probe)
    finally:
        page.close()
    checks = judge(found, REGISTER, console_errors=[], external_requests=0)
    return checks | {"probe": {"passed": True, "failures": [], "value": after}}


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


def test_a_state_in_a_collapsed_details_with_a_visible_summary_is_visible_on_demand(
    browser: Any,
) -> None:
    """The details is opened and its states judged: they pass both visibility checks, and the
    report names each one as revealed. A closed details without a state stays closed."""
    plain = "<details id='plain'><summary>notes</summary><p>no state here</p></details>"
    checks = measure(
        browser,
        page_html(COLLAPSED + plain),
        probe="[document.querySelector('.register-safety-details').open,"
        " document.querySelector('#plain').open]",
    )
    assert failed(checks) == set(), checks
    assert checks["probe"]["value"] == [True, False]
    revealed = checks["state_visible"]["revealed_from_details"]
    assert revealed == checks["state_not_covered"]["revealed_from_details"]
    assert len(revealed) == 4, revealed
    assert all(r.endswith(" in details.register-safety-details") for r in revealed), revealed
    assert any(r.startswith('li[data-missing="LIVE_SENDER_NOT_WIRED"]') for r in revealed)
    assert any(r.startswith('tr[data-grant="g1"]') for r in revealed)


@pytest.mark.parametrize(
    "defect", ["details_summary_covered", "details_summary_hidden", "details_summary_invisible"]
)
def test_a_collapsed_details_whose_summary_is_not_shown_stays_closed(
    browser: Any, defect: str
) -> None:
    body, _ = DEFECTS[defect]
    checks = measure(browser, page_html(body), probe="document.querySelector('details').open")
    assert checks["probe"]["value"] is False
    assert checks["state_visible"]["failures"] == [
        'span.chip[data-state="BLOCKED"] closed-details:details'
    ], checks
    assert checks["state_visible"]["revealed_from_details"] == []
