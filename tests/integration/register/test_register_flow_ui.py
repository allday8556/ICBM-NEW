"""The registration flow's answers in the real UI (2026-10-15 usability check).

- 등록 전송 only queues a CREATE job: the screen says the request was accepted, follows the job the
  server named until it stops, and then shows the unit's read state and the job's cause in Korean
  — never "완료" for a job that died, never the server's English message or its code as the text;
- 등록관리 draws the units as soon as the screen counts and the units arrive, and each slower read
  fills its own place when it answers;
- before anything is saved, the AI category recommendation can still be applied in one press:
  the operator's entries are saved as the preparation first, and the apply then records the
  category as AI_SUGGESTION on it (AIC-04) — never confirmed until the operator's own save.

Every request is answered in-process by the application under test. Where a test needs an answer
the DRY_RUN application cannot give (a send the server offers, a job that died), only that one
answer is replaced in the browser; the screen's own behaviour is what is proven.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route

from app.container import Container
from app.stages.register.category_catalog import CategoryCatalogStore
from app.stages.register.model import ListingShape
from app.stages.register.policy import StaticRegistrationPolicy
from tests.conftest import LOCAL
from tests.integration.ai.test_aic2_category_task import LEAVES, _answer
from tests.integration.ai.test_ais1_provider_profile import (
    FakeComplete,
    FakeProbe,
    _approve_all,
    _serving,
)
from tests.integration.register.test_m5_register_execution import prepare
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    account,
    browser,
    client,
    container,
    prep,
    sources,
)
from tests.support.product_support import Collections, context
from tests.support.register_support import (
    CID,
    OPERATOR,
    Preparation,
    establish,
    ready_item,
    target,
)

pytestmark = pytest.mark.integration

REGISTER = f"{LOCAL}/#/register"
FORWARDED = ("x-icbm-client", "content-type", "accept")
# The answer a replaced route gives: (status, JSON body), or None to let the application answer.
Override = Callable[[str, str, bytes], tuple[int, object] | None]


@contextmanager
def _page(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    calls: list[tuple[str, str]],
    override: Override | None = None,
    held: dict[str, list[Route]] | None = None,
) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        if held is not None and parts.path in held:
            held[parts.path].append(route)
            return
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if parts.path.startswith("/api/"):
            calls.append((request.method, parts.path))
        body = request.post_data_buffer or b""
        replaced = override(request.method, parts.path, body) if override else None
        if replaced is not None:
            status, payload = replaced
            route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
            return
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        response = client.request(request.method, path, content=body, headers=headers)
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        yield page
    finally:
        page.close()


def release(route: Route, client: TestClient) -> None:  # noqa: F811
    """Answer a held request now, with the application's own answer."""
    parts = urlsplit(route.request.url)
    response = client.get(parts.path, headers={"X-ICBM-Client": "pytest"})
    route.fulfill(
        status=response.status_code, headers=dict(response.headers), body=response.content
    )


def test_a_send_is_accepted_followed_and_its_dead_job_explained_in_korean(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    job_id = "job-usability-1"
    polls: list[str] = []

    def override(method: str, path: str, _body: bytes) -> tuple[int, object] | None:
        if method == "GET" and path == "/api/v1/register/overview":
            # The server offers the send (the DRY_RUN application under test does not).
            served = client.get(path, headers={"X-ICBM-Client": "pytest"}).json()
            for unit in served["units"]:
                for action in unit["actions"]:
                    if action["action"] == "CREATE_ENQUEUE":
                        action.update(enabled=True, reason_code=None)
            return 200, served
        if method == "POST" and path == f"/api/v1/register/intents/{ready.intent_id}/create":
            return 200, {"action": "CREATE_ENQUEUE", "intent_id": ready.intent_id, "job_id": job_id}
        if method == "GET" and path == f"/api/v1/system/jobs/{job_id}":
            polls.append(path)
            state = "QUEUED" if len(polls) == 1 else "DEAD"
            return 200, {
                "job_id": job_id,
                "job_type": "register.create",
                "target_ref": f"intent:{ready.intent_id}",
                "state": state,
                "attempt_count": 1,
                "max_attempts": 3,
                "next_attempt_at": None,
                "correlation_id": "c",
                "last_error_class": "POLICY_BLOCKED" if state == "DEAD" else None,
                "last_error_code": "REGISTER_SEND_PREFLIGHT_NOT_READY" if state == "DEAD" else None,
                "last_error_message": (
                    "the preflight is no longer READY under current truth"
                    if state == "DEAD"
                    else None
                ),
                "created_at": "2026-10-15T00:00:00Z",
                "started_at": None,
                "finished_at": None,
                "updated_at": "2026-10-15T00:00:00Z",
                "attempts": [],
            }
        return None

    calls: list[tuple[str, str]] = []
    with _page(browser, client, calls, override) as page:
        page.goto(REGISTER)
        unit = page.locator(f".register-unit[data-draft='{ready.draft_id}']")
        send = unit.locator("button[data-action='CREATE_ENQUEUE']")
        send.wait_for(timeout=15_000)
        assert send.is_enabled()
        send.click()
        # Accepted, never "done": the toast says the request was received.
        toasts = page.locator("#toastStack")
        toasts.get_by_text("전송 요청을 접수했습니다").first.wait_for(timeout=5_000)
        outcome = unit.locator("[data-role='send-outcome']")
        outcome.wait_for()
        page.wait_for_function(
            "() => document.querySelector('[data-role=send-outcome]')?.dataset.state === 'DEAD'",
            timeout=20_000,
        )
        text = outcome.inner_text()
        assert "등록 전송이 중단되었습니다" in text
        assert "보내기 직전에 다시 확인했더니" in text
        # The unit's read state now, as the server labels it.
        assert outcome.locator("[data-read-state]").count() == 1
        # Never the English message, never the code as text, never "완료".
        modal = page.locator("[data-role='send-outcome-modal']")
        assert modal.get_attribute("data-state") == "DEAD"
        for shown in (text, modal.inner_text(), toasts.inner_text()):
            assert "no longer READY" not in shown
            assert "REGISTER_SEND_PREFLIGHT_NOT_READY" not in shown
            assert "등록 전송 완료" not in shown
        assert outcome.locator("[data-reason='REGISTER_SEND_PREFLIGHT_NOT_READY']").count() == 1
    assert ("POST", f"/api/v1/register/intents/{ready.intent_id}/create") in calls
    assert len(polls) >= 2
    assert ("GET", f"/api/v1/register/units/{ready.draft_id}") in calls


def test_the_units_are_drawn_before_the_slower_reads_answer(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    held: dict[str, list[Route]] = {
        "/api/v1/register/canary": [],
        "/api/v1/register/live": [],
        "/api/v1/register/readiness": [],
    }
    calls: list[tuple[str, str]] = []
    with _page(browser, client, calls, held=held) as page:
        page.goto(REGISTER)
        # The queue is on screen while the three reads are still unanswered.
        page.locator("[data-role='register-queue'] tr[data-queue-unit]").first.wait_for(
            timeout=15_000
        )
        page.wait_for_function("() => document.querySelectorAll('[data-loading]').length >= 3")
        for role in ("canary", "live", "readiness"):
            assert page.locator(f".register-loading[data-loading='{role}']").count() == 1
        assert page.locator(".register-canary").count() == 0
        assert page.locator("[data-card='category'] [data-loading]").count() == 1
        assert all(len(routes) == 1 for routes in held.values())
        # Each answer fills its own place.
        release(held["/api/v1/register/canary"][0], client)
        page.locator(".register-canary").wait_for(timeout=10_000)
        assert page.locator(".register-loading[data-loading='live']").count() == 1
        release(held["/api/v1/register/live"][0], client)
        page.locator("[data-role='live-panel']").wait_for(timeout=10_000)
        release(held["/api/v1/register/readiness"][0], client)
        page.locator(".register-readiness").wait_for(timeout=10_000)
        page.locator(f".register-unit[data-draft='{ready.draft_id}']").wait_for(timeout=10_000)
        assert page.locator("[data-loading]").count() == 0
        assert page.locator("[data-card='category'] [data-loading]").count() == 0


def test_before_a_save_the_ai_category_applies_as_a_suggestion_on_a_saved_preparation(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    tmp_path: Path,
) -> None:
    provider = container.ai_provider._provider
    provider._probe = FakeProbe(_serving(tmp_path))
    provider._complete = FakeComplete(value=_answer())
    _approve_all(container.ai_provider)
    catalog = CategoryCatalogStore(container.db, container.clock, container.audit)
    catalog.record(
        "smartstore", LEAVES, endpoint_mapping_revision="test", actor="operator", correlation_id="c"
    )
    snapshot = catalog.current("smartstore")
    assert snapshot is not None
    account_id = establish(container, container.config, "smartstore", "uid-category-presave")
    container.registration_preflight._policies = StaticRegistrationPolicy(
        (
            target(
                account_id,
                marketplace_key="smartstore",
                taxonomy_revision=snapshot.taxonomy_revision,
            ),
        )
    )
    item = ready_item(container, sources, "7003", pricing=context(marketplace_key="smartstore"))
    with container.registrations.transaction() as tx:
        created = tx.create_draft(
            "smartstore",
            account_id,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR,
            correlation_id=CID,
        )
        tx.add_draft_item(
            created.draft_id,
            item.item_id,
            item.pricing_snapshot_id,
            added_by=OPERATOR,
            correlation_id=CID,
        )
    headers = {"X-ICBM-Client": "pytest"}
    unit = client.get(f"/api/v1/register/units/{created.draft_id}", headers=headers).json()[0]
    assert unit["authored"] is None
    calls: list[tuple[str, str]] = []
    with _page(browser, client, calls) as page:
        page.goto(f"{LOCAL}/#/db?product={item.group}")
        page.wait_for_function(
            "() => document.querySelector('[data-action=ai-category-request]')?.dataset.ready"
            " === 'true'"
        )
        page.locator("[data-role='detail-ai-category'] [data-action='ai-category-request']").click()
        page.locator("[data-role='detail-ai-category'] [data-role='ai-category-name']").wait_for(
            timeout=30_000
        )
        page.goto(
            f"{LOCAL}/#/register-editor?draft={created.draft_id}&unit={unit['unit_ref']}"
            "&mode=register"
        )
        block = page.locator("[data-role='editor-ai-category']")
        block.locator("[data-role='ai-category-name']").wait_for(timeout=15_000)
        apply = block.locator("[data-action='ai-category-apply']")
        page.wait_for_function(
            "() => !document.querySelector('[data-action=ai-category-apply]')?.disabled"
        )
        # Not "save first": the recommendation can be applied now, and says what it does.
        why = block.locator("[data-role='ai-category-apply-why']").inner_text()
        assert "먼저 준비 내용을 저장하면" not in why
        assert "준비 내용으로 먼저 저장한 뒤" in why
        before = [call for call in calls if call[0] != "GET"]
        page.fill("input[name='name']", "직접 쓴 상품명")
        apply.click()
        page.locator("[data-role='ai-category-applied']").wait_for(timeout=15_000)
        chosen = page.locator("[data-role='category-current'][data-category='50000803']")
        chosen.wait_for(timeout=10_000)
        assert (
            chosen.locator("[data-role='category-path']").inner_text()
            == "식품 > 식용유/오일 > 들기름"
        )
    stored = container.registrations.preparations_of_draft(created.draft_id)
    assert len(stored) == 1
    preparation_id = stored[0].preparation_id
    # The operator's entries were saved first (no category), then the apply named that revision.
    assert [call for call in calls if call[0] != "GET"][len(before) :] == [
        ("POST", "/api/v1/register/preparations"),
        ("POST", f"/api/v1/register/preparations/{preparation_id}/apply-enrichment"),
    ]
    current = stored[0].current
    assert current.revision_no == 2
    assert current.category is not None
    assert current.category["category_id"] == "50000803"
    # AIC-04: an AI category is a suggestion until the operator's own save confirms it.
    assert current.category["confirmation"] == "AI_SUGGESTION"
    assert current.listing["name"]["value"] == "직접 쓴 상품명"
