"""Gate 3 area 3: the judgment of one measured surface (``scripts.g3visual.checker.judge``)."""

from typing import Any

import pytest

from app.live import visual
from scripts.g3visual.checker import judge

REGISTER = next(t for t in visual.REQUIRED_TARGETS if t.name == "register")
BLOCKER = '.register-preflight[data-preflight="REVIEW_REQUIRED"]'


def clean() -> dict[str, Any]:
    return {
        "rendered": True,
        "populated": dict.fromkeys(REGISTER.populated, 1),
        "document_overflow": 0,
        "states": [{"at": BLOCKER}, {"at": 'span.chip[data-grant-state="ACTIVE"]'}],
        "overlaps": [],
        "title_help_icons": 1,
        "unlabeled_help_icons": [],
        "nav_items": 10,
        "active_nav": "register",
    }


def failing(measurement: dict[str, Any], **kwargs: Any) -> dict[str, list[str]]:
    values = {"console_errors": [], "external_requests": 0}
    values.update(kwargs)
    checks = judge(measurement, REGISTER, **values)
    assert tuple(checks) == visual.REQUIRED_CHECKS
    return {name: out["failures"] for name, out in checks.items() if not out["passed"]}


def test_a_clean_surface_passes_every_check() -> None:
    assert failing(clean()) == {}


@pytest.mark.parametrize(
    ("change", "check"),
    [
        ({"states": [{"at": BLOCKER, "hidden": "no-box"}]}, "state_visible"),
        ({"states": [{"at": BLOCKER, "hidden": "visibility"}]}, "state_visible"),
        ({"states": [{"at": BLOCKER, "clipped": "y:div.panel"}]}, "state_visible"),
        ({"states": [{"at": BLOCKER, "truncated": True}]}, "state_not_truncated"),
        ({"states": [{"at": BLOCKER, "covered": "div.toast"}]}, "state_not_covered"),
        ({"overlaps": [f"{BLOCKER} ~ span.chip"]}, "state_not_covered"),
        ({"states": [{"at": BLOCKER, "offscreen": True}]}, "no_horizontal_overflow"),
        ({"states": [{"at": BLOCKER, "clipped": "x:div.db-list"}]}, "no_horizontal_overflow"),
        ({"document_overflow": 24}, "no_horizontal_overflow"),
        ({"rendered": False}, "rendered"),
        ({"populated": {}}, "populated"),
        ({"title_help_icons": 2}, "title_help_icon"),
        ({"title_help_icons": 0}, "title_help_icon"),
        ({"unlabeled_help_icons": ["span.help-icon"]}, "title_help_icon"),
        ({"nav_items": 9}, "navigation_intact"),
        ({"active_nav": "db"}, "navigation_intact"),
    ],
)
def test_each_hidden_or_broken_state_fails_its_check(change: dict[str, Any], check: str) -> None:
    measurement = clean() | change
    found = failing(measurement)
    assert check in found, found
    if "states" in change:
        assert any(BLOCKER in failure for failure in found[check])


def test_console_errors_and_external_requests_fail() -> None:
    assert "no_console_error" in failing(clean(), console_errors=["TypeError: x"])
    assert failing(clean(), external_requests=1) == {"no_external_request": ["external-requests:1"]}


def test_the_harness_opens_every_required_surface_at_every_required_viewport() -> None:
    """The run cannot silently drop a surface or a viewport: it iterates the contract itself, and
    the scenario routes exactly the contract's surfaces to their own screens."""
    import inspect

    from scripts.g3visual import harness
    from scripts.g3visual.scenario import Populated

    routes = Populated("acct", "product", "draft", "prep", "run-1", "run-2").routes()
    assert set(routes) == {t.name for t in visual.REQUIRED_TARGETS}
    for target in visual.REQUIRED_TARGETS:
        assert routes[target.name].startswith(f"#/{target.screen}"), target
    browse = inspect.getsource(harness._browse)
    assert "visual.REQUIRED_VIEWPORTS.items()" in browse
    assert "for target in visual.REQUIRED_TARGETS" in browse
    assert "list(visual.STATE_SELECTORS)" in browse
    run = inspect.getsource(harness.run)
    assert '"state_selectors": list(visual.STATE_SELECTORS)' in run


def test_the_run_claims_only_a_fresh_dedicated_root(tmp_path: Any) -> None:
    from scripts.g3visual.harness import RootRefused, claim_root
    from scripts.m4accept.root import REPO_ROOT

    used = tmp_path / "used"
    used.mkdir()
    (used / "left-over").write_text("x")
    for root in (REPO_ROOT, REPO_ROOT / "var" / "g3", used, tmp_path / "m3-accept-01"):
        with pytest.raises(RootRefused):
            claim_root(root)
    with pytest.raises(RootRefused):
        claim_root(type(tmp_path)("relative-root"))
    assert claim_root(tmp_path / "fresh").is_dir()
