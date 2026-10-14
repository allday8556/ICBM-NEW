"""ADR-0041 I5 in the real UI: the editor's 「이미지 자동번역」 and the Image Studio panel (§6, §7).

The page runs in the installed browser; every request it makes is answered in-process by the
application under test. Under the production binding (``NoOcrProvider``, no approved AI profile)
both buttons stay inert 준비 중 placeholders naming what is missing, and pressing one sends
nothing. With the fakes READY (the fake OCR engine and the application's own profiled provider
answered by the AIS-1 fakes) the operator runs a translation from ⑤, reviews the regions — a
contact detail kept by default, a flagged region left out by 「모두 적용」 until it is confirmed —
edits a Korean text, previews, 「적용」, 「확인 완료」 and 「되돌리기」. The selection moves to
USE_DERIVED and back to USE_SOURCE through the M4 image owner, the source asset stays
byte-identical, and no OCR or Korean text reaches an audit row or a log record. Neither the
screen nor the panel scrolls sideways at 1920×1080 or 390×844.
"""

import contextlib
import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Locator, Page, Route

from app.config import AppConfig
from app.container import Container
from app.stages.products.image_model import ImageAssetKind, SourceDecisionKind
from app.stages.register.model import ListingShape
from tests.conftest import LOCAL
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    account,
    browser,
    client,
    container,
)
from tests.support.image_studio_support import (
    TEXTS,
    StudioProduct,
    make_ready,
    priced,
    studio_product,
)
from tests.support.product_support import raw
from tests.support.register_support import CID, MARKET, OPERATOR

pytestmark = pytest.mark.integration

FORWARDED = ("x-icbm-client", "content-type", "accept")
MODAL = ".modal"
BASE = "/api/v1/image-studio"
WIDE = {"width": 1920, "height": 1080}
PHONE = {"width": 390, "height": 844}


def _draft(served: Container, holder: str, item: StudioProduct) -> str:
    snapshot = priced(served, item.item_id)
    with served.registrations.transaction() as unit:
        created = unit.create_draft(
            MARKET,
            holder,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR,
            correlation_id=CID,
        )
        unit.add_draft_item(
            created.draft_id, item.item_id, snapshot, added_by=OPERATOR, correlation_id=CID
        )
        return created.draft_id


@contextmanager
def _page(chrome: Browser, app: TestClient, writes: list[tuple[str, str]]) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        body = request.post_data_buffer
        if request.method != "GET":
            writes.append((request.method, parts.path))
        response = app.request(request.method, path, content=body, headers=headers)
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = chrome.new_page(viewport=WIDE)
    page.route("**/*", answer)
    try:
        yield page
    finally:
        page.close()


def _open_editor(page: Page, app: TestClient, draft_id: str) -> None:
    unit = app.get(f"/api/v1/register/units/{draft_id}").json()[0]
    page.goto(f"{LOCAL}/#/register-editor?draft={draft_id}&unit={unit['unit_ref']}&mode=register")
    page.locator("[data-role='register-editor']").wait_for(timeout=15_000)


def _no_horizontal_scroll(page: Page) -> None:
    overflow = page.evaluate(
        """() => {
          const doc = document.documentElement;
          const card = document.querySelector('.modal .modal-card');
          const body = document.querySelector('.modal .modal-body');
          return {
            page: doc.scrollWidth - doc.clientWidth,
            card: card ? card.scrollWidth - card.clientWidth : 0,
            body: body ? body.scrollWidth - body.clientWidth : 0,
          };
        }"""
    )
    assert overflow == {"page": 0, "card": 0, "body": 0}, overflow


def _preview_settled(page: Page) -> None:
    page.wait_for_function(
        "() => document.querySelector('[data-role=studio-preview-state]')"
        "?.dataset.complete === 'true'",
        timeout=15_000,
    )


def _region(page: Page, rid: str) -> Locator:
    return page.locator(f"{MODAL} [data-region='{rid}']")


def test_both_buttons_stay_inert_and_name_what_is_missing_under_the_production_binding(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    account: str,  # noqa: F811
) -> None:
    item = studio_product(container, config)
    draft_id = _draft(container, account, item)
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        _open_editor(page, client, draft_id)
        page.wait_for_function(
            "() => [...document.querySelectorAll('[data-studio-scope]')]"
            ".every((b) => b.dataset.ready === 'false')"
        )
        buttons = page.locator("[data-studio-scope]")
        assert sorted(b.get_attribute("data-studio-scope") or "" for b in buttons.all()) == [
            "detail",
            "selected",
        ]
        for button in buttons.all():
            assert button.get_attribute("aria-disabled") == "true"
            assert button.get_attribute("data-ai-placeholder") == "이미지 자동번역"
            inert = button.get_attribute("data-inert") or ""
            assert "준비 중" in inert and "OCR 엔진이 설치되지 않았습니다" in inert
            assert "AI 번역 연결" in inert
            assert "준비 중" in (button.get_attribute("title") or "")
        page.locator(".stepper .step[data-step='images']").click()
        page.locator("[data-studio-scope='selected']").click(force=True)
        page.locator(".stepper .step[data-step='detail']").click()
        page.locator("[data-studio-scope='detail']").click(force=True)
        assert page.locator(MODAL).count() == 0
        _no_horizontal_scroll(page)
    assert writes == []


def test_a_translation_is_reviewed_applied_confirmed_and_reverted(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    account: str,  # noqa: F811
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    make_ready(container, tmp_path)
    item = studio_product(container, config)
    draft_id = _draft(container, account, item)
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        _open_editor(page, client, draft_id)
        page.locator(".stepper .step[data-step='detail']").click()
        button = page.locator("[data-studio-scope='detail']")
        page.wait_for_function(
            "() => document.querySelector('[data-studio-scope=detail]')?.dataset.ready === 'true'"
        )
        assert button.get_attribute("aria-disabled") is None
        assert button.get_attribute("data-inert") is None
        button.click()
        studio = page.locator(f"{MODAL} [data-role='image-studio']")
        studio.wait_for(timeout=15_000)
        # ⑤ ticks the product's detail images; the representative image is left to the operator.
        detail = studio.locator(f"[data-image='{item.detail}'] [data-role='studio-pick']")
        detail.wait_for(timeout=15_000)
        assert detail.is_checked()
        assert not studio.locator(
            f"[data-image='{item.representative}'] [data-role='studio-pick']"
        ).is_checked()
        assert writes == []
        _no_horizontal_scroll(page)

        studio.locator("[data-action='studio-run']").click()
        page.locator(f"{MODAL} .studio-region").nth(4).wait_for(timeout=30_000)
        candidate = studio.locator("[data-role='studio-candidate']")
        assert candidate.get_attribute("data-status") == "FRESH"
        _preview_settled(page)
        # Nothing is included yet: every region keeps its original until the operator decides.
        assert {
            rid: (
                _region(page, rid).get_attribute("data-include"),
                _region(page, rid).get_attribute("data-flags"),
            )
            for rid in ("r0", "r1", "r2", "r3", "r4")
        } == {
            "r0": ("false", ""),
            "r1": ("false", ""),
            "r2": ("false", "CONTACT_DETAIL"),
            "r3": ("false", "LOW_CONFIDENCE"),
            "r4": ("false", "SCRIPT_MISMATCH COMPLEX_BACKGROUND"),
        }
        apply = studio.locator("[data-action='studio-apply']")
        assert apply.is_disabled()
        # The contact detail was kept and never sent: its row has no suggestion to offer.
        assert _region(page, "r2").locator("[data-role='studio-korean']").input_value() == ""
        assert _region(page, "r0").locator("[data-role='studio-korean']").input_value() == (
            "순수 천연 참기름"
        )

        # 「모두 적용」 includes every unflagged region and never a flagged one.
        studio.locator("[data-action='studio-apply-all']").click()
        _preview_settled(page)
        included = [
            rid
            for rid in ("r0", "r1", "r2", "r3", "r4")
            if _region(page, rid).get_attribute("data-include") == "true"
        ]
        assert included == ["r0", "r1"]
        page.wait_for_function(
            "() => !document.querySelector('[data-action=studio-apply]').disabled"
        )
        # The flagged r3 included blocks 「적용」 until it is confirmed on its own.
        _region(page, "r3").locator("[data-role='studio-include']").check()
        _preview_settled(page)
        assert apply.is_disabled()
        assert "r3" in studio.locator("[data-role='studio-apply-why']").inner_text()
        _region(page, "r3").locator("[data-role='studio-confirm']").check()
        page.wait_for_function(
            "() => !document.querySelector('[data-action=studio-apply]').disabled"
        )
        # The operator edits one Korean text; the preview follows.
        _region(page, "r1").locator("[data-role='studio-korean']").fill("용량 350ml 대용량")
        _preview_settled(page)
        assert _region(page, "r1").get_attribute("data-flags") == ""
        assert (
            studio.locator("[data-role='studio-preview-image']")
            .get_attribute("src")
            .startswith("data:image/png;base64,")
        )
        _no_horizontal_scroll(page)
        page.set_viewport_size(PHONE)
        _no_horizontal_scroll(page)
        page.set_viewport_size(WIDE)

        page.wait_for_function(
            "() => !document.querySelector('[data-action=studio-apply]').disabled"
        )
        apply.click()
        artifact = studio.locator("[data-role='studio-artifact']")
        artifact.wait_for(timeout=15_000)
        artifact_sha = artifact.get_attribute("data-sha256") or ""
        assert len(artifact_sha) == 64
        # Recorded, not yet used: the source is still selected until 「확인 완료」.
        selection = container.images.current_selection(item.item_id)
        assert selection is not None
        assert {d.sha256: d.decision for d in selection.decisions}[item.detail] == (
            SourceDecisionKind.USE_SOURCE
        )

        studio.locator("[data-action='studio-qa-pass']").click()
        studio.locator(
            "[data-role='studio-review'] [data-role='studio-selection']"
            "[data-decision='USE_DERIVED']"
        ).wait_for(timeout=15_000)
        # The panel shows the translated image in use, exactly the artifact confirmed.
        in_use = studio.locator("[data-role='studio-in-use'] img")
        assert in_use.get_attribute("data-sha256") == artifact_sha
        selection = container.images.current_selection(item.item_id)
        assert selection is not None and selection.decision_origin.value == "OPERATOR"
        decided = {d.sha256: d for d in selection.decisions}[item.detail]
        assert decided.decision is SourceDecisionKind.USE_DERIVED
        assert [(o.role.value, o.asset_kind, o.sha256) for o in selection.outputs] == [
            ("REPRESENTATIVE", ImageAssetKind.SOURCE_ASSET, item.representative),
            ("DETAIL", ImageAssetKind.DERIVED_ARTIFACT, artifact_sha),
        ]
        _no_horizontal_scroll(page)
        page.set_viewport_size(PHONE)
        _no_horizontal_scroll(page)
        page.set_viewport_size(WIDE)

        studio.locator("[data-action='studio-revert']").click()
        studio.locator(
            "[data-role='studio-review'] [data-role='studio-selection'][data-decision='USE_SOURCE']"
        ).wait_for(timeout=15_000)
        selection = container.images.current_selection(item.item_id)
        assert selection is not None
        assert {d.sha256: d.decision for d in selection.decisions}[item.detail] == (
            SourceDecisionKind.USE_SOURCE
        )

    image = f"{BASE}/items/{item.item_id}/images/{item.detail}"
    assert [w for w in writes if not w[1].endswith("/preview")] == [
        ("POST", f"{BASE}/products/{item.group}/translations"),
        ("POST", f"{image}/revisions"),
        ("POST", f"{image}/apply"),
        ("POST", f"{image}/qa"),
        ("POST", f"{image}/select"),
        ("POST", f"{image}/revert"),
    ]
    # The source asset is byte-identical (IT-01), and the translated image is its own artifact.
    assert hashlib.sha256(container.source_assets.read(item.detail)).hexdigest() == item.detail
    assert artifact_sha != item.detail
    # No OCR text and no Korean text in an audit row or a log record (§8).
    with contextlib.closing(raw(config)) as connection:
        dump = "\n".join(str(r) for r in connection.execute("SELECT * FROM audit_events"))
    assert not [t for t in TEXTS if t in dump]
    assert "용량 350ml 대용량" not in dump
    logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert not [t for t in (*TEXTS, "용량 350ml 대용량") if t in logged]
