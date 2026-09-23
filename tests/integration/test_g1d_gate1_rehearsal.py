"""Gate 1 end to end, provider-zero, from a fresh data root (ADR-0015 §6; Issue #89 5796323069 §12).

One operator path, driven through the real screens of a real application over an empty, freshly
migrated data root, then a reload, then a restart of the process on the same root:

    COLLECT UI submit → RECORDED → Product DB list/detail → select Product and Items
    → choose the durable target account and policy → the G1-D command → M4 PricingSnapshot pins
    → RegistrationDraft → the existing preparation authoring → the server preflight
    → reload → restart → the same durable Draft, preparation and pins, and the same derived truth

The controlled fixture establishes only the local owners the path needs: a canonical account from
a committed M2 binding unit, a target policy and operator-reviewed category metadata saved through
their durable Settings owners, and a non-regulated synthetic product from the scripted shop. It is
**not** a ComplianceGate PASS, and nothing is frozen, sent or listed to make it pass.

What it proves is narrow on purpose: the two local owner gaps Gate 1 closes are closed —
``REGISTER_TARGET_POLICY_MISSING`` and ``CATEGORY_METADATA_MISSING`` do not appear — while every
other reason the preflight still gives is reported as it is, not claimed away. Gate 1 needs an
authored preparation and a reachable candidate, not a freezable unit (ADR-0015 §6, decision
5800619183): the two authoring revisions have no owner, so they are authored as ``null``, the
candidate reports ``AUTHORING_REVISIONS_UNOWNED`` and FREEZE stays disabled.
"""

import contextlib
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.register.preparation import AUTHORING_REVISIONS_UNOWNED
from tests.collect_submit_support import (
    ScriptedShop,
    product_url,
    sellable_registered,
    served,
)
from tests.conftest import LOCAL
from tests.gate1_support import CATEGORY, MARKET, record_reviewed_metadata, save_policy
from tests.product_support import raw
from tests.register_support import establish

pytestmark = pytest.mark.integration

BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
FORWARDED = ("x-icbm-client", "content-type", "accept")
GATE1_GAPS = ("REGISTER_TARGET_POLICY_MISSING", "CATEGORY_METADATA_MISSING")
NEVER_IN_GATE1 = (
    "registration_snapshots",
    "registration_item_snapshots",
    "registration_batches",
    "registration_intents",
    "registration_attempts",
)
RECHECK_LIMIT = 40


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
def _page(browser: Browser, client: TestClient, seen: list[tuple[str, str]]) -> Iterator[Page]:
    def answer(route: Any) -> None:
        request = route.request
        seen.append((request.method, request.url))
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        response = client.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        yield page
    finally:
        page.close()


def _counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def _await_materialized(page: Page) -> None:
    """The RECORDED run's handoff, followed by bounded read-only rechecks (G1-E)."""
    for _ in range(RECHECK_LIMIT):
        handoff = page.locator("[data-role='run-focus'] [data-role='handoff']")
        handoff.wait_for(timeout=15_000)
        if handoff.get_attribute("data-state") == "MATERIALIZED":
            return
        page.locator("[data-action='recheck-product']").click()
        page.wait_for_timeout(250)
    raise AssertionError("the Product did not become visible")


def _preflight_codes(page: Page, draft_id: str) -> tuple[str | None, list[str]]:
    block = page.locator(f".register-unit[data-draft='{draft_id}'] .register-preflight")
    block.wait_for(timeout=15_000)
    codes = [
        str(code.get_attribute("data-reason"))
        for code in block.locator("[data-reason]").all()
        if code.get_attribute("data-reason")
    ]
    return block.get_attribute("data-preflight"), sorted(codes)


def _durable(container: Container, draft_id: str) -> dict[str, Any]:
    """The durable state the path produced, as the owners hold it."""
    draft = container.registrations.draft(draft_id)
    assert draft is not None
    preparations = container.registrations.preparations_of_draft(draft_id)
    return {
        "draft": (draft.marketplace_key, draft.marketplace_account_id, draft.listing_shape.value),
        "draft_revision": draft.draft_revision,
        "pins": [(i.item_id, i.pricing_snapshot_id) for i in draft.items],
        "preparations": [
            (p.preparation_id, p.current.preparation_revision_id, p.current.revision_no)
            for p in preparations
        ],
    }


def test_gate1_from_a_fresh_data_root_through_reload_and_restart(
    browser: Browser, config: AppConfig
) -> None:
    assert _counts(config)["collection_runs"] == 0, "a fresh data root"
    seen: list[tuple[str, str]] = []
    evidence: dict[str, Any] = {}
    with served(config, ScriptedShop(), collections=(sellable_registered(),)) as client:
        container: Container = client.app.state.container  # type: ignore[attr-defined]
        # The local owners the path needs, recorded the way the operator records them.
        account = establish(container, config, MARKET, "provider-account-rehearsal")
        evidence["policy_revision"] = save_policy(client, account)
        evidence["metadata_revision"] = record_reviewed_metadata(client)
        with _page(browser, client, seen) as page:
            # COLLECT UI submit → RECORDED → the Product DB.
            page.goto(f"{LOCAL}/#/collect?view=jobs")
            page.locator("#collect-url").fill(product_url("4242"))
            page.locator("[data-action='submit-collection']").click()
            page.wait_for_selector(
                "[data-role='run-focus'][data-outcome='RECORDED']", timeout=20_000
            )
            evidence["collection_run_id"] = page.locator("[data-role='run-focus']").get_attribute(
                "data-run"
            )
            _await_materialized(page)
            page.locator("[data-action='open-product']").click()
            detail = page.locator("[data-role='product-detail'][data-state='ready']")
            detail.wait_for(timeout=15_000)
            group = str(detail.get_attribute("data-product"))
            evidence["product_group_id"] = group
            # Select the Product's Items, the durable target and a listing shape; run G1-D.
            for box in page.locator(
                "[data-role='product-detail'] input[type='checkbox']:not([disabled])"
            ).all():
                box.check()
            option = page.locator(f"#draft-target option[data-account='{account}']")
            option.wait_for(state="attached")
            page.select_option("#draft-target", value=option.get_attribute("value"))
            page.select_option("#draft-shape", "SINGLE_LISTING_WITH_OPTIONS")
            page.locator("[data-action='create-draft']").click()
            result = page.locator("[data-role='draft-result'][data-state='created']")
            result.wait_for(timeout=15_000)
            draft_id = str(result.get_attribute("data-draft"))
            evidence["draft_id"] = draft_id
            # The existing preparation authoring, from the opened Draft.
            page.locator("[data-action='open-draft']").click()
            unit = page.locator(f".register-unit[data-draft='{draft_id}'][aria-current='true']")
            unit.wait_for(timeout=15_000)
            assert unit.get_attribute("data-preparation") == "DRAFTED"
            items = [i.item_id for i in container.registrations.draft(draft_id).items]  # type: ignore[union-attr]
            unit.locator("input[name='category_id']").fill(CATEGORY)
            unit.locator("input[name='name']").fill("리허설 합성 상품")
            unit.locator("input[name='attribute.brand']").fill("합성 브랜드")
            unit.locator("input[name='notice.manufacturer']").fill("합성 제조사")
            unit.locator("input[data-detail-reference='notice'][data-field-key='origin']").check()
            unit.locator("textarea[name='detail_body']").fill("리허설 상세 본문")
            unit.locator("input[data-option-dimension]").fill("수량")
            for index, item_id in enumerate(items):
                unit.locator(f"input[data-option-item='{item_id}']").fill(f"{index + 1}개")
            unit.locator("button[data-action='SAVE_PREPARATION']").click()
            page.wait_for_selector(
                f".register-unit[data-draft='{draft_id}'] .register-authoring[data-preparation]"
                ":not([data-preparation=''])",
                timeout=15_000,
            )
            # The server preflight: the operator's Preflight 평가, and the owner's own answer.
            page.locator(
                f".register-unit[data-draft='{draft_id}'] button[data-action='EVALUATE']"
            ).click()
            page.wait_for_timeout(500)
            status, codes = _preflight_codes(page, draft_id)
            evaluated = container.register.evaluate_preparation(
                container.registrations.preparations_of_draft(draft_id)[0].preparation_id
            )
            assert evaluated.preflight is not None
            assert sorted(evaluated.preflight.reason_codes) == codes
            evidence["preflight"] = {"status": status, "reason_codes": codes}
            for gap in GATE1_GAPS:
                assert gap not in codes, (gap, codes)
            # The authoring revisions have no owner (5800619183): authored as null, reported,
            # and nothing can be frozen.
            assert AUTHORING_REVISIONS_UNOWNED in codes
            assert status != "READY"
            authored = container.registrations.preparations_of_draft(draft_id)[0].current
            assert authored.category["mapping_revision"] is None
            assert authored.detail is not None and authored.detail["composition_revision"] is None
            freeze = page.locator(
                f".register-unit[data-draft='{draft_id}'] button[data-action='FREEZE']"
            )
            assert freeze.is_disabled()
            # Reload: the same durable truth, and nothing sent again.
            posts_before_reload = [s for s in seen if s[0] != "GET"]
            page.reload()
            assert _preflight_codes(page, draft_id) == (status, codes)
            assert [s for s in seen if s[0] != "GET"] == posts_before_reload
        before_restart = _durable(container, draft_id)
        counts_before_restart = _counts(config)
    # Restart the process on the same data root.
    with served(config, ScriptedShop(), collections=(sellable_registered(),)) as client:
        container = client.app.state.container  # type: ignore[attr-defined]
        assert _durable(container, draft_id) == before_restart
        with _page(browser, client, seen) as page:
            page.goto(f"{LOCAL}/#/register?draft={draft_id}")
            assert _preflight_codes(page, draft_id) == (status, codes)
        counts_after_restart = _counts(config)
        assert (
            counts_after_restart["pricing_snapshots"] == counts_before_restart["pricing_snapshots"]
        )
        assert container.jobs.count(job_type_prefix="register.") == 0
    assert {t: counts_after_restart[t] for t in NEVER_IN_GATE1} == dict.fromkeys(NEVER_IN_GATE1, 0)
    assert counts_after_restart["registration_drafts"] == 1
    assert counts_after_restart["registration_draft_items"] == 2
    assert counts_after_restart["registration_preparations"] == 1
    # Everything the browser asked for was this application; the scripted shop sent nothing.
    assert all(urlsplit(url).netloc == urlsplit(LOCAL).netloc for _, url in seen)
    posts = sorted({urlsplit(url).path for method, url in seen if method == "POST"})
    assert posts == [
        "/api/v1/collect/collections",
        "/api/v1/register/drafts",
        "/api/v1/register/preparations",
        f"/api/v1/register/preparations/{before_restart['preparations'][0][0]}/evaluate",
    ]
    evidence["durable"] = before_restart
    print("GATE1_REHEARSAL " + json.dumps(evidence, sort_keys=True, default=str))
