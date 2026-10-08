"""ADR-0026 AIF-1 in the real UI: Settings' AI Prompt Registry and its layered prompt editor.

The page runs in the installed browser and every request it makes is answered in-process by the
application under test. The registry shows the v29 entries with each one's server revision; the
editor opens on a role × policy × task with the prototype's seven tabs; a save and a reset write
exactly one revision each through the registry routes, and the preview is the server's
composition. No AI provider exists, and no request leaves the application.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route

from tests.conftest import LOCAL
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    browser,
    client,
)

pytestmark = pytest.mark.integration

FORWARDED = ("x-icbm-client", "content-type", "accept")
SETTINGS = f"{LOCAL}/#/settings?tab=common&sub=ai"
BUNDLE = "TASK_PRODUCT_RECOMMEND_BUNDLE_V1"
STATE = "document.querySelector('[data-role=prompt-state]')"
TEXT = "document.querySelector('[data-role=prompt-text]')"


@contextmanager
def _page(on: Browser, app: TestClient, writes: list[tuple[str, str]], url: str) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        if request.method != "GET":
            writes.append((request.method, parts.path))
        response = app.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = on.new_page()
    page.route("**/*", answer)
    try:
        page.goto(url)
        yield page
    finally:
        page.close()


def _card(page: Page, key: str) -> str:
    return page.locator(f".registry-grid [data-prompt-key='{key}'] em").inner_text().strip()


def test_the_registry_edits_one_layer_at_a_time_and_previews_the_composition(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes, SETTINGS) as page:
        registry = page.locator("[data-role='prompt-registry']")
        registry.locator(".registry-grid.tasks .registry-item").first.wait_for(timeout=15_000)
        # The v29 registry: 5 roles, 3 platform policies and 6 tasks, each at its seed revision.
        assert registry.locator(".registry-grid.roles .registry-item").count() == 5
        assert registry.locator(".registry-grid.policies .registry-item").count() == 3
        assert registry.locator(".registry-grid.tasks .registry-item").count() == 6
        assert _card(page, BUNDLE) == "v1 · 기본값"
        # AIF-2: the AI 기본 설정 card reads the ai capability from readiness: no provider.
        chip = page.locator("[data-role='ai-capability']")
        page.wait_for_function(
            "() => document.querySelector('[data-role=ai-capability]')?.dataset.status"
        )
        assert chip.get_attribute("data-status") == "NOT_CONFIGURED"
        assert chip.inner_text() == "미설정 · AI 공급자 없음"

        registry.locator(f".registry-grid.tasks [data-prompt-key='{BUNDLE}']").click()
        editor = page.locator("[data-role='prompt-editor']")
        editor.wait_for()
        tabs = [t.inner_text().strip() for t in editor.locator("[data-layer-tab]").all()]
        assert tabs == [
            "공통 규칙",
            "역할 Role",
            "플랫폼 Policy",
            "작업 Task",
            "출력 형식",
            "입력 변수",
            "조립 미리보기",
        ]
        area = editor.locator("[data-role='prompt-text']")
        assert area.input_value().startswith("GOAL\n선택 상품에 대해 상품명, 태그, 카테고리, 옵션")
        assert editor.locator("[data-role='prompt-state']").inner_text() == "기본값"

        # Only the current tab is saved: the output schema, as one new revision.
        editor.locator("[data-layer-tab='output']").click()
        save = page.locator("[data-action='prompt-save']")
        assert save.is_disabled()
        area.fill('{"product_name":{"recommended":""}}')
        assert editor.locator("[data-role='prompt-state']").inner_text() == "저장되지 않은 변경"
        save.click()
        page.wait_for_function(f"() => {STATE}?.innerText === '사용자 수정본'")
        assert writes == [("POST", f"/api/v1/ai/prompts/{BUNDLE}/revisions")]
        page.wait_for_function(
            f"() => document.querySelector(\".registry-grid [data-prompt-key='{BUNDLE}'] em\")"
            "?.innerText.includes('v2')"
        )
        assert _card(page, BUNDLE) == "v2 · 사용자 수정본"

        # The preview is the server's composition of the saved layers, runtime data left open.
        editor.locator("[data-layer-tab='preview']").click()
        page.wait_for_function(f"() => {TEXT}?.value.includes('RUNTIME_DATA')")
        preview = area.input_value()
        assert "OUTPUT_SCHEMA" in preview and '{"product_name":{"recommended":""}}' in preview
        assert page.locator("[data-action='prompt-save']").is_hidden()

        # A reset restores the seed as one more revision.
        editor.locator("[data-layer-tab='output']").click()
        page.locator("[data-action='prompt-reset']").click()
        page.wait_for_function(f"() => {STATE}?.innerText === '기본값'")
        assert writes[-1] == ("POST", f"/api/v1/ai/prompts/{BUNDLE}/reset")
        page.wait_for_function(
            f"() => document.querySelector(\".registry-grid [data-prompt-key='{BUNDLE}'] em\")"
            "?.innerText.includes('v3')"
        )
        assert _card(page, BUNDLE) == "v3 · 기본값"
        assert len(writes) == 2


def test_a_marketplace_policy_button_and_the_insight_agent_open_their_own_layer(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    writes: list[tuple[str, str]] = []
    # The Coupang 상품 / 카테고리 sub-tab holds its policy button, as v29 places it.
    with _page(browser, client, writes, f"{LOCAL}/#/settings?tab=coupang&sub=product") as page:
        page.locator("button[data-prompt-key='POLICY_COUPANG_V1']").click()
        editor = page.locator("[data-role='prompt-editor']")
        editor.wait_for()
        assert editor.get_attribute("data-policy") == "POLICY_COUPANG_V1"
        assert (
            page.locator(".prompt-tabs [data-layer-tab='policy']").get_attribute("aria-selected")
            == "true"
        )
        assert (
            page.locator("[data-role='prompt-text']")
            .input_value()
            .startswith("PLATFORM_POLICY\n쿠팡")
        )
    with _page(browser, client, writes, f"{LOCAL}/#/ai-insight") as page:
        page.locator("button[data-prompt-key='ROLE_SHOPPING_INSIGHT_V1']").click()
        editor = page.locator("[data-role='prompt-editor']")
        editor.wait_for()
        assert editor.get_attribute("data-task") == "TASK_TREND_ANALYSIS_V1"
        assert (
            page.locator("[data-role='prompt-text']")
            .input_value()
            .startswith("ROLE\n너는 ICBM의 상품기획")
        )
    # Opening an editor reads; it never writes.
    assert writes == []
