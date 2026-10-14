"""A scripted storefront for 「사이트 분석」 (ADR-0036 O3): no network, every request recorded.

The real policed collection gateway runs over an ``httpx.MockTransport`` that answers product pages
from a dict, with a pacer that never sleeps; the CONNECT session is a fixed payload in the shape the
gateway decodes. Every request the transport receives is recorded, so a test proves exactly one
product read per trial and no image request at all. The pages are the repository's reconnaissance
fixtures moved onto the test host, or synthetic pages written here; no supplier is contacted.
"""

import html as html_text
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config import AppConfig
from app.container import Container, build_container
from app.platform.core.ownership import acquire_data_dir
from app.platform.core.secrets import MemorySecretStore
from app.stages.collect.onboarding.site import OnboardingState
from integrations.suppliers.transport.collection import PolicedCollectionGateway
from integrations.suppliers.transport.pacing import RequestPacer
from integrations.suppliers.transport.session_payload import encode_session
from tests.support.jobs_support import TEST_JOBS, FakeClock

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "suppliers"
OPERATOR = "operator:local"
CAFE24_HOST = "shop.trial-sample.co.kr"
CAFE24_ORIGIN = f"https://{CAFE24_HOST}"
GODOMALL_HOST = "www.godo-trial.com"
GODOMALL_ORIGIN = f"https://{GODOMALL_HOST}"
IMAGE_HOST = "img.trial-cdn.com"
SESSION_VALUE = "sid-trial-session-0123456789"
MEMBER_NAME = "홍길동회원"
MEMBER_EMAIL = "member.person@example-mail.com"


def cafe24_url(number: int | str, host: str = CAFE24_HOST) -> str:
    return f"https://{host}/product/sample/{number}/"


def godomall_url(number: int | str, host: str = GODOMALL_HOST) -> str:
    return f"https://{host}/goods/goods_view.php?goodsNo={number}"


def row(label: str, value: str) -> str:
    return f'<tr class="xans-record-"><th><span>{label}</span></th><td>{value}</td></tr>'


def cafe24_page(
    number: int | str,
    *,
    host: str = CAFE24_HOST,
    name: str | None = None,
    rows: str | None = None,
    extra_rows: str = "",
    member: bool = True,
    duplicate_module: bool = False,
    body_extra: str = "",
) -> str:
    """A Cafe24 product page every default word reads: its name, one price, no option axis, an
    active purchase control and a representative image on the image host. A signed-in member's
    greeting sits in the layout's log-on module, as on a real member page."""
    title = name or f"트라이얼 상품 {number}"
    stated = rows if rows is not None else row("상품명", title) + row("판매가", "12,000원")
    module = (
        '<div class="xans-element- xans-product xans-product-detail">'
        '<div class="xans-element- xans-product xans-product-image">'
        '<div class="keyImg">'
        f'<img src="https://{IMAGE_HOST}/p/{number}.jpg" class="BigImage"></div>'
        "</div>"
        f'<div class="infoArea"><table><tbody>{stated}{extra_rows}</tbody></table></div>'
        '<div class="xans-element- xans-product xans-product-option"></div>'
        '<div class="xans-element- xans-product xans-product-action">'
        '<a><span id="btnBuy">바로 구매하기</span></a></div>'
        "</div>"
    )
    greeting = (
        '<div class="xans-element- xans-layout xans-layout-statelogon">'
        f"<span>{MEMBER_NAME}님 환영합니다</span><span>{MEMBER_EMAIL}</span>"
        '<a href="/exec/front/Member/logout/">로그아웃</a></div>'
        if member
        else ""
    )
    return (
        "<!DOCTYPE html><html><head>"
        f'<meta property="og:title" content="{html_text.escape(title)}">'
        f'<meta property="og:image" content="https://{IMAGE_HOST}/p/{number}.jpg">'
        f'<meta property="product:productId" content="{number}">'
        f'<meta property="product:retailer_item_id" content="{number}">'
        f'<link rel="canonical" href="https://{host}/product/sample/{number}/">'
        '<script src="https://assets.trial-cdn.com/front.js"></script>'
        "<script>var member_id = 'tester01'; var token='eyJhbGciOiJIUzI1NiJ9.abcdefghijk';</script>"
        "</head><body>"
        f"{greeting}"
        '<form action="https://login.trial-auth.com/member/login" method="post">'
        '<input type="hidden" name="csrf" value="csrf-secret-value-77"></form>'
        f"{module}{module if duplicate_module else ''}{body_extra}"
        "</body></html>"
    )


def script_rendered_page() -> str:
    return (
        "<!DOCTYPE html><html><head><script src='/app.js'></script></head>"
        '<body><div id="app"></div><script>render()</script></body></html>'
    )


def upick_page(number: str = "4954", host: str = CAFE24_HOST) -> str:
    """U-PICK's reconnaissance capture, moved onto the test host."""
    text = (FIXTURES / "upick" / f"{number}.html").read_text("utf-8")
    return text.replace("upickb2b.com", host)


def ggsan_page(number: str = "1000002412", host: str = GODOMALL_HOST) -> str:
    """건강산's reconnaissance capture, moved onto the test host."""
    text = (FIXTURES / "ggsan" / f"{number}.html").read_text("utf-8")
    return text.replace("www.ggsan.com", host)


Page = tuple[int, str]


@dataclass
class TrialShop:
    """The storefronts' product pages by (host, path and query), and every request received."""

    pages: dict[tuple[str, str], Page] = field(default_factory=dict)
    requests: list[tuple[str, str, str]] = field(default_factory=list)
    cookies: list[str] = field(default_factory=list)
    # Called as each request arrives, before it is answered: a test's way to let time pass there.
    on_request: Callable[[], None] | None = None

    def serve(self, url: str, body: str, status: int = 200) -> None:
        parsed = httpx.URL(url)
        target = parsed.path + (f"?{parsed.query.decode()}" if parsed.query else "")
        self.pages[(parsed.host, target)] = (status, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        target = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        self.requests.append((request.method, request.url.host, target))
        self.cookies.append(request.headers.get("cookie", ""))
        if self.on_request is not None:
            self.on_request()
        page = self.pages.get((request.url.host, target))
        if page is None:
            return httpx.Response(404, text="not found")
        status, body = page
        headers = {"location": "/member/login.html"} if status in (301, 302) else {}
        return httpx.Response(
            status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers}
        )

    def gateway(self) -> PolicedCollectionGateway:
        return PolicedCollectionGateway(
            pacer=RequestPacer(sleep=lambda _: None),
            http_transport=httpx.MockTransport(self.handler),
        )

    def hosts(self) -> set[str]:
        return {host for _, host, _ in self.requests}


class StubSessions:
    """CONNECT's session for a collection read, without CONNECT: one payload per supplier."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def collection_session(self, supplier_key: str, *, operator_initiated: bool = False) -> bytes:
        self.asked.append(supplier_key)
        host = CAFE24_HOST if "trial-sample" in supplier_key else GODOMALL_HOST
        return encode_session(
            [{"name": "SID", "value": SESSION_VALUE, "domain": host, "path": "/"}],
            user_agent="TrialTest/1",
            hosts={host},
        )


@contextmanager
def trial_container(
    config: AppConfig, clock: FakeClock, shop: TrialShop, sessions: StubSessions
) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=MemorySecretStore(),
            extra_jobs=TEST_JOBS,
            collection_gateway=shop.gateway(),
            collection_sessions=sessions,
        )
        try:
            yield built
        finally:
            built.db.dispose()


def recon_site(container: Container, origin: str, template: str, name: str = "트라이얼") -> str:
    """An onboarded site moved to ``RECON`` through the store, as O2's flow ends."""
    record = container.onboarding.create(
        name=name, origin=origin, template=template, actor=OPERATOR
    )
    container.onboarding.transition(record.supplier_key, OnboardingState.RECON, actor=OPERATOR)
    return record.supplier_key


def run_trial(container: Container, key: str, url: str) -> str:
    """Submit one sample and run its job; the job's id."""
    submitted = container.onboarding_analysis.submit(key, url, actor=OPERATOR)
    container.runner.run_next()
    job = container.jobs.get(submitted.job_id)
    assert job.state == "SUCCEEDED", (job.last_error_code, job.last_error_message)
    return submitted.job_id


def active_cafe24_site(
    container: Container, clock: FakeClock, shop: TrialShop, name: str = "트라이얼"
) -> str:
    """A Cafe24 site at ``CAFE24_ORIGIN`` taken to ``ACTIVE`` as the operator does: three samples
    read (O3), each confirmed as shown, then 「사용 시작」 (O5a)."""
    key = recon_site(container, CAFE24_ORIGIN, "cafe24", name)
    for number in (901, 902, 903):
        shop.serve(cafe24_url(number), cafe24_page(number))
        run_trial(container, key, cafe24_url(number))
        clock.advance(11)
    for sample in container.onboarding_analysis.view(key).samples:
        container.onboarding_analysis.verify(
            key,
            sample.trial_id,
            "CORRECT",
            shown_fingerprint=sample.facts_fingerprint,
            actor=OPERATOR,
        )
    container.onboarding_analysis.activate(key, actor=OPERATOR)
    assert container.onboarding.current(key).state is OnboardingState.ACTIVE
    return key
