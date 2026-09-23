"""The Settings target-policy surface in the real UI (Gate 1 G1-A, ADR-0015 §2).

The page runs in the installed browser and every request it makes is answered in-process by the
application under test. Proven here:
- Settings reads the registration target policy, edits the supported fields, saves them, and the
  same durable policy comes back after a reload and after an application restart;
- the save bar says what the **server** says is editable: only the target-policy surface, while
  every general setting stays read-only and its save stays inert;
- a save the server refuses shows the server's reason and writes nothing.

No provider is reached: a target policy is local configuration.
"""

import contextlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from tests.conftest import LOCAL
from tests.product_support import raw
from tests.register_support import establish

pytestmark = pytest.mark.integration

BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
MARKETPLACE = "smartstore"
POLICY_TAB = f"{LOCAL}/#/settings?tab=smartstore&sub=policy"
COMMON_TAB = f"{LOCAL}/#/settings?tab=common"
FORWARDED = ("x-icbm-client", "content-type", "accept")
TABLES = (
    "registration_target_policies",
    "registration_target_policy_revisions",
    "registration_target_policy_current",
)

VALUES = {
    "taxonomy_revision": "taxonomy-ui-1",
    "sanitizer_profile_version": "sanitizer-ui-1",
    "fee_table_version": "fee-ui-1",
    "pricing_policy_version": "pricing-ui-1",
    "fee_rate": "0.055",
    "fee_fixed_krw": "0",
    "other_cost_rate": "0",
    "other_cost_fixed_krw": "0",
    "asset_profile": "asset-profile-ui-1",
    "min_images": "1",
    "max_images": "10",
}


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
        except PlaywrightError as exc:
            pytest.skip(f"no {BROWSER_CHANNEL} browser to launch here ({type(exc).__name__})")
        try:
            yield launched
        finally:
            launched.close()


@contextmanager
def _served(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@contextmanager
def _page(
    browser: Browser, client: TestClient, url: str, ready: str, writes: list[tuple[str, str]]
) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        if request.method != "GET":
            writes.append((request.method, parts.path))
        response = client.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(url)
        page.wait_for_selector(ready, timeout=15_000)
        yield page
    finally:
        page.close()


def _counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }


def _editor(account: str) -> str:
    return f".target-policy[data-account='{account}']"


def _fill(page: Page, account: str, values: dict[str, str]) -> None:
    editor = page.locator(_editor(account))
    for name, value in values.items():
        editor.locator(f"[data-policy-field='{name}']").fill(value)
    editor.locator("[data-policy-field='templates']").fill(
        "shipping=shipping-template-ui\nreturns=returns-template-ui"
    )
    for name in ("requires_representative", "duplicate_proof_required", "lookup_SELLER_CODE"):
        editor.locator(f"[data-policy-field='{name}']").check()


def _state(page: Page, account: str) -> tuple[str | None, str | None, str]:
    editor = page.locator(_editor(account))
    chip = editor.locator("[data-policy-state]")
    return (
        chip.get_attribute("data-policy-state"),
        editor.locator("[data-policy-history]").get_attribute("data-policy-history"),
        chip.inner_text().strip(),
    )


def test_settings_saves_the_target_policy_and_it_survives_reload_and_restart(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client:
        served: Container = client.app.state.container  # type: ignore[attr-defined]
        account = establish(served, config, MARKETPLACE, "uid-smartstore-1")
        with _page(browser, client, POLICY_TAB, _editor(account), writes) as page:
            state, history, _ = _state(page, account)
            assert (state, history) == ("none", "0")
            assert writes == []
            # The two server-owned references have no owner yet: shown, never authorable.
            editor = page.locator(_editor(account))
            for reference in ("category_mapping_revision", "detail_composition_revision"):
                assert editor.locator(f"[data-policy-field='{reference}']").count() == 0
                shown = editor.locator(f"[data-policy-reference='{reference}']")
                assert shown.locator("input, textarea, select").count() == 0
                assert "입력할 수 없습니다" in shown.inner_text()
            _fill(page, account, VALUES)
            page.locator(f"{_editor(account)} button[data-action='save-target-policy']").click()
            page.wait_for_selector(
                f"{_editor(account)} [data-policy-state='saved']", timeout=10_000
            )
            saved = _state(page, account)
            assert saved[:2] == ("saved", "1")
            assert "현재 리비전 #1" in saved[2]
            assert writes == [
                ("POST", f"/api/v1/settings/target-policies/{MARKETPLACE}/{account}/revisions")
            ]
            # A reload rebuilds the same state from the server, nothing kept in the page.
            page.reload()
            page.wait_for_selector(f"{_editor(account)} [data-policy-state='saved']")
            assert _state(page, account) == saved
            field = page.locator(f"{_editor(account)} [data-policy-field='fee_rate']")
            assert field.input_value() == "0.055"
        stored = served.target_policies.policy(MARKETPLACE, account)
        current = stored.current
        assert current is not None and stored.inputs is not None
        assert stored.inputs.category_mapping_revision is None
        assert stored.inputs.detail_composition_revision is None
    # A restart: a new process owns the same data directory and serves the same durable policy.
    with (
        _served(config) as client,
        _page(browser, client, POLICY_TAB, _editor(account), []) as page,
    ):
        page.wait_for_selector(f"{_editor(account)} [data-policy-state='saved']")
        assert _state(page, account) == saved
        restarted: Container = client.app.state.container  # type: ignore[attr-defined]
        target = restarted.registration_preflight.target_policy(MARKETPLACE, account)
        assert target is not None and target.policy_revision == current.policy_revision
        assert target.pricing_context.fee_rate == "0.055"
    assert _counts(config) == dict.fromkeys(TABLES, 1)


def test_the_save_bar_is_server_owned_and_general_settings_stay_read_only(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client, _page(browser, client, COMMON_TAB, ".savebar", writes) as page:
        chip = page.locator(".savebar .chip")
        assert chip.get_attribute("data-save-scope") == "REGISTRATION_TARGET_POLICY"
        text = chip.inner_text()
        assert "M0" not in text
        assert "일반 설정은 저장 계약이 없어 읽기 전용입니다" in text
        assert "등록 대상 정책" in text
        # Every general setting field is read-only, and the general save is inert.
        fields = page.locator("input[data-setting]")
        assert fields.count() > 0
        assert all(
            fields.nth(i).get_attribute("readonly") is not None for i in range(fields.count())
        )
        general_save = page.locator(".savebar-actions button", has_text="설정 저장")
        assert general_save.get_attribute("aria-disabled") == "true"
        # Even a forced click on it only explains itself: it reaches no contract.
        general_save.click(force=True)
        page.locator(".toast").first.wait_for(timeout=5_000)
        assert writes == []
    assert _counts(config) == dict.fromkeys(TABLES, 0)


def test_a_refused_save_shows_the_server_reason_and_writes_nothing(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client:
        served: Container = client.app.state.container  # type: ignore[attr-defined]
        account = establish(served, config, MARKETPLACE, "uid-smartstore-1")
        with _page(browser, client, POLICY_TAB, _editor(account), writes) as page:
            _fill(page, account, {**VALUES, "min_images": "5", "max_images": "2"})
            page.locator(f"{_editor(account)} button[data-action='save-target-policy']").click()
            toast = page.locator(".toast", has_text="TARGET_POLICY_INVALID")
            toast.wait_for(timeout=10_000)
            assert _state(page, account)[:2] == ("none", "0")
            # The typed values stay for a fix; nothing was promoted locally.
            field = page.locator(f"{_editor(account)} [data-policy-field='min_images']")
            assert field.input_value() == "5"
    assert len(writes) == 1
    assert _counts(config) == dict.fromkeys(TABLES, 0)
