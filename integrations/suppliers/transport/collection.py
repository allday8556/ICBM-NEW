"""The policy-enforcing COLLECT gateway (ADR-0010 §3, §4, §9).

This is the only code that performs a supplier collection request. For every request:

* the target is checked against the collection profile before anything else:
  - https only, with no credentials, no port other than 443 and no fragment;
  - product and policy documents on the storefront host, in the product path form or at an exact
    policy path;
  - images only on the explicit image hosts;
  - a product URL may carry only the explicitly safe query keys;
* the request is reserved in the request budget before any byte is sent, so a refusal sends
  nothing;
* it is paced by the supplier's ``RequestPolicy`` and runs inside an egress grant limited to the
  profile's hosts. A redirect is never followed; it is returned as evidence;
* the session payload of the M1 connection owner is sent only with a product read;
* it is attributed with one ``supplier.collect_request`` log line of allowlisted fields. The
  target is the path of a document and only the host of an image, never a query.

A discovered policy read is a separate, bounded concept, not a widening of the profile's fixed
``policy_paths``. It is one public document linked from a product page, read from the
storefront host only, with no session, no query, never a product path, and never followed
further. The budget reserves it under a ``discovered:`` subject, which the reconnaissance ledger
allows at most once.

While a COLLECT exchange runs, the HTTP stack's own log records are dropped: httpx's request line
carries the full URL, and httpcore's debug lines can carry headers and cookies. The guard is a
filter bound to the COLLECT exchange through a context variable. It changes no logger level and
leaves every other owner's records untouched. Bodies are read up to a bound and refused beyond
it, keeping nothing partial. The live network transport is never built under CI or pytest.
"""

import logging
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import SplitResult, unquote_plus, urlsplit

import httpx

from app.collect.facts import FetchTargetRefusal
from app.core.egress import EGRESS, EgressBlockedError
from app.core.errors import (
    AppError,
    ErrorClass,
    PolicyBlockedError,
    RateLimitedError,
    TransientError,
)
from app.core.safe_payload import safe_payload
from integrations.suppliers.base import SupplierTransport
from integrations.suppliers.collection import (
    DISCOVERED_POLICY_PREFIX,
    IMAGE_ROBOTS_PATH,
    IMAGE_ROBOTS_PREFIX,
    CollectionProfile,
    DocumentView,
    FetchIssue,
    ImageResponse,
    ReadKind,
)
from integrations.suppliers.transport.gateway import DEFAULT_USER_AGENT
from integrations.suppliers.transport.pacing import RequestPacer
from integrations.suppliers.transport.session_payload import decode_session

logger = logging.getLogger("icbm.collect.transport")

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
# Environment markers of CI and test runs: the live transport never exists under them.
CI_MARKERS = ("CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# The HTTP stack's loggers, exactly as httpx and httpcore name them.
HTTP_STACK_LOGGERS = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_IN_COLLECT_EXCHANGE: ContextVar[bool] = ContextVar("icbm_collect_exchange", default=False)

Observe = Callable[[int], None]


class _CollectExchangeGuard(logging.Filter):
    """Drops the HTTP stack's own records only while a COLLECT exchange runs in this context."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _IN_COLLECT_EXCHANGE.get()


_GUARD = _CollectExchangeGuard()


def install_log_guards() -> None:
    """Attach the COLLECT exchange guard to the HTTP stack's loggers (idempotent; no level)."""
    for name in HTTP_STACK_LOGGERS:
        logging.getLogger(name).addFilter(_GUARD)


class CollectionTargetRefused(PolicyBlockedError):
    """The URL is outside the collection profile; nothing was reserved or sent.

    ``reason`` says which rule refused it, from a closed vocabulary with no catch-all. The message
    is for people and never echoes the URL.
    """

    def __init__(self, reason: FetchTargetRefusal, message: str) -> None:
        super().__init__("COLLECT_TARGET_REFUSED", message)
        self.reason = reason


class CollectionBudgetRefused(PolicyBlockedError):
    """The request budget refused the request before any byte was sent."""


class ImageFetchRefused(AppError):
    """An image response is unusable; the reference becomes REVIEW_REQUIRED with ``issue``."""

    error_class = ErrorClass.REVIEW_REQUIRED

    def __init__(self, issue: FetchIssue, message: str) -> None:
        super().__init__(f"COLLECT_IMAGE_{issue.value}", message)
        self.issue = issue


class LiveTransportRefused(RuntimeError):
    """The live supplier transport was requested where it may never exist."""


class RequestBudget(Protocol):
    def reserve(self, kind: ReadKind, subject: str) -> None:
        """Durably reserve one request before it is sent, or raise ``CollectionBudgetRefused``.

        ``subject`` is the canonical product URL, the policy path, ``discovered:<path>`` for a
        discovered policy read (allowed at most once), or the image host — never a signed URL."""
        ...


def ci_or_test(environ: Mapping[str, str] | None = None) -> str | None:
    """Why this process is a CI or test run, or None."""
    env = os.environ if environ is None else environ
    for name in CI_MARKERS:
        if env.get(name):
            return name
    return "pytest" if "pytest" in sys.modules else None


def _refused(reason: FetchTargetRefusal, message: str) -> CollectionTargetRefused:
    return CollectionTargetRefused(reason, message)


def _https(url: str) -> SplitResult:
    """The parts of an https URL without credentials, another port, a fragment or whitespace.

    A URL with more than one defect is refused for the first of them, in this fixed order, so the
    same URL always gets the same reason: whitespace, a fragment, an unparseable URL or port, no
    scheme, ``http``, another scheme, no host, credentials, another port. Nothing is repaired: a
    URL is accepted as written or refused.
    """
    if any(char.isspace() for char in url):
        raise _refused(FetchTargetRefusal.WHITESPACE, "a collection URL has no whitespace")
    if "#" in url:
        raise _refused(FetchTargetRefusal.FRAGMENT, "a collection URL has no fragment")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _refused(
            FetchTargetRefusal.UNPARSEABLE, "the collection URL is not parseable"
        ) from None
    if not parts.scheme:
        raise _refused(FetchTargetRefusal.NOT_ABSOLUTE, "a collection URL names its own scheme")
    if parts.scheme == "http":
        raise _refused(FetchTargetRefusal.NON_HTTPS, "a collection URL is https, not http")
    if parts.scheme != "https":
        raise _refused(FetchTargetRefusal.UNSUPPORTED_SCHEME, "a collection URL is https")
    if not parts.hostname:
        raise _refused(FetchTargetRefusal.UNPARSEABLE, "the collection URL names no host")
    if parts.username is not None or parts.password is not None:
        raise _refused(
            FetchTargetRefusal.CREDENTIALS_PRESENT, "a collection URL carries no credentials"
        )
    if port not in (None, 443):
        raise _refused(FetchTargetRefusal.NON_STANDARD_PORT, "a collection URL uses port 443")
    return parts


@dataclass(frozen=True)
class ImageFetchTarget:
    """What the target check made of one image URL it allows.

    ``host`` is the budget subject. ``locator`` is the canonical form of the target — https, the
    host as parsed, the path as written — and ``None`` when the target carries a query, which may
    be a token and is never persisted. The request itself is still sent to the URL exactly as it
    was written: the canonical form is for reading back, never a rewrite.
    """

    host: str
    locator: str | None


def image_fetch_target(profile: CollectionProfile, url: str) -> ImageFetchTarget:
    """The one judge of whether an image URL may be fetched, or its structured refusal.

    :func:`check_target` answers image requests through this function, and generic COLLECT core
    asks it the same question to record what a reference resolved to. No other code decides it.
    """
    parts = _https(url)
    host = parts.hostname or ""
    if host not in profile.image_hosts:
        raise _refused(FetchTargetRefusal.HOST_NOT_ALLOWLISTED, "the image host is not allowlisted")
    return ImageFetchTarget(
        host=host, locator=None if parts.query else f"https://{host}{parts.path}"
    )


def check_target(profile: CollectionProfile, url: str, kind: ReadKind) -> str:
    """Refuse a URL outside the profile; return the budget subject of an allowed one."""
    if kind is ReadKind.IMAGE_REQUEST:
        return image_fetch_target(profile, url).host
    parts = _https(url)
    host, path = parts.hostname or "", parts.path or "/"
    if (
        kind is ReadKind.POLICY_READ
        and host != profile.storefront_host
        and (host in profile.image_hosts)
    ):
        # Exactly that one document on that host, with no query and no other path. This is the
        # host's own policy, not an opening to read anything else from it.
        if parts.query:
            raise _refused(
                FetchTargetRefusal.QUERY_NOT_ALLOWED,
                "an image host answers for its robots document only",
            )
        if path != IMAGE_ROBOTS_PATH:
            raise _refused(
                FetchTargetRefusal.PATH_NOT_ALLOWED,
                "an image host answers for its robots document only",
            )
        return f"{IMAGE_ROBOTS_PREFIX}{host}"
    if host != profile.storefront_host:
        raise _refused(
            FetchTargetRefusal.HOST_NOT_ALLOWLISTED,
            "documents are read only from the storefront host",
        )
    if kind is ReadKind.POLICY_READ:
        if parts.query:
            raise _refused(FetchTargetRefusal.QUERY_NOT_ALLOWED, "a policy document has no query")
        if path not in profile.policy_paths:
            raise _refused(
                FetchTargetRefusal.PATH_NOT_ALLOWED, "not an allowlisted policy document"
            )
        return path
    if not profile.is_product_path(path):
        raise _refused(FetchTargetRefusal.PATH_NOT_ALLOWED, "the path is not the product path form")
    safe = profile.safe_query_keys.get(host, frozenset())
    keys = [pair.split("=", 1)[0] for pair in parts.query.split("&") if pair]
    if any(unquote_plus(key) not in safe for key in keys):
        raise _refused(
            FetchTargetRefusal.QUERY_NOT_ALLOWED,
            "the product URL carries a query key that is not explicitly safe",
        )
    return f"https://{host}{path}" + (f"?{parts.query}" if parts.query else "")


def check_discovered_policy(profile: CollectionProfile, url: str) -> str:
    """Refuse a discovered policy read that is not a plain public storefront document; return
    its path."""
    parts = _https(url)
    path = parts.path or "/"
    if parts.hostname != profile.storefront_host:
        raise _refused(
            FetchTargetRefusal.HOST_NOT_ALLOWLISTED,
            "a discovered policy document is read only from the storefront host",
        )
    if parts.query:
        raise _refused(
            FetchTargetRefusal.QUERY_NOT_ALLOWED, "a discovered policy read carries no query"
        )
    if path in profile.policy_paths:
        raise _refused(
            FetchTargetRefusal.PATH_NOT_ALLOWED,
            "a fixed policy document is read as a fixed policy read",
        )
    if profile.is_product_path(path):
        raise _refused(FetchTargetRefusal.PATH_NOT_ALLOWED, "a product page is never a policy read")
    return path


class PolicedCollectionGateway:
    def __init__(
        self,
        *,
        pacer: RequestPacer | None = None,
        # Tests pass an httpx.MockTransport; without one the gateway uses the network stack,
        # which it refuses to do under CI or pytest.
        http_transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if http_transport is None and (blocker := ci_or_test(environ)) is not None:
            raise LiveTransportRefused(
                f"the live collection transport never exists under {blocker}"
            )
        install_log_guards()
        self._pacer = pacer or RequestPacer()
        self._http_transport = http_transport

    # ---------------------------------------------------------------- documents

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView:
        if kind is ReadKind.IMAGE_REQUEST:
            raise ValueError("images are read with read_image")
        subject = check_target(profile, url, kind)
        budget.reserve(kind, subject)
        cookies: list[dict[str, str]] = []
        user_agent = DEFAULT_USER_AGENT
        if kind is ReadKind.PRODUCT_READ and session is not None:
            cookies, user_agent = decode_session(session)
        return self._document(profile, url, kind, cookies, user_agent)

    def read_discovered_policy(
        self, profile: CollectionProfile, url: str, *, budget: RequestBudget
    ) -> DocumentView:
        """One public document linked from a product page: same storefront, no session (there is
        no session parameter), no query, and at most once per budget."""
        path = check_discovered_policy(profile, url)
        budget.reserve(ReadKind.POLICY_READ, f"{DISCOVERED_POLICY_PREFIX}{path}")
        return self._document(profile, url, ReadKind.POLICY_READ, [], DEFAULT_USER_AGENT)

    def _document(
        self,
        profile: CollectionProfile,
        url: str,
        kind: ReadKind,
        cookies: list[dict[str, str]],
        user_agent: str,
    ) -> DocumentView:
        path = urlsplit(url).path or "/"
        with (
            self._exchange(profile, kind, target=path) as observe,
            self._client(profile, cookies, user_agent) as client,
            client.stream("GET", url) as response,
        ):
            observe(response.status_code)
            self._raise_for_status(response.status_code)
            data = _bounded(response, MAX_DOCUMENT_BYTES)
            if data is None:
                raise PolicyBlockedError(
                    "COLLECT_DOCUMENT_TOO_LARGE", "the document exceeds the size bound"
                )
            location = response.headers.get("location")
            return DocumentView(
                kind=kind,
                status=response.status_code,
                path=path,
                location=(
                    urlsplit(location).path or "/"
                    if location and response.status_code in _REDIRECTS
                    else None
                ),
                content_type=response.headers.get("content-type", ""),
                body=data.decode(response.encoding or "utf-8", errors="replace"),
            )

    # ---------------------------------------------------------------- images

    def read_image(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        budget: RequestBudget,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
    ) -> ImageResponse:
        """Fetch one image, revalidating with the stored validators when there are any.

        ``max_bytes`` narrows the profile's own per-image bound to what the caller still has left
        to spend. The bound is applied to the declared size and to the body as it arrives, so a
        response never reaches the caller — and never reaches storage — above it.
        """
        host = check_target(profile, url, ReadKind.IMAGE_REQUEST)
        budget.reserve(ReadKind.IMAGE_REQUEST, host)
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        with (
            self._exchange(profile, ReadKind.IMAGE_REQUEST, target=host) as observe,
            self._client(profile, [], DEFAULT_USER_AGENT) as client,
            client.stream("GET", url, headers=headers) as response,
        ):
            status = response.status_code
            observe(status)
            validators = (response.headers.get("etag"), response.headers.get("last-modified"))
            if status == 304:
                return ImageResponse(304, None, validators[0] or etag, validators[1])
            self._raise_for_status(status)
            if status != 200:
                raise ImageFetchRefused(FetchIssue.FETCH_FAILED, f"HTTP {status}")
            content_type = response.headers.get("content-type", "")
            media = content_type.split(";", 1)[0].strip().lower()
            if not media.startswith("image/"):
                raise ImageFetchRefused(FetchIssue.BAD_CONTENT_TYPE, "the response is not an image")
            limit = min(profile.limits.max_image_bytes, max_bytes or profile.limits.max_image_bytes)
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > limit:
                raise ImageFetchRefused(FetchIssue.OVERSIZE, "declared size over the bound")
            data = _bounded(response, limit)
            if data is None:
                raise ImageFetchRefused(FetchIssue.OVERSIZE, "body over the size bound")
            return ImageResponse(200, media, validators[0], validators[1], bytes(data))

    # ---------------------------------------------------------------- common

    @contextmanager
    def _client(
        self, profile: CollectionProfile, cookies: list[dict[str, str]], user_agent: str
    ) -> Iterator[httpx.Client]:
        jar = httpx.Cookies()
        for cookie in cookies:
            jar.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""))
        supplier = profile.supplier
        with (
            self._pacer.slot(supplier),
            EGRESS.grant(f"supplier:{supplier.supplier_key}:collect", profile.hosts),
            httpx.Client(
                timeout=supplier.request_policy.request_timeout_s,
                follow_redirects=False,
                trust_env=False,
                transport=self._http_transport,
                cookies=jar,
                headers={"User-Agent": user_agent, "Accept-Language": "ko-KR,ko;q=0.9"},
            ) as client,
        ):
            yield client

    @contextmanager
    def _exchange(
        self, profile: CollectionProfile, kind: ReadKind, *, target: str
    ) -> Iterator[Observe]:
        """Bind the log guard, translate transport failures and write the one attribution
        line per request."""
        result, status = "ERROR", None
        started, started_mono = datetime.now(UTC), time.monotonic()

        def observe(code: int) -> None:
            nonlocal status, result
            status, result = code, f"HTTP_{code}"

        token = _IN_COLLECT_EXCHANGE.set(True)
        try:
            yield observe
        except EgressBlockedError as exc:
            result = "EGRESS_BLOCKED"
            raise PolicyBlockedError("COLLECT_EGRESS_BLOCKED", str(exc)) from exc
        except httpx.TimeoutException as exc:
            result = "TIMEOUT"
            raise TransientError("COLLECT_TIMEOUT", "the supplier did not answer in time") from exc
        except httpx.TransportError as exc:
            result = "NETWORK_ERROR"
            raise TransientError("COLLECT_NETWORK_ERROR", type(exc).__name__) from exc
        finally:
            _IN_COLLECT_EXCHANGE.reset(token)
            logger.info(
                "supplier.collect_request",
                extra=safe_payload(
                    supplier_key=profile.supplier.supplier_key,
                    request_kind=kind,
                    transport=SupplierTransport.HTTP,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    latency_ms=round((time.monotonic() - started_mono) * 1000, 1),
                    retry_count=0,
                    result_class=result,
                    http_status=status,
                    target=target[:200],
                ),
            )

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if status == 429:
            raise RateLimitedError("COLLECT_RATE_LIMITED", "the supplier throttled the request")
        if status >= 500:
            raise TransientError("COLLECT_SERVER_ERROR", f"the supplier answered HTTP {status}")


def _bounded(response: httpx.Response, limit: int) -> bytearray | None:
    """The body, or None as soon as it exceeds ``limit`` (nothing partial is kept)."""
    data = bytearray()
    for chunk in response.iter_bytes():
        data.extend(chunk)
        if len(data) > limit:
            return None
    return data


class DeferredCollectionGateway:
    """The policed gateway, built on the first request and not before.

    Composing the application must not open a transport: a process that never collects anything
    never builds one, and under CI or pytest building one is refused outright. Deferring the
    construction keeps that refusal where it belongs — at the moment a real request would be
    made — instead of making the whole application impossible to compose in a test.
    """

    def __init__(self, build: Callable[[], PolicedCollectionGateway] | None = None) -> None:
        self._build = build or PolicedCollectionGateway
        self._gateway: PolicedCollectionGateway | None = None

    def _ready(self) -> PolicedCollectionGateway:
        if self._gateway is None:
            self._gateway = self._build()
        return self._gateway

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView:
        return self._ready().read_document(profile, url, kind=kind, budget=budget, session=session)

    def read_image(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        budget: RequestBudget,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
    ) -> ImageResponse:
        return self._ready().read_image(
            profile,
            url,
            budget=budget,
            etag=etag,
            last_modified=last_modified,
            max_bytes=max_bytes,
        )
