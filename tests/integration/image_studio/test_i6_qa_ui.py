"""ADR-0041 I6 in the real UI: 「적용」 names a region with source text left on the rendering (§6).

The page runs in the installed browser, every request answered in-process by the application
under test, as in the I5 UI suite. The fake OCR engine read r1's box too tight, so its last two
"glyphs" survive the rendering: 「적용」 is refused, the panel marks r1 `원문 글자 남음` and keeps
「적용」 disabled, naming r1, until the operator confirms it; then the translated image is recorded.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser

from app.config import AppConfig
from app.container import Container
from app.stages.products.image_model import SourceDecisionKind
from tests.integration.image_studio.test_i5_studio_ui import (
    MODAL,
    _draft,
    _open_editor,
    _page,
    _preview_settled,
    _region,
)
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    account,
    browser,
    client,
    container,
)
from tests.support.image_studio_support import TIGHT, FakeOcr, make_ready, studio_product

pytestmark = pytest.mark.integration

ENABLED = "() => !document.querySelector('[data-action=studio-apply]').disabled"


def test_leftover_source_text_is_named_and_confirmed_before_the_image_is_recorded(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    account: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    make_ready(container, tmp_path, FakeOcr(regions=TIGHT))
    item = studio_product(container, config)
    draft_id = _draft(container, account, item)
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        _open_editor(page, client, draft_id)
        page.locator(".stepper .step[data-step='detail']").click()
        page.wait_for_function(
            "() => document.querySelector('[data-studio-scope=detail]')?.dataset.ready === 'true'"
        )
        page.locator("[data-studio-scope='detail']").click()
        studio = page.locator(f"{MODAL} [data-role='image-studio']")
        studio.wait_for(timeout=15_000)
        studio.locator(f"[data-image='{item.detail}'] [data-role='studio-pick']").wait_for()
        studio.locator("[data-action='studio-run']").click()
        page.locator(f"{MODAL} .studio-region").nth(4).wait_for(timeout=30_000)
        _preview_settled(page)
        # Nothing flags r1 before 「적용」: only the rendering shows what is left of it.
        studio.locator("[data-action='studio-apply-all']").click()
        _preview_settled(page)
        assert _region(page, "r1").get_attribute("data-flags") == ""
        page.wait_for_function(ENABLED)
        apply = studio.locator("[data-action='studio-apply']")
        apply.click()

        r1 = _region(page, "r1")
        page.wait_for_function(
            "() => document.querySelector(\"[data-region='r1']\")"
            "?.dataset.flags.includes('LEFTOVER_SOURCE_TEXT')",
            timeout=15_000,
        )
        assert r1.locator("[data-flag='LEFTOVER_SOURCE_TEXT']").inner_text() == "원문 글자 남음"
        assert r1.get_attribute("data-flagged") == "true"
        assert apply.is_disabled()
        assert "r1" in studio.locator("[data-role='studio-apply-why']").inner_text()
        assert studio.locator("[data-role='studio-artifact']").count() == 0
        selection = container.images.current_selection(item.item_id)
        assert selection is not None
        assert {d.sha256: d.decision for d in selection.decisions}[item.detail] == (
            SourceDecisionKind.USE_SOURCE
        )

        r1.locator("[data-role='studio-confirm']").check()
        page.wait_for_function(ENABLED)
        apply.click()
        artifact = studio.locator("[data-role='studio-artifact']")
        artifact.wait_for(timeout=15_000)
        assert len(artifact.get_attribute("data-sha256") or "") == 64
        # The flag stays shown on the confirmed row.
        assert r1.get_attribute("data-flags") == "LEFTOVER_SOURCE_TEXT"

    image = f"/api/v1/image-studio/items/{item.item_id}/images/{item.detail}"
    assert [w for w in writes if not w[1].endswith("/preview")] == [
        ("POST", f"/api/v1/image-studio/products/{item.group}/translations"),
        ("POST", f"{image}/revisions"),
        ("POST", f"{image}/apply"),
        # The same edit again is that revision (IMAGE_EDIT_UNCHANGED), applied with r1 confirmed.
        ("POST", f"{image}/revisions"),
        ("POST", f"{image}/apply"),
    ]
