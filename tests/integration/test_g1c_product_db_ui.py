"""The product DB screen in the real UI (Gate 1 G1-C, ADR-0015 §5, Issue #89 5792525426).

The page runs in the installed browser and every request it makes is answered in-process by the
application under test; a response can be held back to replay a slow network. Proven here:
- the operator lists, searches and pages Products and reads one Product's detail, member by
  member; a retired Product is readable by its identifier and nothing of it is selectable;
- a late detail response for Product A, arriving after the operator moved to Product B, is
  discarded: A's data never appears under B;
- Items chosen on A are cleared the moment the operator moves to B, and never come back;
- a registration-target check is a read: it sends no write, creates no Draft or registration row,
  and a reload shows server state with nothing chosen; a membership that moved after the choice is
  refused by the server and the operator re-reads the Product.

No supplier, marketplace or AI provider is reached.
"""

import contextlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.collect.facts import (
    FieldFact,
    FieldStatus,
    ImageIssue,
    ImageReference,
    ImageRole,
    QuantityTier,
    QuantityTiersValue,
    TextValue,
)
from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.products.materialization import MaterializationStatus
from app.products.model import MoveReason
from tests.collect_support import confirmed
from tests.conftest import LOCAL
from tests.product_support import SUPPLIER, Collections, product, raw

pytestmark = pytest.mark.integration

BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
SCREEN = f"{LOCAL}/#/db"
FORWARDED = ("x-icbm-client", "content-type", "accept")
DETAIL = "[data-role='product-detail']"
ROWS = "[data-role='product-list'] tr[data-product]"
ROW_COUNT = f'(n) => document.querySelectorAll("{ROWS}").length === n'
# What a registration choice must never create in G1-C: no Draft, no price pin, no registration row.
NEVER_CREATED = (
    "pricing_snapshots",
    "registration_drafts",
    "registration_draft_items",
    "registration_preparations",
    "registration_preparation_revisions",
    "registration_snapshots",
    "registration_item_snapshots",
    "registration_batches",
    "registration_intents",
    "registration_attempts",
)


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
def _served(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


def _fulfill(route: Route, client: TestClient) -> None:
    request = route.request
    parts = urlsplit(request.url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
    response = client.request(
        request.method, path, content=request.post_data_buffer, headers=headers
    )
    route.fulfill(
        status=response.status_code, headers=dict(response.headers), body=response.content
    )


@contextmanager
def _page(
    browser: Browser,
    client: TestClient,
    writes: list[tuple[str, str]],
    held: dict[str, list[Route]] | None = None,
    url: str = SCREEN,
) -> Iterator[Page]:
    """The screen, with every request answered in-process. A request whose path is a key of
    ``held`` is parked there unanswered until the test fulfils it."""

    def answer(route: Route) -> None:
        parts = urlsplit(route.request.url)
        if route.request.method != "GET":
            writes.append((route.request.method, parts.path))
        for path, parked in (held or {}).items():
            if parts.path == path:
                parked.append(route)
                return
        _fulfill(route, client)

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(url)
        page.wait_for_selector(ROWS, timeout=15_000)
        yield page
    finally:
        page.close()


# ---------------------------------------------------------------- synthetic Products


def _named(name: str, **overrides: FieldFact) -> dict[str, FieldFact]:
    return product(original_name=confirmed(TextValue(text=name), ".name"), **overrides)


def _tiers(*quantities: int) -> FieldFact:
    return confirmed(
        QuantityTiersValue(
            tiers=tuple(
                QuantityTier(quantity=q, total_price_krw=q * 9900, label=f"{q}") for q in quantities
            )
        ),
        ".tiers",
    )


class Catalog:
    """Products materialized by the M4 owner in the served application."""

    def __init__(self, client: TestClient, config: AppConfig) -> None:
        self.container: Container = client.app.state.container  # type: ignore[attr-defined]
        self.sources = Collections.of(self.container, config)

    def make(
        self,
        source_id: str,
        fields: dict[str, FieldFact],
        extra_images: tuple[ImageReference, ...] = (),
    ) -> str:
        run_id, _ = self.sources.collect(
            fields, source_product_id=source_id, extra_images=extra_images
        )
        result = self.container.materializer.materialize_run(run_id)
        assert result.status is MaterializationStatus.MATERIALIZED, result
        return str(result.product_group_id)

    def join(self, group: str, source_id: str, name: str) -> None:
        _, revision = self.sources.collect(_named(name), source_product_id=source_id)
        store = self.container.product_store
        uid = store.source_product(SUPPLIER, source_id).source_product_uid
        store.record_move(
            uid, revision.revision_id, reason=MoveReason.INITIAL, decided_by="t", correlation_id="c"
        )
        store.confirm_new_member(group, uid, reason="t-join", decided_by="t", correlation_id="c")

    def standard(self) -> dict[str, str]:
        made = {
            "tiered": self.make("S-TIER", _named("묶음 참기름", quantity_tiers=_tiers(1, 2, 3))),
            "a": self.make("S-A", _named("에이 전용 상품명")),
            "b": self.make("S-B", _named("비 전용 상품명")),
        }
        # A newer revision drops the 3-pack: that Item stays, unbound.
        self.make("S-TIER", _named("묶음 참기름", quantity_tiers=_tiers(1, 2)))
        return made


def _row(page: Page, group: str) -> str:
    return f"{ROWS}[data-product='{group}']"


def _ready(page: Page, group: str) -> None:
    page.wait_for_selector(f"{DETAIL}[data-product='{group}'][data-state='ready']")


def _open(page: Page, group: str) -> None:
    page.locator(_row(page, group)).click()
    _ready(page, group)


def _counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


# ---------------------------------------------------------------- the screen


def test_the_operator_lists_searches_pages_and_reads_a_product_member_by_member(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client:
        catalog = Catalog(client, config)
        made = catalog.standard()
        catalog.join(made["a"], "S-A-2", "에이 두번째 원천")
        for n in range(19):
            catalog.make(f"S-{n:02d}", _named(f"채움 상품 {n:02d}"))
        with _page(browser, client, writes) as page:
            assert page.locator(ROWS).count() == 20
            assert page.locator("[data-role='matching-total']").inner_text() == "22개"
            assert page.locator("[data-action='previous-page']").is_disabled()
            first_page = [
                page.locator(ROWS).nth(i).get_attribute("data-product") for i in range(20)
            ]
            page.locator("[data-action='next-page']").click()
            page.wait_for_selector("[data-role='page-number']:has-text('2페이지')")
            page.wait_for_function(ROW_COUNT, arg=2)
            second_page = [
                page.locator(ROWS).nth(i).get_attribute("data-product") for i in range(2)
            ]
            assert not set(first_page) & set(second_page)
            assert page.locator("[data-action='next-page']").is_disabled()
            page.locator("[data-action='previous-page']").click()
            page.wait_for_selector("[data-role='page-number']:has-text('1페이지')")

            page.locator("input[type='search']").fill("참기름")
            page.locator("[data-role='db-search'] button[type='submit']").click()
            page.wait_for_function(ROW_COUNT, arg=1)
            assert page.locator(ROWS).first.get_attribute("data-product") == made["tiered"]
            _open(page, made["tiered"])
            items = page.locator(f"{DETAIL} tr[data-item]")
            assert items.count() == 3
            unbound = page.locator(f"{DETAIL} tr[data-selectable='false']")
            assert unbound.count() == 1
            assert (
                unbound.locator("[data-reason]").get_attribute("data-reason")
                == "PRODUCTS_ITEM_BINDING_MISSING"
            )
            assert unbound.locator("input[type='checkbox']").is_disabled()

            page.locator("input[type='search']").fill("에이")
            page.locator("[data-role='db-search'] button[type='submit']").click()
            page.wait_for_selector(_row(page, made["a"]))
            _open(page, made["a"])
            members = page.locator(f"{DETAIL} .db-member")
            assert members.count() == 2
            texts = [members.nth(i).inner_text() for i in range(2)]
            assert "에이 전용 상품명" in texts[0] and "에이 두번째 원천" in texts[1]
            served = catalog.container.product_store
            for i, source_id in enumerate(("S-A", "S-A-2")):
                uid = served.source_product(SUPPLIER, source_id).source_product_uid
                assert members.nth(i).get_attribute("data-revision") == (
                    served.current_source_revision(uid)
                )
    assert writes == []


def test_a_retired_product_is_readable_by_its_identifier_and_nothing_of_it_is_selectable(
    browser: Browser, config: AppConfig
) -> None:
    with _served(config) as client:
        made = Catalog(client, config).standard()
        with contextlib.closing(raw(config)) as connection:
            connection.execute(
                "UPDATE product_groups SET status = 'RETIRED', retired_at = '2026-09-23 00:00:00'"
                " WHERE product_group_id = ?",
                (made["b"],),
            )
            connection.commit()
        with _page(browser, client, [], url=f"{SCREEN}?product={made['b']}") as page:
            _ready(page, made["b"])
            assert page.locator(_row(page, made["b"])).count() == 0
            assert "보관됨" in page.locator(f"{DETAIL} .detail-title").inner_text()
            note = page.locator(f"{DETAIL} .note[data-reason]")
            assert note.get_attribute("data-reason") == "PRODUCTS_PRODUCT_RETIRED"
            boxes = page.locator(f"{DETAIL} input[type='checkbox']")
            assert boxes.count() == 1 and boxes.first.is_disabled()
            assert page.locator("[data-action='check-registration-target']").is_disabled()


# A detail image the collection never finished fetching: unresolved, so the images field stays
# under review although the representative image was stored and included.
UNRESOLVED_DETAIL = ImageReference(
    role=ImageRole.DETAIL,
    ordinal=0,
    host="img.shop.example",
    provenance="#prdDetail img:nth-of-type(1)",
    status=FieldStatus.REVIEW_REQUIRED,
    issue=ImageIssue.BUDGET_EXHAUSTED,
)


def test_an_images_field_under_review_is_shown_as_under_review_beside_its_representative(
    browser: Browser, config: AppConfig
) -> None:
    with _served(config) as client:
        catalog = Catalog(client, config)
        review = catalog.make("S-IMG", _named("이미지 확인 상품"), (UNRESOLVED_DETAIL,))
        settled = catalog.make("S-OK", _named("이미지 정상 상품"))
        # The real current revision: its representative is included, and the field is under review.
        images = catalog.container.products.detail(review).member_sources[0].images
        assert images is not None and images.status is FieldStatus.REVIEW_REQUIRED
        assert images.representative_sha256 is not None
        assert (images.included, images.references) == (1, 2)
        with _page(browser, client, [], url=f"{SCREEN}?product={review}") as page:
            _ready(page, review)
            shown = page.locator(f"{DETAIL} [data-fact='images']")
            assert shown.get_attribute("data-status") == "REVIEW_REQUIRED"
            text = shown.inner_text()
            assert f"대표 {images.representative_sha256[:8]} · 1/2" in text
            assert shown.locator(".chip.warn").inner_text() == "확인 필요"
            # A settled images field shows its representative and no review state.
            _open(page, settled)
            shown = page.locator(f"{DETAIL} [data-fact='images']")
            assert shown.get_attribute("data-status") == "CONFIRMED"
            assert "대표 " in shown.inner_text()
            assert shown.locator(".chip").count() == 0


def test_a_late_detail_for_another_product_is_never_rendered(
    browser: Browser, config: AppConfig
) -> None:
    with _served(config) as client:
        made = Catalog(client, config).standard()
        held: dict[str, list[Route]] = {f"/api/v1/products/{made['a']}/detail": []}
        with _page(browser, client, [], held) as page:
            page.locator(_row(page, made["a"])).click()
            page.wait_for_selector(f"{DETAIL}[data-product='{made['a']}'][data-state='loading']")
            # The operator moves on to B while A's detail is still on the wire.
            _open(page, made["b"])
            parked = held[f"/api/v1/products/{made['a']}/detail"]
            assert len(parked) == 1
            _fulfill(parked[0], client)
            page.wait_for_timeout(600)
            panel = page.locator(DETAIL)
            assert panel.get_attribute("data-product") == made["b"]
            assert panel.get_attribute("data-state") == "ready"
            assert "비 전용 상품명" in panel.inner_text()
            assert "에이 전용 상품명" not in panel.inner_text()
            assert page.locator(f"{DETAIL} .db-member h4").inner_text().endswith("S-B")
            assert (
                page.locator("[data-role='selection']").get_attribute("data-product") == made["b"]
            )


def test_chosen_items_are_cleared_when_the_product_changes(
    browser: Browser, config: AppConfig
) -> None:
    with _served(config) as client:
        made = Catalog(client, config).standard()
        held: dict[str, list[Route]] = {f"/api/v1/products/{made['b']}/detail": []}
        with _page(browser, client, [], held) as page:
            _open(page, made["tiered"])
            for box in page.locator(f"{DETAIL} input[type='checkbox']:not([disabled])").all():
                box.check()
            selection = page.locator("[data-role='selection']")
            assert selection.get_attribute("data-product") == made["tiered"]
            assert selection.get_attribute("data-count") == "2"
            # The moment the operator picks B, nothing of the tiered Product's choice remains,
            # even before B's detail arrives.
            page.locator(_row(page, made["b"])).click()
            page.wait_for_selector(f"{DETAIL}[data-product='{made['b']}'][data-state='loading']")
            assert page.locator(f"{DETAIL} input[type='checkbox']").count() == 0
            assert page.locator(f"{DETAIL} [data-role='selection']").count() == 0
            _fulfill(held[f"/api/v1/products/{made['b']}/detail"][0], client)
            _ready(page, made["b"])
            assert page.locator(f"{DETAIL} input:checked").count() == 0
            assert selection.get_attribute("data-product") == made["b"]
            assert selection.get_attribute("data-count") == "0"
            # Going back reads the tiered Product fresh, with nothing chosen.
            _open(page, made["tiered"])
            assert page.locator(f"{DETAIL} input:checked").count() == 0
            assert selection.get_attribute("data-count") == "0"


def test_a_target_check_is_a_read_and_a_reload_keeps_nothing_chosen(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client:
        catalog = Catalog(client, config)
        made = catalog.standard()
        before = _counts(config)
        with _page(browser, client, writes) as page:
            _open(page, made["tiered"])
            for box in page.locator(f"{DETAIL} input[type='checkbox']:not([disabled])").all():
                box.check()
            page.locator("[data-action='check-registration-target']").click()
            result = page.locator(
                f"[data-role='target-result'][data-state='confirmed'][data-product='{made['tiered']}']"
            )
            result.wait_for()
            assert page.locator("[data-target-item]").count() == 2
            page.reload()
            page.wait_for_selector(ROWS)
            assert page.locator(DETAIL).get_attribute("data-state") == "idle"
            assert page.locator("input:checked").count() == 0

            # The membership moves after the operator chose: the server refuses the stale choice.
            _open(page, made["a"])
            page.locator(f"{DETAIL} input[type='checkbox']").first.check()
            catalog.join(made["a"], "S-A-2", "에이 두번째 원천")
            page.locator("[data-action='check-registration-target']").click()
            refused = page.locator("[data-role='target-result'][data-state='refused']")
            refused.wait_for()
            assert (
                refused.locator("[data-reason]").get_attribute("data-reason")
                == "PRODUCTS_SELECTION_MEMBERSHIP_STALE"
            )
            refused.locator("[data-action='reload-detail']").click()
            page.wait_for_selector(f"{DETAIL}[data-product='{made['a']}'][data-state='ready']")
            membership = catalog.container.product_store.current_membership_revision(made["a"])
            assert membership is not None
            assert page.locator(DETAIL).get_attribute("data-membership") == (
                membership.membership_revision_id
            )
            assert page.locator(f"{DETAIL} input:checked").count() == 0
        after = _counts(config)
    assert writes == []
    assert {t: after[t] for t in NEVER_CREATED} == dict.fromkeys(NEVER_CREATED, 0)
    # Only the membership the test itself made moved: the screen wrote nothing.
    changed = {t for t in after if after[t] != before[t]}
    assert changed <= {
        "source_products",
        "product_facts_revisions",
        "product_facts_fields",
        "product_facts_evidence",
        "product_facts_image_refs",
        "collection_runs",
        "current_source_revision_moves",
        "group_members",
        "group_membership_revisions",
        "group_change_events",
    }, changed
