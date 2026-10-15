"""The duplicate row a seller sees in the registration editor (ADR-0042 S3b, §0 a4).

The component runs in the installed browser, inside the application page; every request it makes
is answered in-process, and the override call is answered by a stub. Proven, with synthetic units:
- 「중복 없음 · <time> 동기화 기준」 for a NO_MATCH;
- 「스토어 다시 확인 중…」 with the last complete sync time while the automatic re-sync runs;
- a real match shows 「이미 스토어에 있는 상품: …」 with its listing and 「의도적으로 한 번
  더 등록」, which asks for a reason and posts it; 「같은 상품으로 연결」 is not shown before S2a;
- a recorded override reads 「한 번 더 등록하기로 함」 with 「되돌리기」;
- the words are plain Korean: every code stays in a data-* attribute.
"""

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.main import create_app
from tests.conftest import LOCAL
from tests.support.browser import BROWSER_CHANNEL, launch_browser

pytestmark = pytest.mark.integration

FORWARDED = ("x-icbm-client", "content-type", "accept")
AS_OF = "2026-10-16T05:02:00+00:00"
CODES = ("SELLER_CODE", "NORMALIZED_NAME", "DUPLICATE_", "PROVIDER_", "NO_MATCH", "RESYNCING")


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            launched = launch_browser(playwright)
        except PlaywrightError as exc:
            pytest.skip(f"no {BROWSER_CHANNEL} browser to launch here ({type(exc).__name__})")
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def page(browser: Browser, api: TestClient) -> Iterator[tuple[Page, list[dict[str, Any]]]]:
    posted: list[dict[str, Any]] = []

    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        if parts.path.endswith("/duplicate-override") and request.method == "POST":
            posted.append({"path": parts.path, "body": json.loads(request.post_data or "{}")})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {"preparation_id": "prep-1", "override_ids": ["ovr-1"], "active": True}
                ),
            )
            return
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        response = api.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    opened = browser.new_page()
    opened.route("**/*", answer)
    try:
        opened.goto(f"{LOCAL}/#/register")
        opened.wait_for_load_state("networkidle")
        yield opened, posted
    finally:
        opened.close()


RENDER = """async (unit) => {
  const m = await import('/js/components/duplicate-row.js');
  window.__reloads = 0;
  const node = m.duplicateRow(unit, async () => { window.__reloads += 1; });
  node.id = 'probe-' + unit.duplicate.state;
  document.body.append(node);
  return {
    state: node.dataset.duplicateState,
    reason: node.dataset.reason ?? null,
    text: node.innerText,
    time: m.syncTime(unit.duplicate.as_of),
  };
}"""


def _unit(state: str, **duplicate: Any) -> dict[str, Any]:
    return {
        "authored": {"preparation_id": "prep-1"},
        "snapshot": None,
        "duplicate": {
            "state": state,
            "as_of": AS_OF,
            "matches": [],
            "codes": [],
            "override_ids": [],
            **duplicate,
        },
    }


def _plain(text: str) -> None:
    assert not any(code in text for code in CODES), text


def test_no_match_reads_as_no_duplicate_at_the_sync_time(
    page: tuple[Page, list[dict[str, Any]]],
) -> None:
    opened, _ = page
    shown = opened.evaluate(RENDER, _unit("NO_MATCH"))
    assert shown["state"] == "NO_MATCH"
    assert f"중복 없음 · {shown['time']} 동기화 기준" in shown["text"]
    _plain(shown["text"])


def test_the_resync_reads_as_checking_the_store_again_with_the_last_sync(
    page: tuple[Page, list[dict[str, Any]]],
) -> None:
    opened, _ = page
    shown = opened.evaluate(RENDER, _unit("RESYNCING", codes=["DUPLICATE_EVIDENCE_STALE"]))
    assert "스토어 다시 확인 중…" in shown["text"]
    assert f"마지막 동기화 {shown['time']}" in shown["text"]
    assert shown["reason"] == "DUPLICATE_EVIDENCE_STALE"
    _plain(shown["text"])
    opened.click("#probe-RESYNCING [data-action='duplicate-recheck']")
    assert opened.evaluate("window.__reloads") == 1


def test_a_real_match_blocks_with_the_sellers_choices(
    page: tuple[Page, list[dict[str, Any]]],
) -> None:
    opened, posted = page
    shown = opened.evaluate(
        RENDER,
        _unit(
            "MATCH",
            codes=["PROVIDER_DUPLICATE_FOUND"],
            matches=[
                {
                    "key_kind": "SELLER_CODE",
                    "marketplace_product_id": "777",
                    "name": "스토어에 있는 상품",
                    "sale_status": "SALE",
                }
            ],
        ),
    )
    text = shown["text"]
    assert "이미 스토어에 있는 상품:" in text
    assert "스토어에 있는 상품" in text and "상품번호 777" in text and "판매중" in text
    assert "판매자코드가 같습니다" in text
    assert f"마지막 동기화 {shown['time']}" in text
    _plain(text)
    row = "#probe-MATCH"
    assert opened.locator(f"{row} [data-key-kind='SELLER_CODE']").count() == 1
    # Linking is S2a's: no inert button stands in for it.
    assert opened.locator(f"{row} [data-action='duplicate-link']").count() == 0
    assert "같은 상품으로 연결" not in text
    again = opened.locator(f"{row} [data-action='duplicate-override']")
    assert again.inner_text() == "의도적으로 한 번 더 등록" and again.is_enabled()
    again.click()
    opened.fill("[data-role='duplicate-reason']", "구성이 다른 묶음이라 따로 팝니다")
    opened.click("[data-action='duplicate-override-confirm']")
    opened.wait_for_function("window.__reloads === 1", timeout=10_000)
    assert posted == [
        {
            "path": "/api/v1/register/preparations/prep-1/duplicate-override",
            "body": {"actor": "operator", "reason": "구성이 다른 묶음이라 따로 팝니다"},
        }
    ]


def test_a_recorded_override_can_be_undone(
    page: tuple[Page, list[dict[str, Any]]],
) -> None:
    opened, _ = page
    shown = opened.evaluate(
        RENDER,
        _unit(
            "MATCH",
            override_ids=["ovr-1"],
            matches=[
                {
                    "key_kind": "NORMALIZED_NAME",
                    "marketplace_product_id": "779",
                    "name": None,
                    "sale_status": None,
                }
            ],
        ),
    )
    assert "한 번 더 등록하기로 함" in shown["text"]
    assert "상품명이 같습니다" in shown["text"]
    assert "되돌리기" in shown["text"]
    assert "의도적으로 한 번 더 등록" not in shown["text"]
    _plain(shown["text"])


def test_a_store_never_synced_says_so_in_plain_words(
    page: tuple[Page, list[dict[str, Any]]],
) -> None:
    opened, _ = page
    shown = opened.evaluate(RENDER, _unit("UNAVAILABLE", as_of=None))
    assert "스토어 상품을 아직 확인하지 못했습니다" in shown["text"]
    assert "아직 동기화 기록이 없습니다" in shown["text"]
    _plain(shown["text"])
