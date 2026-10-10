"""ADR-0033 G3 in the real UI: Settings › 공통 › 상세페이지 공지.

The page runs in the installed browser and every request it makes is answered in-process by the
application under test. The card renders what the Detail Guidance owner holds and only POSTs: the
five-template preview, every validation verdict, the saved image and each period's status are the
server's. A preset only fills the inputs (asking first when they were edited) and suggests its
template; choosing a template never changes the text. Nothing reaches a product or a marketplace.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Dialog, Locator, Page, Route

from tests.conftest import LOCAL
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    browser,
    client,
)

pytestmark = pytest.mark.integration

FORWARDED = ("x-icbm-client", "content-type", "accept")
GUIDANCE = f"{LOCAL}/#/settings?tab=common&sub=guidance"
BASE = "/api/v1/settings/detail-guidance"
LABELS = ["클린", "모던", "웜", "국내배송", "해외배송"]
TIMEOUT = 15_000


@contextmanager
def _page(
    on: Browser,
    app: TestClient,
    writes: list[tuple[str, str]],
    dialogs: list[str] | None = None,
    answer_dialog: Callable[[], bool] = lambda: True,
    viewport: dict[str, int] | None = None,
) -> Iterator[Page]:
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

    def dialog(d: Dialog) -> None:
        if dialogs is not None:
            dialogs.append(d.message)
        if answer_dialog():
            d.accept()
        else:
            d.dismiss()

    page = on.new_page(viewport=viewport) if viewport else on.new_page()
    page.route("**/*", answer)
    page.on("dialog", dialog)
    try:
        page.goto(GUIDANCE)
        page.locator("[data-role='detail-guidance'][data-state='ready']").wait_for(timeout=TIMEOUT)
        yield page
    finally:
        page.close()


def _values(scope: Locator, role: str) -> list[str]:
    return [item.input_value() for item in scope.locator(f"[data-role='{role}']").all()]


def _five_previews(scope: Locator) -> None:
    scope.locator("[data-role='dg-preview'] [data-role='dg-template'] img").nth(4).wait_for(
        timeout=TIMEOUT
    )


def _ready(page: Page, scope: Locator) -> None:
    scope.locator("[data-role='dg-save']").wait_for()
    page.wait_for_function(
        "el => !el.disabled",
        arg=scope.locator("[data-role='dg-save']").element_handle(),
        timeout=TIMEOUT,
    )


def _settings(app: TestClient) -> dict[str, Any]:
    body: dict[str, Any] = app.get(BASE).json()
    return body


def _placement(app: TestClient, placement: str) -> dict[str, Any]:
    found: dict[str, Any] = next(
        p for p in _settings(app)["placements"] if p["placement"] == placement
    )
    return found


def test_a_standing_notice_is_previewed_in_five_templates_saved_and_turned_off(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        card = page.locator("[data-role='detail-guidance']")
        top = card.locator("[data-role='dg-top'] [data-role='dg-standing']")
        bottom = card.locator("[data-role='dg-bottom'] [data-role='dg-standing']")
        # Nothing saved yet: the top opens with one empty block, the bottom with the two editable
        # default headings of ADR-0033 §4.
        assert top.locator("[data-role='dg-current']").get_attribute("data-seq") == ""
        assert _values(top, "dg-heading") == [""]
        assert _values(bottom, "dg-heading") == ["배송 안내", "C/S 안내"]
        assert top.locator("[data-role='dg-save']").is_disabled()
        assert writes == []

        top.locator("[data-role='dg-heading']").fill("당일발송 안내")
        top.locator("[data-role='dg-line']").fill("12시 이전 주문시 당일발송")
        _five_previews(top)
        tiles = top.locator("[data-role='dg-template']")
        assert [t.inner_text().strip() for t in tiles.all()] == LABELS
        assert all(img.evaluate("i => i.naturalWidth") == 860 for img in tiles.locator("img").all())
        # The preview is the server's; nothing is chosen for the operator, so nothing saves yet.
        assert top.locator("[data-role='dg-save']").is_disabled()
        assert set(writes) == {("POST", f"{BASE}/preview")}

        # Choosing a template picks a design only: the text stays exactly as typed.
        top.locator("[data-template='MODERN']").click()
        assert top.locator("[data-template='MODERN']").get_attribute("aria-checked") == "true"
        assert top.locator("[data-template='CLEAN']").get_attribute("aria-checked") == "false"
        assert _values(top, "dg-heading") == ["당일발송 안내"]
        assert _values(top, "dg-line") == ["12시 이전 주문시 당일발송"]
        _ready(page, top)
        top.locator("[data-role='dg-save']").click()
        page.wait_for_function(
            "() => document.querySelector(\"[data-role='dg-top'] [data-role='dg-current']\")"
            "?.dataset.seq === '1'"
        )
        current = top.locator("[data-role='dg-current']")
        assert current.locator("[data-role='dg-current-template']").inner_text() == "모던"
        assert current.locator("[data-role='dg-enabled']").inner_text() == "사용 중"
        image = current.locator("[data-role='dg-current-image']")
        page.wait_for_function("i => i.complete && i.naturalWidth > 0", arg=image.element_handle())
        assert image.evaluate("i => i.naturalWidth") == 860
        assert writes.count(("POST", f"{BASE}/revisions")) == 1
        standing = _placement(client, "TOP")["standing"]
        assert (standing["seq"], standing["template"], standing["enabled"]) == (1, "MODERN", True)
        assert standing["content"] == {
            "blocks": [{"heading": "당일발송 안내", "lines": ["12시 이전 주문시 당일발송"]}]
        }
        assert image.get_attribute("src") == standing["image_url"]
        # After the save the inputs hold the saved text and template, read back from the server.
        assert _values(top, "dg-heading") == ["당일발송 안내"]
        assert top.locator("[data-template='MODERN']").get_attribute("aria-checked") == "true"

        # Turning it off appends a revision with the same text and template.
        top.locator("[data-action='dg-toggle']").click()
        page.wait_for_function(
            "() => document.querySelector(\"[data-role='dg-top'] [data-role='dg-current']\")"
            "?.dataset.seq === '2'"
        )
        assert top.locator("[data-role='dg-enabled']").inner_text() == "꺼짐"
        standing = _placement(client, "TOP")["standing"]
        assert (standing["seq"], standing["template"], standing["enabled"]) == (2, "MODERN", False)

        # Saving what is already current changes nothing, and the page says so.
        _ready(page, top)
        top.locator("[data-role='dg-save']").click()
        message = top.locator("[data-role='dg-message'][data-code='GUIDANCE_UNCHANGED']")
        message.wait_for(timeout=TIMEOUT)
        assert "바뀐 것이 없습니다" in message.inner_text()
        assert _placement(client, "TOP")["standing"]["seq"] == 2

        # A notice that moved since it was read is never overwritten: the operator reloads.
        moved = client.post(
            f"{BASE}/revisions",
            headers={"X-ICBM-Client": "pytest"},
            json={
                "actor": "elsewhere",
                "placement": "TOP",
                "kind": "STANDING",
                "expected_current_seq": 2,
                "template": "WARM",
                "content": standing["content"],
                "enabled": True,
            },
        )
        assert moved.status_code == 200, moved.text
        top.locator("[data-role='dg-line']").fill("오후 1시 이전 주문시 당일발송")
        _ready(page, top)
        top.locator("[data-role='dg-save']").click()
        moved_line = top.locator("[data-role='dg-message'][data-code='GUIDANCE_CURRENT_MOVED']")
        moved_line.wait_for(timeout=TIMEOUT)
        assert "다시 불러온 뒤" in moved_line.inner_text()
        assert _placement(client, "TOP")["standing"]["seq"] == 3
        top.locator("[data-action='dg-reload']").click()
        page.wait_for_function(
            "() => document.querySelector(\"[data-role='dg-top'] [data-role='dg-current']\")"
            "?.dataset.seq === '3'"
        )
        assert top.locator("[data-role='dg-current-template']").inner_text() == "웜"
        # Only the Detail Guidance contract was written to.
        assert {path for _, path in writes} <= {f"{BASE}/preview", f"{BASE}/revisions"}


def test_a_preset_fills_only_its_placement_and_asks_before_replacing_edits(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    presets = {p["key"]: p for p in _settings(client)["presets"]}
    overseas, domestic = presets["OVERSEAS"], presets["DOMESTIC"]
    writes: list[tuple[str, str]] = []
    dialogs: list[str] = []
    accept = {"answer": True}
    with _page(browser, client, writes, dialogs, lambda: accept["answer"]) as page:
        card = page.locator("[data-role='detail-guidance']")
        top = card.locator("[data-role='dg-top'] [data-role='dg-standing']")
        bottom = card.locator("[data-role='dg-bottom'] [data-role='dg-standing']")
        note = bottom.locator("[data-role='dg-preset-note']")
        assert note.is_visible()
        assert "예시 문구" in note.inner_text() and "○○" in note.inner_text()

        # Untouched inputs are replaced without asking.
        bottom.locator("[data-role='dg-preset'][data-preset='OVERSEAS']").click()
        assert dialogs == []
        expected = overseas["bottom"]["blocks"]
        assert _values(bottom, "dg-heading") == [b["heading"] for b in expected]
        assert _values(bottom, "dg-line") == [line for b in expected for line in b["lines"]]
        # The preset suggests its template; the other placement is untouched.
        assert bottom.locator("[data-template='OVERSEAS']").get_attribute("aria-checked") == "true"
        assert _values(top, "dg-heading") == [""] and _values(top, "dg-line") == [""]
        assert bottom.locator("[data-role='dg-placeholder']").is_visible()
        _five_previews(bottom)

        # Choosing another template keeps the preset's text.
        bottom.locator("[data-template='CLEAN']").click()
        assert _values(bottom, "dg-heading") == [b["heading"] for b in expected]

        # Edited inputs are replaced only when the operator agrees.
        bottom.locator("[data-role='dg-line']").first.fill("우리 가게 해외배송 안내입니다")
        accept["answer"] = False
        bottom.locator("[data-role='dg-preset'][data-preset='DOMESTIC']").click()
        assert len(dialogs) == 1 and "국내배송" in dialogs[0]
        assert _values(bottom, "dg-line")[0] == "우리 가게 해외배송 안내입니다"
        accept["answer"] = True
        bottom.locator("[data-role='dg-preset'][data-preset='DOMESTIC']").click()
        assert len(dialogs) == 2
        expected = domestic["bottom"]["blocks"]
        assert _values(bottom, "dg-heading") == [b["heading"] for b in expected]
        assert _values(bottom, "dg-line") == [line for b in expected for line in b["lines"]]
        assert bottom.locator("[data-template='DOMESTIC']").get_attribute("aria-checked") == "true"

        # A preset on the top section fills it with the preset's top text.
        top.locator("[data-role='dg-preset'][data-preset='DOMESTIC']").click()
        assert len(dialogs) == 2
        top_blocks = domestic["top"]["blocks"]
        assert _values(top, "dg-heading") == [b["heading"] for b in top_blocks]
        # Nothing is saved by a preset.
        assert ("POST", f"{BASE}/revisions") not in writes
        assert _placement(client, "BOTTOM")["standing"] is None


def test_the_server_refuses_a_url_and_the_save_stays_disabled(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        top = page.locator("[data-role='dg-top'] [data-role='dg-standing']")
        error = top.locator("[data-role='dg-error']")
        line = top.locator("[data-role='dg-line']")
        top.locator("[data-template='CLEAN']").click()
        line.fill("자세한 안내는 톡톡으로 문의해 주세요")
        _five_previews(top)
        _ready(page, top)

        # A block may grow to six lines and the notice to three blocks, and no further. An empty
        # line is the server's refusal too, named under the inputs.
        for _ in range(5):
            top.locator("[data-action='dg-line-add']").click()
        assert line.count() == 6
        assert top.locator("[data-action='dg-line-add']").is_disabled()
        error.wait_for(state="visible", timeout=TIMEOUT)
        page.wait_for_function(
            "e => e.innerText.includes('빈 줄')", arg=error.element_handle(), timeout=TIMEOUT
        )
        assert top.locator("[data-role='dg-save']").is_disabled()
        for _ in range(5):
            top.locator("[data-action='dg-line-remove']").last.click()
        top.locator("[data-action='dg-block-add']").click()
        top.locator("[data-action='dg-block-add']").click()
        assert top.locator("[data-role='dg-block']").count() == 3
        assert top.locator("[data-action='dg-block-add']").is_disabled()
        top.locator("[data-action='dg-block-remove']").last.click()
        top.locator("[data-action='dg-block-remove']").last.click()
        assert top.locator("[data-role='dg-block']").count() == 1
        assert top.locator("[data-action='dg-block-remove']").is_disabled()
        error.wait_for(state="hidden", timeout=TIMEOUT)
        _ready(page, top)

        # A URL is refused by the server: its message shows under the inputs, the line is
        # marked, no preview is shown and the save is disabled.
        line.fill("자세한 안내는 naver.com 에서 확인")
        error.wait_for(state="visible", timeout=TIMEOUT)
        assert "URL" in error.inner_text()
        assert "GUIDANCE_TEXT_INVALID" in error.inner_text()
        assert line.get_attribute("aria-invalid") == "true"
        assert top.locator("[data-role='dg-preview'] img").count() == 0
        assert top.locator("[data-role='dg-save']").is_disabled()

        # The fixed text previews and may be saved again.
        line.fill("자세한 안내는 톡톡으로 문의해 주세요")
        error.wait_for(state="hidden", timeout=TIMEOUT)
        _five_previews(top)
        _ready(page, top)
        assert ("POST", f"{BASE}/revisions") not in writes


def test_a_period_notice_is_added_listed_as_scheduled_and_ended_early(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    writes: list[tuple[str, str]] = []
    dialogs: list[str] = []
    start = (datetime.now() + timedelta(days=1)).replace(second=0, microsecond=0)
    end = start + timedelta(days=2)
    with _page(browser, client, writes, dialogs) as page:
        periods = page.locator("[data-role='dg-bottom'] [data-role='dg-periods']")
        assert periods.locator("[data-role='dg-period']").count() == 0
        periods.locator("[data-role='dg-period-add']").click()
        form = periods.locator("[data-role='dg-period-form']")
        form.locator("[data-role='dg-heading']").fill("추석 연휴 배송 안내")
        form.locator("[data-role='dg-line']").fill("연휴 기간 주문은 연휴 후 순차 발송됩니다")
        form.locator("[data-role='dg-start']").fill(start.strftime("%Y-%m-%dT%H:%M"))
        form.locator("[data-role='dg-end']").fill(end.strftime("%Y-%m-%dT%H:%M"))
        _five_previews(form)
        form.locator("[data-template='WARM']").click()
        _ready(page, form)
        form.locator("[data-role='dg-save']").click()

        row = periods.locator("[data-role='dg-period']")
        row.wait_for(timeout=TIMEOUT)
        assert row.locator("[data-role='dg-status']").inner_text() == "예정"
        assert row.get_attribute("data-status") == "SCHEDULED"
        notice = _placement(client, "BOTTOM")["periods"][0]
        # The local wall time was sent with the browser's offset and stored as that instant.
        assert datetime.fromisoformat(notice["starts_at"]) == start.astimezone(UTC)
        assert datetime.fromisoformat(notice["ends_at"]) == end.astimezone(UTC)
        assert (notice["seq"], notice["template"], notice["enabled"]) == (1, "WARM", True)
        assert row.locator("img").get_attribute("src") == notice["image_url"]
        # The standing notice of the placement is untouched.
        assert _placement(client, "BOTTOM")["standing"] is None

        row.locator("[data-action='dg-period-end']").click()
        assert len(dialogs) == 1
        page.wait_for_function(
            "() => document.querySelector(\"[data-role='dg-bottom'] [data-role='dg-period']\")"
            "?.dataset.status === 'ENDED'"
        )
        row = periods.locator("[data-role='dg-period']")
        assert row.locator("[data-role='dg-status']").inner_text() == "종료"
        assert row.locator("[data-action='dg-period-end']").count() == 0
        notice = _placement(client, "BOTTOM")["periods"][0]
        assert (notice["seq"], notice["enabled"], notice["status"]) == (2, False, "ENDED")
        assert writes.count(("POST", f"{BASE}/revisions")) == 2


@pytest.mark.parametrize(
    "viewport",
    [
        {"width": 1920, "height": 1080},
        {"width": 1080, "height": 1920},
        {"width": 390, "height": 844},
    ],
    ids=["1920x1080", "1080x1920", "phone"],
)
def test_the_populated_card_never_overflows_horizontally(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    viewport: dict[str, int],
) -> None:
    presets = {p["key"]: p for p in _settings(client)["presets"]}
    for placement, key in (("TOP", "top"), ("BOTTOM", "bottom")):
        saved = client.post(
            f"{BASE}/revisions",
            headers={"X-ICBM-Client": "pytest"},
            json={
                "actor": "pytest",
                "placement": placement,
                "kind": "STANDING",
                "template": "OVERSEAS",
                "content": presets["OVERSEAS"][key],
            },
        )
        assert saved.status_code == 200, saved.text
    now = datetime.now(UTC)
    period = client.post(
        f"{BASE}/revisions",
        headers={"X-ICBM-Client": "pytest"},
        json={
            "actor": "pytest",
            "placement": "BOTTOM",
            "kind": "PERIOD",
            "template": "WARM",
            "content": {
                "blocks": [{"heading": "택배 마감", "lines": ["연휴 동안 발송이 멈춥니다"]}]
            },
            "starts_at": (now - timedelta(hours=1)).isoformat(),
            "ends_at": (now + timedelta(days=1)).isoformat(),
        },
    )
    assert period.status_code == 200, period.text
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes, viewport=viewport) as page:
        for role in ("dg-top", "dg-bottom"):
            _five_previews(page.locator(f"[data-role='{role}'] [data-role='dg-standing']"))
        row = page.locator("[data-role='dg-bottom'] [data-role='dg-period']")
        assert row.locator("[data-role='dg-status']").inner_text() == "진행 중"
        page.locator("[data-role='dg-bottom'] [data-role='dg-period-add']").click()
        overflow = page.evaluate(
            """() => {
              const wide = (el) => el.scrollWidth > el.clientWidth + 1;
              const card = document
                .querySelector('[data-role=detail-guidance]')
                .closest('.setting-card');
              const inside = [...card.querySelectorAll('*')].filter((el) => {
                const r = el.getBoundingClientRect();
                const c = card.getBoundingClientRect();
                return r.width > 0 && (r.right > c.right + 1 || r.left < c.left - 1);
              }).map((el) => el.className || el.tagName);
              return { page: wide(document.documentElement), card: wide(card), inside };
            }"""
        )
        assert overflow == {"page": False, "card": False, "inside": []}, overflow
