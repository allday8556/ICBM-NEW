"""Representative visual checks of the M0 UI against the approved v29 prototype (ADR-0003).

Renders every top-level screen of the running application and of the v29 prototype — the latter
in its own approved zero-data mode (``window.ICBMUIEmpty``) — at a landscape and a portrait
viewport with the locally installed Microsoft Edge (Playwright ``channel="msedge"``; nothing is
downloaded). It saves screenshots, measures shell geometry in both, and records every network
request and console error the M0 pages produce.

Exit status is non-zero when the M0 UI loads anything from outside its own origin, logs a console
error, scrolls horizontally, or renders a page title without exactly one help icon. Geometry
differences are reported, not failed: ADR-0003 requires them to be documented.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
V29 = REPO_ROOT / "ui" / "prototypes" / "icbm_redesign_test_v29_final.html"
SCREENS = [
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
]
EXTRA_VIEWS = {"collect-suppliers": ("collect", {"view": "suppliers"})}
VIEWPORTS = {"landscape-1920x1080": (1920, 1080), "portrait-1080x1920": (1080, 1920)}

MEASURE = """(sel) => {
  const box = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
  };
  const title = document.querySelector(sel.title);
  const style = title ? getComputedStyle(title) : null;
  const active = document.querySelector(sel.activeNav);
  return {
    sidebar: box(document.querySelector('.sidebar')),
    topbar: box(document.querySelector('.topbar')),
    title: box(title),
    title_font: style ? [style.fontSize, style.fontWeight] : null,
    title_text: title ? title.firstChild.textContent.trim() : null,
    title_help_icons: title ? title.querySelectorAll(sel.helpIcon).length : null,
    empty: box(document.querySelector(sel.empty)),
    nav_items: document.querySelectorAll(sel.navItem).length,
    active_nav: active ? (active.getAttribute('aria-label') || active.textContent).trim() : null,
    horizontal_scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  };
}"""

SELECTORS = {
    "v29": {
        "title": ".page:not(.hidden) h1",
        "helpIcon": ".icbm-help-icon, .ui-help-icon",
        "empty": ".page:not(.hidden) .icbm-empty-state:not(.hidden)",
        "navItem": "#nav button",
        "activeNav": "#nav button.active",
    },
    "m0": {
        "title": "#content h1",
        "helpIcon": ".help-icon",
        "empty": "#content .empty-state",
        "navItem": "#nav .nav-item",
        "activeNav": "#nav .nav-item[aria-current='page']",
    },
}


def _query(params: dict[str, str]) -> str:
    return "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""


def _open_v29(page: Page, screen: str, params: dict[str, str]) -> None:
    page.goto(f"{V29.as_uri()}#/{screen}")
    page.wait_for_timeout(700)
    if params.get("view") == "suppliers":
        page.click('#collectInnerTabs [data-collect-view="suppliers"]')
    elif screen != "settings":
        page.evaluate("s => window.ICBMUIEmpty.set(s, true)", screen)
    page.wait_for_timeout(300)


def _open_m0(page: Page, base_url: str, screen: str, params: dict[str, str]) -> None:
    page.goto(f"{base_url}/#/{screen}{_query(params)}")
    page.wait_for_selector(f'#content[data-page="{screen}"]', timeout=15_000)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(250)


def _close(a: list[int] | None, b: list[int] | None, tolerance: int) -> bool:
    if a is None or b is None:
        return a is b
    return all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def compare(v29: dict[str, Any], m0: dict[str, Any]) -> dict[str, bool]:
    return {
        "sidebar": v29["sidebar"] == m0["sidebar"],
        "topbar": _close(v29["topbar"], m0["topbar"], 2),
        "title_font": v29["title_font"] == m0["title_font"],
        "title_box": _close(v29["title"], m0["title"], 3),
        "empty_state_box": _close(v29["empty"], m0["empty"], 4),
        "nav_items": v29["nav_items"] == m0["nav_items"],
        "single_help_icon": m0["title_help_icons"] == 1,
        "no_horizontal_scroll": not m0["horizontal_scroll"],
    }


def run(base_url: str, out_dir: Path, channel: str) -> dict[str, Any]:
    report: dict[str, Any] = {"base_url": base_url, "v29": str(V29.relative_to(REPO_ROOT))}
    failures: list[str] = []
    views = {screen: (screen, {}) for screen in SCREENS} | EXTRA_VIEWS
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        report["browser"] = f"{channel} {browser.version}"
        for viewport, (width, height) in VIEWPORTS.items():
            (out_dir / viewport).mkdir(parents=True, exist_ok=True)
            report[viewport] = {}
            for name, (screen, params) in views.items():
                entry: dict[str, Any] = {}
                for target in ("v29", "m0"):
                    page = browser.new_page(viewport={"width": width, "height": height})
                    requests: list[str] = []
                    errors: list[str] = []
                    page.on("request", lambda request, sink=requests: sink.append(request.url))
                    page.on(
                        "console",
                        lambda msg, sink=errors: (
                            sink.append(msg.text) if msg.type == "error" else None
                        ),
                    )
                    page.on("pageerror", lambda exc, sink=errors: sink.append(str(exc)))
                    if target == "v29":
                        _open_v29(page, screen, params)
                    else:
                        _open_m0(page, base_url, screen, params)
                    shot = out_dir / viewport / f"{name}__{target}.jpg"
                    page.screenshot(path=str(shot), type="jpeg", quality=72)
                    entry[target] = page.evaluate(MEASURE, SELECTORS[target])
                    entry[target]["screenshot"] = str(shot.relative_to(out_dir)).replace("\\", "/")
                    if target == "m0":
                        external = [u for u in requests if not u.startswith((base_url, "data:"))]
                        entry["m0"]["requests"] = len(requests)
                        entry["m0"]["external_requests"] = external
                        entry["m0"]["console_errors"] = errors
                        if external:
                            failures.append(f"{viewport}/{name}: external requests {external}")
                        if errors:
                            failures.append(f"{viewport}/{name}: console errors {errors}")
                    page.close()
                entry["match"] = compare(entry["v29"], entry["m0"])
                for check in ("single_help_icon", "no_horizontal_scroll"):
                    if not entry["match"][check]:
                        failures.append(f"{viewport}/{name}: {check} failed")
                report[viewport][name] = entry
        browser.close()
    report["failures"] = failures
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8790")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "var" / "visual")
    parser.add_argument("--channel", default="msedge")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = run(args.base_url.rstrip("/"), args.out, args.channel)
    (args.out / "visual-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for viewport in VIEWPORTS:
        print(f"\n## {viewport}")
        for name, entry in report[viewport].items():
            mismatched = [k for k, ok in entry["match"].items() if not ok]
            print(
                f"  {name:<18} {'match' if not mismatched else 'differs: ' + ', '.join(mismatched)}"
            )
    print(f"\nfailures: {report['failures'] or 'none'}")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
