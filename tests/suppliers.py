"""A test supplier and gateway: no network, no browser, exact request and login counts."""

import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.errors import AppError, AuthError
from integrations.suppliers.base import (
    Credentials,
    LoginFormSpec,
    ProbeResponse,
    ProtectedReadProbe,
    RequestKind,
    RequestPolicy,
    SupplierDefinition,
    SupplierProfile,
    Verdict,
)
from integrations.suppliers.transport.session_payload import encode_session

FAKE_KEY = "fakesupplier"
USERNAME = "icbm-test-operator@example.invalid"
# Characters that change under URL, JSON, HTML and Base64 encodings.
PASSWORD = 'Pa$$ w0rd/ü&한글<"secret">'
SESSION_TOKEN = "fake-session-token-7f3a9c"


@dataclass(frozen=True)
class SitePages:
    logged_out: str
    logged_in: str


FAKE_PAGES = SitePages(
    logged_out='<div class="state-logoff"></div>',
    logged_in='<div class="state-logon"></div><a href="/logout">',
)
# Synthetic pages carrying only the markers KM통상's predicates look for — no real page content.
# Both carry the myshop page skeleton, which a signed-out visitor also receives.
KMRETAIL_PAGES = SitePages(
    logged_out=(
        '<div class="xans-element- xans-myshop xans-myshop-asyncbankbook"></div>'
        '<div class="xans-element- xans-layout xans-layout-statelogoff"></div>'
        "<script>var sCheckUseModule = /toMoveLoginCheckModule/;</script>"
    ),
    logged_in=(
        '<div class="xans-element- xans-myshop xans-myshop-asyncbankbook"></div>'
        '<div class="xans-element- xans-layout xans-layout-statelogon">'
        '<a href="/exec/front/Member/logout/">LOGOUT</a></div>'
    ),
)


def _login_required(response: ProbeResponse) -> Verdict:
    hit = "state-logoff" in response.body
    return hit, ("state_logoff",) if hit else ()


def _authenticated(response: ProbeResponse) -> Verdict:
    hit = response.status == 200 and "state-logon" in response.body and "/logout" in response.body
    return hit, ("state_logon",) if hit else ()


def fake_definition(*, auth_retry_limit: int = 3) -> SupplierDefinition:
    return SupplierDefinition(
        profile=SupplierProfile(
            supplier_key=FAKE_KEY,
            display_name="Fake supplier",
            base_url="https://supplier.test",
            auth_required=True,
            egress_hosts=frozenset({"supplier.test"}),
            request_policy=RequestPolicy(
                minimum_request_interval_s=0.0, auth_retry_limit=auth_retry_limit
            ),
        ),
        probe=ProtectedReadProbe(
            target="/member",
            unauthenticated_expectation=_login_required,
            authenticated_predicate=_authenticated,
        ),
        login=LoginFormSpec(
            path="/login", username_selector="#id", password_selector="#pw", submit_selector="#go"
        ),
    )


class FakeGateway:
    """Simulates the supplier. Sessions it issued stay valid until ``expire_sessions()``."""

    def __init__(self, pages: SitePages = FAKE_PAGES) -> None:
        self.pages = pages
        self.accepted = Credentials(username=USERNAME, password=PASSWORD)
        self.valid_sessions: set[bytes] = set()
        self.logins = 0
        self.requests: list[RequestKind] = []
        self.target_is_public = False
        self.unrecognized = False
        self.login_effective = True
        self.login_error: AppError | None = None
        self.fetch_error: AppError | None = None
        self.login_gate: threading.Event | None = None
        self._lock = threading.Lock()

    def fetch(
        self, definition: SupplierDefinition, *, kind: RequestKind, session: bytes | None
    ) -> ProbeResponse:
        with self._lock:
            self.requests.append(kind)
        if self.fetch_error is not None:
            raise self.fetch_error
        if kind is RequestKind.PROTECTED_READ and self.unrecognized:
            body = "<html>maintenance</html>"
        elif self.target_is_public or (session is not None and session in self.valid_sessions):
            body = self.pages.logged_in
        else:
            body = self.pages.logged_out
        return ProbeResponse(status=200, path=definition.probe.target, location=None, body=body)

    def login(self, definition: SupplierDefinition, credentials: Credentials) -> bytes:
        with self._lock:
            self.logins += 1
            number = self.logins
        if self.login_gate is not None:
            self.login_gate.wait(10)
        if self.login_error is not None:
            raise self.login_error
        if credentials != self.accepted:
            raise AuthError("SUPPLIER_LOGIN_REJECTED", "the supplier rejected the login")
        host = urlsplit(definition.profile.base_url).hostname or ""
        session = encode_session(
            [{"name": "SID", "value": f"{SESSION_TOKEN}-{number}", "domain": host, "path": "/"}],
            user_agent="FakeSupplier/1",
            hosts={host},
        )
        if self.login_effective:
            self.valid_sessions.add(session)
        return session

    def expire_sessions(self) -> None:
        self.valid_sessions.clear()

    def count(self, kind: RequestKind) -> int:
        return self.requests.count(kind)
