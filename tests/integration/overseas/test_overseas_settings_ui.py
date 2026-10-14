"""ADR-0038 §3, §5 in the real UI: Settings › 공통 › 해외구매대행.

The page runs in the installed browser and every request it makes is answered in-process by the
application under test, whose FX publication source is an invented fake: nothing reaches Korea
Eximbank. The settings card shows the server's values and saves them with the revision it read;
the key input is write-only (the page shows only 설정됨/미설정 and clears the input); the rates
are the server's snapshots, refreshed by 「지금 갱신」. Nothing is priced here.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route

from app.config import AppConfig
from app.main import create_app
from tests.conftest import LOCAL
from tests.integration.overseas.test_fx_snapshots import KEY, FakeSource
from tests.integration.register.test_m5_register_ui import browser  # noqa: F401 - fixture
from tests.support.jobs_support import TEST_JOBS

pytestmark = pytest.mark.integration

FORWARDED = ("x-icbm-client", "content-type", "accept")
OVERSEAS = f"{LOCAL}/#/settings?tab=common&sub=overseas"
TIMEOUT = 15_000


@pytest.fixture
def source() -> FakeSource:
    fake = FakeSource()
    fake.always = True
    return fake


@pytest.fixture
def app(config: AppConfig, source: FakeSource) -> Iterator[TestClient]:
    with TestClient(
        create_app(config, extra_jobs=TEST_JOBS, fx_source=source), base_url=LOCAL
    ) as test_client:
        yield test_client


@contextmanager
def _page(on: Browser, app: TestClient, writes: list[tuple[str, str, str]]) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        if request.method != "GET":
            writes.append((request.method, parts.path, request.post_data or ""))
        response = app.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = on.new_page()
    page.route("**/*", answer)
    try:
        page.goto(OVERSEAS)
        page.locator("[data-role='overseas-settings'][data-state='ready']").wait_for(
            timeout=TIMEOUT
        )
        page.locator("[data-role='fx-rates'][data-state='ready']").wait_for(timeout=TIMEOUT)
        yield page
    finally:
        page.close()


def test_the_card_shows_and_saves_the_servers_settings(
    browser: Browser,  # noqa: F811
    app: TestClient,
) -> None:
    writes: list[tuple[str, str, str]] = []
    with _page(browser, app, writes) as page:
        card = page.locator("[data-role='overseas-settings']")
        field = lambda name: card.locator(f"[data-field='{name}']")  # noqa: E731
        assert field("fx_margin_percent").input_value() == "5"
        assert field("duty_rate_percent").input_value() == "8"
        assert field("overseas_delivery_days_min").input_value() == "7"
        assert field("overseas_delivery_days_max").input_value() == "14"
        assert field("overseas_shipping_default_krw").input_value() == ""
        assert (
            card.locator("[data-switch='duties_included_over_usd150']").get_attribute(
                "aria-checked"
            )
            == "true"
        )
        assert "기본값" in card.locator("[data-role='overseas-changed']").inner_text()
        field("fx_margin_percent").fill("6.5")
        field("overseas_shipping_default_krw").fill("8000")
        field("importer_label").fill("ICBM 상점")
        card.locator("[data-switch='duties_included_over_usd150']").click()
        card.locator("[data-action='save-overseas-settings']").click()
        page.locator("[data-role='overseas-settings'][data-revision='1']").wait_for(timeout=TIMEOUT)
        assert "1차 변경" in card.locator("[data-role='overseas-changed']").inner_text()
        # A value the server refuses is its verdict: nothing is written.
        field("overseas_delivery_days_min").fill("30")
        card.locator("[data-action='save-overseas-settings']").click()
        page.locator(".toast").filter(has_text="입력값을 확인하세요").wait_for(timeout=TIMEOUT)
    assert [(method, path) for method, path, _ in writes] == [
        ("PUT", "/api/v1/settings/overseas"),
        ("PUT", "/api/v1/settings/overseas"),
    ]
    held = app.get("/api/v1/settings/overseas").json()
    assert (
        held["revision_no"],
        held["fx_margin_percent"],
        held["overseas_shipping_default_krw"],
        held["duties_included_over_usd150"],
        held["importer_label"],
        held["overseas_delivery_days_min"],
    ) == (1, "6.5", 8000, False, "ICBM 상점", 7)


def test_the_key_is_write_only_and_the_rates_refresh(
    browser: Browser,  # noqa: F811
    app: TestClient,
    source: FakeSource,
) -> None:
    writes: list[tuple[str, str, str]] = []
    with _page(browser, app, writes) as page:
        chip = page.locator("[data-role='fx-key-status']")
        assert chip.inner_text() == "미설정"
        rows = page.locator("[data-role='fx-rate']")
        assert rows.count() == 3
        assert all("받은 환율 없음" in text for text in rows.all_inner_texts())
        page.locator("#fx-api-key").fill(KEY)
        page.locator("[data-action='save-fx-key']").click()
        page.locator("[data-role='fx-key-status']", has_text="설정됨").wait_for(timeout=TIMEOUT)
        assert page.locator("#fx-api-key").input_value() == ""
        assert KEY not in page.content()
        page.locator("[data-action='refresh-fx']").click()
        page.locator("[data-role='fx-rate'][data-currency='JPY']", has_text="938.4").wait_for(
            timeout=TIMEOUT
        )
        texts = rows.all_inner_texts()
        assert "1,399.9원 / 1 USD" in texts[2] and "사용 가능" in texts[2]
        assert "938.4원 / 100 JPY" in texts[1]
        page.locator("[data-action='delete-fx-key']").click()
        page.locator("[data-role='fx-key-status']", has_text="미설정").wait_for(timeout=TIMEOUT)
    assert [(method, path) for method, path, _ in writes] == [
        ("PUT", "/api/v1/settings/overseas/fx-key"),
        ("POST", "/api/v1/settings/overseas/fx/refresh"),
        ("DELETE", "/api/v1/settings/overseas/fx-key"),
    ]
    assert source.calls and {key for key, _ in source.calls} == {KEY}
