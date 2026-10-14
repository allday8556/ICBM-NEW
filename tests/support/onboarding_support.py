"""A scripted storefront for 「+ 공급처 추가」 (ADR-0036 O2): no network, exact request counts.

The real bootstrap reader (``PolicedPublicReader``) runs over an ``httpx.MockTransport`` that
answers from a dict of pages, with a resolver that answers a fixed address and a pacer that never
sleeps. The egress guard's grants are recorded, so a test proves that only the entered host was
ever granted and every read stayed on it. The pages carry only the markers the templates look
for: synthetic text, no real page content.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.platform.core.egress import EGRESS, _active_grant
from integrations.suppliers.transport import gateway
from integrations.suppliers.transport.gateway import PolicedPublicReader
from integrations.suppliers.transport.pacing import RequestPacer

PUBLIC_ADDRESS = "93.184.216.34"

CAFE24_HOME = (
    '<html><head><link rel="stylesheet" href="//img.echosting.cafe24.com/skin/base_ko_KR/x.css">'
    '</head><body><div class="xans-element- xans-layout xans-layout-statelogoff"></div></body>'
    "</html>"
)
CAFE24_LOGIN = (
    '<form id="member_form" action="/exec/front/Member/login/" method="post">'
    '<div class="xans-element- xans-member xans-member-login"></div></form>'
)
GODOMALL_HOME = (
    '<html><head><link href="https://godomall-storage.cdn-nhncommerce.com/a/data/skin/front/s/'
    'style.css"></head><body></body></html>'
)
GODOMALL_LOGIN = '<form id="formLogin" name="formLogin" action="/member/login_ps.php"></form>'
PLAIN_HOME = "<html><body><h1>Just a shop</h1></body></html>"
ROBOTS_OPEN = "User-agent: *\nDisallow: /admin/\n"
ROBOTS_PRODUCTS = "User-agent: *\nDisallow: /product/\nCrawl-delay: 10\n"
ROBOTS_GODOMALL_PRODUCTS = "User-agent: *\nDisallow: /goods/\n"
ROBOTS_ALL = "User-agent: *\nDisallow: /\n"
TERMS = "<html><body>이용약관</body></html>"

Page = tuple[int, str, dict[str, str]]


def cafe24_site(robots: str | None = ROBOTS_OPEN) -> dict[str, Page]:
    pages: dict[str, Page] = {
        "/": (200, CAFE24_HOME, {}),
        "/member/login.html": (200, CAFE24_LOGIN, {}),
        "/member/agreement.html": (200, TERMS, {}),
    }
    if robots is not None:
        pages["/robots.txt"] = (200, robots, {"content-type": "text/plain"})
    return pages


def godomall_site(robots: str | None = ROBOTS_OPEN) -> dict[str, Page]:
    pages: dict[str, Page] = {
        "/": (200, GODOMALL_HOME, {}),
        "/member/login.php": (200, GODOMALL_LOGIN, {}),
        "/service/agreement.php": (200, TERMS, {}),
    }
    if robots is not None:
        pages["/robots.txt"] = (200, robots, {"content-type": "text/plain"})
    return pages


def ambiguous_site() -> dict[str, Page]:
    """A storefront carrying both templates' markers on its home page and both login forms."""
    pages = cafe24_site()
    pages["/"] = (200, CAFE24_HOME + GODOMALL_HOME, {})
    pages["/member/login.php"] = (200, GODOMALL_LOGIN, {})
    pages["/service/agreement.php"] = (200, TERMS, {})
    return pages


def plain_site() -> dict[str, Page]:
    return {"/": (200, PLAIN_HOME, {}), "/robots.txt": (200, ROBOTS_OPEN, {})}


@dataclass
class Storefront:
    """Every request any scripted host received, and the pages each host serves."""

    sites: dict[str, dict[str, Page]] = field(default_factory=dict)
    addresses: dict[str, tuple[str, ...]] = field(default_factory=dict)
    requests: list[tuple[str, str, str]] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    grant_hosts_seen: list[frozenset[str]] = field(default_factory=list)
    # (address connected to, Host header, TLS server name) of every request.
    connections: list[tuple[str, str, str | None]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        # The reader connects to the vetted address; the site is the one the Host header names.
        host = request.headers["host"]
        self.connections.append((request.url.host, host, request.extensions.get("sni_hostname")))
        self.requests.append((request.method, host, request.url.path))
        grant = _active_grant.get()
        self.grant_hosts_seen.append(frozenset() if grant is None else grant.hosts)
        page = self.sites.get(host, {}).get(request.url.path)
        if page is None:
            return httpx.Response(404, text="not found")
        status, body, headers = page
        return httpx.Response(status, text=body, headers=headers)

    def resolve(self, host: str) -> tuple[str, ...]:
        self.resolved.append(host)
        grant = _active_grant.get()
        # The name is resolved inside the entered host's own grant, and only there.
        assert grant is not None and grant.hosts == frozenset({host}), grant
        return self.addresses.get(host, (PUBLIC_ADDRESS,))

    def reader(self) -> PolicedPublicReader:
        return PolicedPublicReader(
            pacer=RequestPacer(sleep=lambda _: None),
            http_transport=httpx.MockTransport(self.handler),
            resolve=self.resolve,
        )

    def paths(self, host: str) -> list[str]:
        return [path for _, h, path in self.requests if h == host]


class RecordingEgress:
    """The real egress guard, with every grant it opens recorded."""

    def __init__(self) -> None:
        self.grants: list[tuple[str, frozenset[str]]] = []

    @contextmanager
    def grant(self, owner: str, hosts: Any) -> Iterator[None]:
        granted = frozenset(hosts)
        self.grants.append((owner, granted))
        with EGRESS.grant(owner, granted):
            yield


@contextmanager
def recorded_grants(monkeypatch: Any) -> Iterator[RecordingEgress]:
    recording = RecordingEgress()
    monkeypatch.setattr(gateway, "EGRESS", recording)
    yield recording


def wait_until(predicate: Callable[[], bool], *, seconds: float = 15.0) -> None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")
