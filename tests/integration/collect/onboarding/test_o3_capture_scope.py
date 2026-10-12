"""ADR-0036 §5 step 3 (slice O3): a sample capture keeps only what the template reads.

The capture keeps text only inside the template's product regions (``capture_regions``), the tag
structure with ``id``/``class``/``style`` everywhere else, and an attribute allowlist. These tests
prove the regions are complete on the repository's real storefront fixtures (U-PICK on Cafe24,
건강산 on Godomall) and on the synthetic trial page: the template reads the capture exactly as it
reads the whole page — every field status, the facts fingerprint and summary, the structural
flags and the product image hosts — while the member's text and every other URL are gone.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

from app.config import AppConfig
from app.container import Container
from app.stages.collect.onboarding.analysis import TrialReport, analyse
from app.stages.collect.onboarding.capture import sanitize
from tests.support.jobs_support import FakeClock
from tests.support.onboarding_trial_support import (
    CAFE24_ORIGIN,
    GODOMALL_ORIGIN,
    MEMBER_EMAIL,
    MEMBER_NAME,
    SESSION_VALUE,
    StubSessions,
    TrialShop,
    cafe24_page,
    cafe24_url,
    ggsan_page,
    godomall_url,
    recon_site,
    trial_container,
    upick_page,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def app(config: AppConfig, clock: FakeClock) -> Iterator[Container]:
    with trial_container(config, clock, TrialShop(), StubSessions()) as built:
        yield built


@dataclass(frozen=True)
class _Reader:
    capture: Callable[[str], str]
    read: Callable[[str], TrialReport]


def _reader(app: Container, origin: str, template: str, url: str) -> _Reader:
    key = recon_site(app, origin, template)
    entry = app.supplier_catalog.onboarded(key)
    assert entry is not None and entry.bound is not None and entry.record is not None
    bound = entry.bound
    analysis = bound.template.analysis
    assert analysis is not None
    overrides = bound.config.label_overrides
    words = {
        slot: defaults + tuple(w for w in overrides.get(slot, ()) if w not in defaults)
        for slot, defaults in bound.template.label_slots.items()
    }

    def read(body: str) -> TrialReport:
        return analyse(
            body=body,
            sample_url=url,
            storefront_host=entry.record.storefront_host,  # type: ignore[union-attr]
            collection=bound.collection,
            analysis=analysis,
            words=words,
        )

    def capture(body: str) -> str:
        return sanitize(
            body,
            regions=analysis.capture_regions(bound.config),
            base_url=url,
            safe_query_keys=bound.collection.profile.safe_query_keys,
        )

    return _Reader(capture, read)


PAGES = [
    pytest.param("cafe24", lambda: upick_page("4954"), cafe24_url(4954), id="upick-4954"),
    pytest.param("cafe24", lambda: upick_page("4082"), cafe24_url(4082), id="upick-4082"),
    pytest.param("cafe24", lambda: cafe24_page(103), cafe24_url(103), id="cafe24-synthetic"),
    pytest.param(
        "godomall", lambda: ggsan_page("1000002412"), godomall_url(1000002412), id="ggsan-2412"
    ),
    pytest.param(
        "godomall", lambda: ggsan_page("1000004918"), godomall_url(1000004918), id="ggsan-4918"
    ),
]


@pytest.mark.parametrize(("template", "page", "url"), PAGES)
def test_the_template_reads_the_capture_exactly_as_the_whole_page(
    app: Container, template: str, page: Callable[[], str], url: str
) -> None:
    origin = CAFE24_ORIGIN if template == "cafe24" else GODOMALL_ORIGIN
    reader = _reader(app, origin, template, url)
    body = page()
    whole = reader.read(body)
    kept = reader.read(reader.capture(body))
    assert dict(kept.statuses) == dict(whole.statuses)
    assert kept.fingerprint == whole.fingerprint
    assert dict(kept.summary) == dict(whole.summary)
    assert kept.structural == whole.structural
    # Product image and login hosts are observed exactly; an asset host only a layout image
    # outside the product regions used is not (asset hosts are shown, never confirmed).
    assert _hosts(kept, "image", "login") == _hosts(whole, "image", "login")
    assert {h for h, _ in _hosts(kept, "asset")} <= {h for h, _ in _hosts(whole, "asset")}
    # Unread rows inside the product regions stay; rows outside them (a footer's company table)
    # are the layout's and are not reported.
    assert {(u.kind, u.label) for u in kept.unread} <= {(u.kind, u.label) for u in whole.unread}


def _hosts(report: TrialReport, *roles: str) -> set[tuple[str, int]]:
    return {(h.host, h.count) for h in report.hosts if h.role in roles}


def test_a_capture_keeps_no_member_text_and_no_url_path_outside_the_product_regions() -> None:
    """Member text in a generic element, a link's secret-bearing path, a form's action path and a
    data attribute never reach the capture; the product's own text and image source do."""
    page = (
        "<html><head><title>상품 103</title>"
        '<meta property="og:title" content="상품 103">'
        '<meta property="product:productId" content="103">'
        '<meta name="csrf-token" content="CSRFMETA77">'
        '<meta name="naver-site-verification" content="VERIFYMETA88">'
        '<meta charset="utf-8">'
        '<link rel="stylesheet" href="https://assets.shop-cdn.com/s/abc/site.css">'
        '<script src="https://assets.shop-cdn.com/k/9f8e7d6c5b4a/front.js"></script></head><body>'
        f'<div class="top"><span>{MEMBER_NAME}님</span> <em>{MEMBER_EMAIL}</em></div>'
        '<a href="https://shop.example.com/myorder/9F2C4A1B7E3D5C6A8B9D0E1F2A3B4C5D/">주문</a>'
        '<a href="https://auth.shop-login.com/member/login.php?r=%2Fsecret">로그인</a>'
        '<form action="https://pay.example.com/charge/7D3C2B1A9E8F/" data-user="u-77">'
        '<input name="t" value="v-secret"></form>'
        '<div class="xans-product-detail" style="display: none !important;'
        ' background: url(https://track.example.com/s.png?token=STYLESECRET)">'
        '<p style="color:red">판매가 12,000원</p>'
        '<img src="https://img.shop-cdn.com/p/103.jpg" data-owner="u-77">'
        '<img src="https://img.shop-cdn.com/p/104.jpg?v=a1b2c3d4&amp;goodsNo=104#frag"'
        ' srcset="https://img.shop-cdn.com/p/105.jpg?exp=1700000000 2x,'
        ' data:image/gif;base64,R0 1x">'
        '<img src="/web/p/106.jpg;jsessionid=PATHSECRET?w=500"></div>'
        "</body></html>"
    )
    kept = sanitize(
        page,
        regions=("xans-product-detail",),
        base_url="https://shop.example.com/product/a/103/",
        safe_query_keys={"img.shop-cdn.com": frozenset({"goodsNo"})},
        secrets=[SESSION_VALUE],
    )
    for gone in (
        MEMBER_NAME,
        MEMBER_EMAIL,
        "9F2C4A1B7E3D5C6A8B9D0E1F2A3B4C5D",
        "myorder",
        "7D3C2B1A9E8F",
        "charge",
        "secret",
        "u-77",
        "9f8e7d6c5b4a",
        "site.css",
        "주문",
        "로그인",
        "a1b2c3d4",
        "frag",
        "1700000000",
        "base64",
        "PATHSECRET",
        "w=500",
        "STYLESECRET",
        "track.example.com",
        "color",
        "CSRFMETA77",
        "csrf-token",
        "VERIFYMETA88",
    ):
        assert gone not in kept, gone
    # ADR-0010: a locator keeps scheme, host, path and only the profile's safe query keys.
    assert 'src="https://img.shop-cdn.com/p/104.jpg?goodsNo=104"' in kept
    assert 'srcset="https://img.shop-cdn.com/p/105.jpg 2x"' in kept
    assert 'src="https://shop.example.com/web/p/106.jpg"' in kept
    # What the template and the host report read stays.
    assert "판매가 12,000원" in kept and 'src="https://img.shop-cdn.com/p/103.jpg"' in kept
    assert '<meta property="og:title" content="상품 103">' in kept and "<title>상품 103" in kept
    # Only public Open Graph and product metadata keep a value; any other meta keeps nothing.
    assert '<meta property="product:productId" content="103">' in kept
    assert '<meta charset="utf-8">' in kept
    assert 'href="//auth.shop-login.com/login"' in kept
    assert 'src="//assets.shop-cdn.com/"' in kept and 'href="//assets.shop-cdn.com/"' in kept
    assert 'class="top"' in kept  # the structure stays, its text does not
    # A style keeps only what can hide an element, squashed as the templates read it.
    assert 'style="display:none!important"' in kept
