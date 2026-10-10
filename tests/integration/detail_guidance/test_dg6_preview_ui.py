"""ADR-0033 G6 in the real UI: the 스마트스토어 미리보기 shows the frozen notices in order.

The page runs in the installed browser and every request is answered in-process by the application
under test. The unit's own frozen Snapshot predates composition v3, so its preview route is answered
with the server's preview of a v3 payload (``app.stages.register.preview``, the same read view the
route returns): the page must draw the top notice slots before the detail images, the body after
them and the bottom notice slots last, each from the owner's local image route. A unit's own v1
preview draws no notice slot. Nothing is written and no provider URL is shown.
"""

import json

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Route

from app.container import Container
from app.stages.register.guidance import GUIDANCE_IMAGE_PATH
from app.stages.register.payload import build_payload
from app.stages.register.preview import preview
from integrations.marketplaces.smartstore import product
from tests.integration.detail_guidance.test_dg6_preview_slots import NEWEST
from tests.integration.register.test_m5_register_execution import prepare
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    _page,
    account,
    browser,
    client,
    container,
    prep,
    sources,
)
from tests.support.product_support import Collections
from tests.support.register_support import Preparation
from tests.unit.register.test_dg5_composition import ready, v3_req, v3_unit

pytestmark = pytest.mark.integration

TIMEOUT = 15_000

# The DOM order of the preview's detail structure, by role.
ORDER = """(detail) => Array.from(detail.children).map((child) => {
  if (child.dataset.role) return child.dataset.role;
  if (child.tagName === 'P') return 'paragraph';
  return child.textContent.includes('상세 이미지') ? 'detail-images' : 'other';
})"""


def test_the_preview_draws_top_notices_detail_images_body_and_bottom_notices_in_order(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    ready_ = prepare(container, sources, container.registrations, account, prep)
    frozen = build_payload(ready(v3_req("first\n\nsecond"), v3_unit())).payload
    v3 = preview("snap-v3", "h" * 64, frozen, product.project, guidance_revisions=NEWEST)
    body = json.dumps(v3.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        unit = page.locator(f".register-unit[data-draft='{ready_.draft_id}']")
        # The unit's own v1 preview: no notice slot at all.
        unit.locator("button[data-action='PREVIEW_SNAPSHOT']").click()
        own = unit.locator(".register-preview-view")
        own.wait_for(timeout=TIMEOUT)
        assert own.locator("[data-role^='preview-guidance']").count() == 0

        def answer_v3(route: Route) -> None:
            route.fulfill(status=200, content_type="application/json", body=body)

        page.route("**/api/v1/register/snapshots/*/preview", answer_v3)
        page.reload()
        page.wait_for_selector(".register-canary", timeout=TIMEOUT)
        unit = page.locator(f".register-unit[data-draft='{ready_.draft_id}']")
        unit.locator("button[data-action='PREVIEW_SNAPSHOT']").click()
        detail = unit.locator(".register-preview-detail")
        detail.wait_for(timeout=TIMEOUT)
        roles = [role for role in detail.evaluate(ORDER) if role != "other"]
        assert roles == [
            "preview-guidance-top",
            "detail-images",
            "paragraph",
            "paragraph",
            "preview-guidance-bottom",
        ]
        top = detail.locator("[data-role='preview-guidance-top'] figure")
        bottom = detail.locator("[data-role='preview-guidance-bottom'] figure")
        expected = {"top": ["a" * 64], "bottom": [sha * 64 for sha in "bca"]}
        for slots, shas in ((top, expected["top"]), (bottom, expected["bottom"])):
            assert slots.evaluate_all("(els) => els.map((e) => e.dataset.sha256)") == shas
            assert slots.evaluate_all(
                "(els) => els.map((e) => e.querySelector('img').getAttribute('src'))"
            ) == [GUIDANCE_IMAGE_PATH.format(sha256=sha) for sha in shas]
        assert "상단 공지" in top.first.inner_text()
        assert "하단 공지" in bottom.first.inner_text()
        # The period notice is labelled with its period.
        assert bottom.first.get_attribute("data-kind") == "PERIOD"
        assert bottom.first.locator("[data-role='preview-guidance-period']").count() == 1
        assert "://" not in detail.inner_text()
        assert writes == []
