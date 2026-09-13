"""The policy-enforcing supplier gateway (ADR-0007).

Core CONNECT receives only this object; supplier definitions receive nothing but the immutable
``ProbeResponse``. Every request here:

* is paced per supplier (concurrency cap + minimum spacing);
* runs inside an egress grant limited to the supplier profile's hosts (HTTP), or a browser whose
  every request is routed through the same host allowlist (browser login);
* is attributed with one ``supplier.request`` log line built from the safe-payload allowlist.

The browser runs headless in an off-the-record context: no persistent profile, no tracing, no HAR,
no video, no screenshots. Only cookies for the supplier's own hosts leave it, as the session
payload that core CONNECT encrypts at rest.
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.egress import EGRESS, EgressBlockedError
from app.core.errors import (
    AppError,
    AuthError,
    PolicyBlockedError,
    RateLimitedError,
    TransientError,
)
from app.core.safe_payload import safe_payload
from integrations.suppliers.base import (
    Credentials,
    ProbeResponse,
    RequestKind,
    SupplierDefinition,
    SupplierProfile,
    SupplierTransport,
)
from integrations.suppliers.transport.pacing import RequestPacer
from integrations.suppliers.transport.session_payload import decode_session, encode_session

logger = logging.getLogger("icbm.connect.transport")

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_LOGIN_POLL_S = 0.25


def _desktop_user_agent(major: str) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36 Edg/{major}.0.0.0"
    )


DEFAULT_USER_AGENT = _desktop_user_agent("140")


def _path(url: str | None) -> str | None:
    return urlsplit(url).path or "/" if url else None


class PolicedSupplierGateway:
    def __init__(
        self,
        *,
        browser_channel: str,
        pacer: RequestPacer | None = None,
        # Tests substitute an httpx.MockTransport; production always uses the network stack.
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._browser_channel = browser_channel
        self._pacer = pacer or RequestPacer()
        self._http_transport = http_transport

    def _observe(
        self,
        profile: SupplierProfile,
        kind: RequestKind,
        transport: SupplierTransport,
        started: datetime,
        started_mono: float,
        result_class: str,
        **extra: object,
    ) -> None:
        logger.info(
            "supplier.request",
            extra=safe_payload(
                supplier_key=profile.supplier_key,
                request_kind=kind,
                transport=transport,
                started_at=started,
                finished_at=datetime.now(UTC),
                latency_ms=round((time.monotonic() - started_mono) * 1000, 1),
                retry_count=0,
                result_class=result_class,
                **extra,
            ),
        )

    # ---------------------------------------------------------------- HTTP protected reads

    def fetch(
        self, definition: SupplierDefinition, *, kind: RequestKind, session: bytes | None
    ) -> ProbeResponse:
        profile = definition.profile
        target = definition.probe.target
        cookies: list[dict[str, str]] = []
        user_agent = DEFAULT_USER_AGENT
        if session is not None and kind is RequestKind.PROTECTED_READ:
            try:
                cookies, user_agent = decode_session(session)
            except ValueError:
                cookies = []  # an unusable session reads as unauthenticated; the proof decides
        jar = httpx.Cookies()
        for cookie in cookies:
            jar.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""))
        result = "ERROR"
        status: int | None = None
        started, started_mono = datetime.now(UTC), time.monotonic()
        try:
            with (
                self._pacer.slot(profile),
                EGRESS.grant(f"supplier:{profile.supplier_key}", profile.egress_hosts),
                httpx.Client(
                    timeout=profile.request_policy.request_timeout_s,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._http_transport,
                    cookies=jar,
                    headers={"User-Agent": user_agent, "Accept-Language": "ko-KR,ko;q=0.9"},
                ) as client,
            ):
                started, started_mono = datetime.now(UTC), time.monotonic()
                response = client.get(profile.base_url + target)
                status = response.status_code
                body = response.text
                location = response.headers.get("location")
        except EgressBlockedError as exc:
            result = "EGRESS_BLOCKED"
            raise PolicyBlockedError("SUPPLIER_EGRESS_BLOCKED", str(exc)) from exc
        except httpx.TimeoutException as exc:
            result = "TIMEOUT"
            raise TransientError("SUPPLIER_TIMEOUT", "supplier did not answer in time") from exc
        except httpx.TransportError as exc:
            result = "NETWORK_ERROR"
            raise TransientError("SUPPLIER_NETWORK_ERROR", type(exc).__name__) from exc
        else:
            result = f"HTTP_{status}"
        finally:
            self._observe(
                profile,
                kind,
                SupplierTransport.HTTP,
                started,
                started_mono,
                result,
                http_status=status,
                target=target,
            )
        if status == 429:
            raise RateLimitedError("SUPPLIER_RATE_LIMITED", "supplier throttled the request")
        if status >= 500:
            raise TransientError("SUPPLIER_SERVER_ERROR", f"supplier answered HTTP {status}")
        return ProbeResponse(
            status=status,
            path=target,
            location=_path(location) if status in _REDIRECTS else None,
            body=body,
        )

    # ---------------------------------------------------------------- browser login

    def login(self, definition: SupplierDefinition, credentials: Credentials) -> bytes:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright

        profile = definition.profile
        spec = definition.login
        hosts = profile.egress_hosts
        timeout_ms = profile.request_policy.request_timeout_s * 1000
        blocked = 0
        dialogs = 0
        result = "ERROR"
        started, started_mono = datetime.now(UTC), time.monotonic()

        def route(route: Any) -> None:
            nonlocal blocked
            host = (urlsplit(route.request.url).hostname or "").lower()
            if host in hosts:
                route.continue_()
            else:
                blocked += 1
                route.abort()

        def on_dialog(dialog: Any) -> None:
            nonlocal dialogs
            dialogs += 1
            dialog.dismiss()

        try:
            with self._pacer.slot(profile), sync_playwright() as playwright:
                started, started_mono = datetime.now(UTC), time.monotonic()
                browser = playwright.chromium.launch(channel=self._browser_channel, headless=True)
                try:
                    user_agent = _desktop_user_agent(browser.version.split(".", 1)[0])
                    context = browser.new_context(
                        user_agent=user_agent,
                        locale="ko-KR",
                        service_workers="block",
                        accept_downloads=False,
                    )
                    context.route("**/*", route)
                    page = context.new_page()
                    page.on("dialog", on_dialog)
                    page.goto(profile.base_url + spec.path, wait_until="load", timeout=timeout_ms)
                    page.fill(spec.username_selector, credentials.username, timeout=timeout_ms)
                    page.fill(spec.password_selector, credentials.password, timeout=timeout_ms)
                    page.click(spec.submit_selector, timeout=timeout_ms)
                    deadline = time.monotonic() + profile.request_policy.request_timeout_s
                    while time.monotonic() < deadline:
                        if urlsplit(page.url).path != spec.path or dialogs:
                            break
                        if any(page.query_selector(s) for s in spec.rejection_selectors):
                            break
                        page.wait_for_timeout(_LOGIN_POLL_S * 1000)
                    if urlsplit(page.url).path == spec.path:
                        if dialogs or any(page.query_selector(s) for s in spec.rejection_selectors):
                            result = "LOGIN_REJECTED"
                            raise AuthError(
                                "SUPPLIER_LOGIN_REJECTED", "the supplier rejected the stored login"
                            )
                        result = "LOGIN_TIMEOUT"
                        raise TransientError(
                            "SUPPLIER_LOGIN_TIMEOUT", "the login did not complete in time"
                        )
                    page.wait_for_load_state("load", timeout=timeout_ms)
                    payload = encode_session(
                        context.cookies([profile.base_url]), user_agent=user_agent, hosts=hosts
                    )
                    result = "LOGIN_SUBMITTED"
                    return payload
                finally:
                    browser.close()
        except AppError:
            raise
        except PlaywrightTimeout as exc:
            result = "BROWSER_TIMEOUT"
            raise TransientError("SUPPLIER_BROWSER_TIMEOUT", "supplier page timed out") from exc
        except PlaywrightError as exc:
            result = "BROWSER_ERROR"
            raise TransientError("SUPPLIER_BROWSER_ERROR", "browser automation failed") from exc
        finally:
            self._observe(
                profile,
                RequestKind.AUTHENTICATE,
                SupplierTransport.BROWSER,
                started,
                started_mono,
                result,
                browser_blocked_requests=blocked,
                target=spec.path,
            )
