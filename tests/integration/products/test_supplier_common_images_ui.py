"""A-NEXT2a: the supplier common-image settings surface in the real browser.

The UI reads the existing list contract, renders bytes only through Issue #231's read-only image
route, and sends only the existing BLOCK/KEEP decision command. The decision survives reload and
application restart; no provider is involved.
"""

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from tests.conftest import LOCAL
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.product_support import SUPPLIER, Collections, count, product

pytestmark = pytest.mark.integration

URL = f"{LOCAL}/#/settings?tab=common&sub=common-images&supplier={SUPPLIER}"
BASE = "/api/v1/products/supplier-common-images"
FORWARDED = ("x-icbm-client", "content-type", "accept")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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


@contextmanager
def _served(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@contextmanager
def _page(
    browser: Browser,
    client: TestClient,
    requests: list[tuple[str, str]],
) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        requests.append((request.method, parts.path))
        response = client.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(URL)
        page.wait_for_selector(".supplier-common-images[data-supplier]", timeout=15_000)
        yield page
    finally:
        page.close()


def _candidate(container: Container, config: AppConfig) -> str:
    stored = container.source_assets.put(PNG)
    image = ImageReference(
        role=ImageRole.DETAIL,
        ordinal=1,
        host="img.shop.example",
        provenance=".detail img:nth-of-type(1)",
        status=FieldStatus.CONFIRMED,
        sha256=stored.sha256,
    )
    collections = Collections.of(container, config)
    for source_id in ("common-ui-1", "common-ui-2", "common-ui-3"):
        collections.collect(product(), source_product_id=source_id, extra_images=(image,))
    return stored.sha256


def _card(sha256: str) -> str:
    return f".common-image-card[data-common-image='{sha256}']"


def test_settings_previews_and_decides_a_supplier_common_image(
    browser: Browser, config: AppConfig
) -> None:
    requests: list[tuple[str, str]] = []
    with _served(config) as client:
        container: Container = client.app.state.container  # type: ignore[attr-defined]
        sha256 = _candidate(container, config)
        with _page(browser, client, requests) as page:
            card = page.locator(_card(sha256))
            card.wait_for(timeout=10_000)
            assert card.get_attribute("data-common-image-verdict") == "REVIEW"
            assert "확인 필요" in card.inner_text()
            assert "발견 상품 3개" in card.inner_text()
            assert "판정 이력 없음" in card.inner_text()
            image = card.locator("img")
            image.wait_for(state="visible")
            assert image.evaluate("node => node.complete && node.naturalWidth > 0")

            list_path = f"{BASE}/{SUPPLIER}"
            preview_path = f"{BASE}/{SUPPLIER}/{sha256}/image"
            common_calls = [(method, path) for method, path in requests if path.startswith(BASE)]
            assert ("GET", list_path) in common_calls
            assert ("GET", preview_path) in common_calls
            assert not [call for call in common_calls if call[0] != "GET"]

            card.locator("[data-common-image-reason]").fill("상세 설명에 필요한 이미지")
            card.locator("[data-action='decide-common-image-keep']").click()
            page.wait_for_selector(f"{_card(sha256)}[data-common-image-verdict='KEEP']")
            kept = page.locator(_card(sha256))
            assert "판정 이력 1회" in kept.inner_text()
            assert "판정자 operator" in kept.inner_text()
            assert "사유 상세 설명에 필요한 이미지" in kept.inner_text()
            assert requests.count(("POST", f"{BASE}/{SUPPLIER}/{sha256}")) == 1

            page.reload()
            page.wait_for_selector(f"{_card(sha256)}[data-common-image-verdict='KEEP']")
            assert "판정 이력 1회" in page.locator(_card(sha256)).inner_text()

        assert count(config, "supplier_common_image_decisions") == 1
        assert count(config, "audit_events", "event_type = ?", "SUPPLIER_COMMON_IMAGE_DECIDED") == 1

    # Fresh application ownership reads the durable decision; nothing was page-local.
    with _served(config) as client, _page(browser, client, []) as page:
        page.wait_for_selector(f"{_card(sha256)}[data-common-image-verdict='KEEP']")
        card = page.locator(_card(sha256))
        assert "판정 이력 1회" in card.inner_text()
        card.locator("[data-action='decide-common-image-block']").click()
        page.wait_for_selector(f"{_card(sha256)}[data-common-image-verdict='BLOCK']")
        assert "판정 이력 2회" in page.locator(_card(sha256)).inner_text()

    assert count(config, "supplier_common_image_decisions") == 2


def test_the_surface_is_wired_only_to_the_adopted_common_image_contracts() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "ui/web/js/pages/settings/supplier-common-images.js").read_text(
        encoding="utf-8"
    )
    assert "'/api/v1/products/supplier-common-images'" in source
    assert "/image`" in source
    assert "sendJson('POST', path(supplier, image.sha256)" in source
    assert "/api/v1/connect/suppliers" not in source
    assert "fetch('http" not in source and 'fetch("http' not in source
