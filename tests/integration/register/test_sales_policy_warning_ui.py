"""A sales-policy warning in the Registration Management screen names the supplier and quotes its
words (ADR-0039 §2, §3, P2).

The page runs in the installed browser and every request it makes is answered in-process by the
application under test, on a Draft of one Item whose supplier (KM통상) states a closed-mall
restriction and a minimum resale price, with both of that supplier's policies not applied. Proven:
- the Item's pricing warning shows the supplier's name, its minimum in won and its own label, and
  the user's choice — read from the Item's sales-policy note, the warning never hidden;
- a channel warning, worded by the same seam the preflight reasons use, quotes the supplier's
  ``policy_text``, and a price warning of the preflight finds its Item in its subject;
- a note that does not say what the warning says leaves the static copy (SP-04);
- a source-currency minimum is shown in its own currency, never converted.
No supplier or marketplace is reached and nothing is written by the page.
"""

import contextlib
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.stages.collect.facts import (
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    SalesChannelScope,
    SalesChannelsValue,
)
from app.stages.products.sales_policy import SaveSalesPolicyRequest
from app.stages.register.model import ListingShape
from tests.conftest import LOCAL
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.gate1_support import MARKET, OPERATOR, save_policy
from tests.support.product_support import SUPPLIER, Collections, minimum, product
from tests.support.register_support import establish

pytestmark = pytest.mark.integration

REGISTER = f"{LOCAL}/#/register"
FORWARDED = ("x-icbm-client", "content-type", "accept")
POLICY_TEXT = "폐쇄몰 전용 상품입니다"
NOT_APPLIED_CHANNELS = "이 공급처의 판매정책을 적용하지 않도록 설정했습니다 (사용자 책임)"
NOT_APPLIED_MINIMUM = "이 공급처의 최저판매가를 적용하지 않도록 설정했습니다 (사용자 책임)"


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
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


@pytest.fixture
def draft(api: TestClient, container: Container, config: AppConfig) -> tuple[str, str]:
    """A Draft of one Item whose supplier's two policies are not applied; the Draft and Item."""
    account = establish(container, config, MARKET, "provider-account-1")
    save_policy(api, account)
    container.sales_policies.save(
        SUPPLIER,
        SaveSalesPolicyRequest(
            expected_revision_no=None,
            apply_channel_restrictions=False,
            apply_minimum_price=False,
        ),
        actor=OPERATOR,
        correlation_id="cid-p2",
    )
    channels = FieldFact(
        FieldStatus.CONFIRMED,
        SalesChannelsValue(scope=SalesChannelScope.CLOSED_MALL_ONLY, policy_text=POLICY_TEXT),
        (Evidence(EvidenceKind.DOM_TEXT, "th:판매가능플랫폼 + td", FieldStatus.CONFIRMED),),
    )
    run_id, _ = Collections.of(container, config).collect(
        product(sales_channels=channels, minimum_sale_price=minimum(50000))
    )
    item_id = container.materializer.materialize_run(run_id).item_id
    assert item_id is not None
    policy = container.registration_preflight.target_policy(MARKET, account)
    assert policy is not None
    snapshot = container.pricing.price(item_id, policy.pricing_context).snapshot
    assert snapshot is not None and not snapshot.minimum_applied
    with container.registrations.transaction() as unit:
        created = unit.create_draft(
            MARKET,
            account,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR,
            correlation_id="cid-p2",
        )
        unit.add_draft_item(
            created.draft_id,
            item_id,
            snapshot.pricing_snapshot_id,
            added_by=OPERATOR,
            correlation_id="cid-p2",
        )
    return created.draft_id, item_id


@contextlib.contextmanager
def _page(
    browser: Browser, client: TestClient, calls: list[tuple[str, str, int]]
) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        response = client.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        if parts.path.startswith("/api/"):
            calls.append((request.method, parts.path, response.status_code))
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(REGISTER)
        page.wait_for_selector(".register-canary", timeout=15_000)
        yield page
    finally:
        page.close()


# Words one detached node through the page's own seam, as a preflight reason node is worded.
PROBE = """async ([id, reason]) => {
  const m = await import('/js/pages/register.js');
  const node = document.createElement('div');
  node.id = id;
  node.textContent = m.REASON_COPY[reason.code];
  document.body.append(node);
  m.quotePolicyWarning(node, reason);
}"""


def _probe(page: Page, probe_id: str, reason: dict[str, Any], state: str) -> str:
    page.evaluate(PROBE, [probe_id, reason])
    node = page.wait_for_selector(
        f"#{probe_id}[data-policy-note='{state}']", state="attached", timeout=10_000
    )
    assert node is not None
    return node.inner_text()


def test_the_warnings_name_the_supplier_and_quote_its_words(
    browser: Browser, api: TestClient, draft: tuple[str, str]
) -> None:
    draft_id, item_id = draft
    calls: list[tuple[str, str, int]] = []
    note_path = f"/api/v1/products/items/{item_id}/sales-policy-note"
    with _page(browser, api, calls) as page:
        unit = page.locator(f".register-unit[data-draft='{draft_id}']")
        assert unit.count() == 1
        warning = unit.locator(
            f"tr[data-item='{item_id}'] [data-reason='PRICING_MINIMUM_SALE_PRICE_NOT_APPLIED']"
        )
        assert warning.count() == 1
        assert warning.get_attribute("data-reason-status") == "READY"
        page.wait_for_selector(
            f".register-unit[data-draft='{draft_id}'] [data-policy-note='quoted']",
            state="attached",
            timeout=10_000,
        )
        quoted = f"KM통상 최저판매가 50,000원 (공급사 표기: “minimum”) — {NOT_APPLIED_MINIMUM}"
        assert warning.text_content() == quoted
        assert warning.get_attribute("title") == quoted
        assert warning.get_attribute("data-supplier") == SUPPLIER
        assert warning.get_attribute("data-policy-item") == item_id
        # A channel warning of the preflight names its Item as its subject; a price warning of
        # the preflight as ``item:{id}:minimum_sale_price``. Both read the same note.
        channel = _probe(
            page,
            "probe-channel",
            {"code": "SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED", "subject": item_id},
            "quoted",
        )
        assert channel == f"KM통상의 판매채널 제한: “{POLICY_TEXT}” — {NOT_APPLIED_CHANNELS}"
        price = _probe(
            page,
            "probe-price",
            {
                "code": "M4_PRICING.PRICING_MINIMUM_SALE_PRICE_NOT_APPLIED",
                "subject": f"item:{item_id}:minimum_sale_price",
            },
            "quoted",
        )
        assert price == quoted
        # The note says the statement is CONFIRMED: an "unread" warning keeps its static copy.
        static = _probe(
            page,
            "probe-static",
            {"code": "SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED", "subject": item_id},
            "static",
        )
        assert static.startswith("공급사 판매채널 문구를 확인할 수 없지만")
        # A source-currency minimum keeps its own currency; nothing is converted.
        foreign = page.evaluate(
            """async () => {
              const m = await import('/js/pages/register.js');
              return m.policyWarningCopy('PRICING_MINIMUM_SALE_PRICE_NOT_APPLIED', {
                supplier_key: 'ali1688', supplier_name: '1688', apply_minimum_price: false,
                minimum_sale_price: {
                  status: 'CONFIRMED', label: '最低售价', currency: 'CNY', amount: '12.50',
                },
              });
            }"""
        )
        assert (
            foreign == f"1688 최저판매가 ¥12.50 (공급사 표기: “最低售价”) — {NOT_APPLIED_MINIMUM}"
        )
    # One read of the note per Item per render, and the page wrote nothing.
    assert [c for c in calls if c[1] == note_path] == [("GET", note_path, 200)]
    assert all(method == "GET" for method, _, _ in calls)
    assert not any(status >= 500 for _, _, status in calls)
