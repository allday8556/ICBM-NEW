"""Pure-ASGI middleware (contextvars propagate reliably, unlike BaseHTTPMiddleware)."""

import logging
import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.correlation import (
    CORRELATION_HEADER,
    accept_or_issue,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from app.core.errors import ErrorClass, error_envelope

logger = logging.getLogger("icbm.http")

CLIENT_HEADER = "X-ICBM-Client"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The UI shell loads nothing from outside this origin; the browser enforces it.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)


class RequestContextMiddleware:
    """Outermost layer: correlation ID, security headers, request log, last-resort 500."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        correlation_id = accept_or_issue(Headers(scope=scope).get(CORRELATION_HEADER))
        token = set_correlation_id(correlation_id)
        path: str = scope["path"]
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers[CORRELATION_HEADER] = correlation_id
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
                if path.startswith("/api/"):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            logger.exception("http.unhandled_exception", extra={"path": path})
            if not response_started:
                response = JSONResponse(
                    error_envelope(
                        ErrorClass.UNKNOWN,
                        "INTERNAL_ERROR",
                        "unexpected server error",
                        correlation_id,
                    ),
                    status_code=500,
                )
                await response(scope, receive, send_with_headers)
        finally:
            level = logging.INFO if path.startswith("/api/") else logging.DEBUG
            logger.log(
                level,
                "http.request",
                extra={
                    "method": scope["method"],
                    "path": path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            reset_correlation_id(token)


class ClientHeaderGuard:
    """CSRF guard for the loopback API.

    State-changing requests must carry ``X-ICBM-Client``. Browsers only attach custom headers
    same-origin or after a CORS preflight, and no CORS is configured, so a third-party page
    cannot drive protected actions through the operator's browser.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] not in _SAFE_METHODS
            and scope["path"].startswith("/api/")
            and not Headers(scope=scope).get(CLIENT_HEADER)
        ):
            response = JSONResponse(
                error_envelope(
                    ErrorClass.POLICY_BLOCKED,
                    "CLIENT_HEADER_REQUIRED",
                    f"state-changing API requests require the {CLIENT_HEADER} header",
                    get_correlation_id(),
                ),
                status_code=403,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
