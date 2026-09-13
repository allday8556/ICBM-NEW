"""K홀세일 (KM통상, https://kmretail.co.kr) — site knowledge only (Issue #7, ADR-0007).

This package describes the supplier; it cannot make a request. It holds no client, page or
transport: the common CONNECT layer fetches and logs in, and hands these predicates nothing but an
immutable ``ProbeResponse`` (Issue #7 comment 5653615136). A repository rule keeps network, browser
and logging imports out of this package.

The site is a Cafe24 storefront, so the member's login state is rendered server-side by the
``xans-layout-statelogon`` / ``xans-layout-statelogoff`` modules. Observed without any account on
2026-09-13: an unauthenticated GET of the protected target answers HTTP 200 — not a redirect —
with the logged-off state module and a script that sends the browser to the login page, while the
``xans-myshop`` page skeleton is present anyway. Neither the status nor the skeleton can therefore
prove authentication; the authenticated predicate requires the logged-on state module and the
member logout action together.
"""

from integrations.suppliers.base import (
    LoginFormSpec,
    ProbeResponse,
    ProtectedReadProbe,
    RequestPolicy,
    SupplierDefinition,
    SupplierProfile,
    Verdict,
)

LOGIN_PATH = "/member/login.html"
# 마이쇼핑: exists only for a signed-in member. Its product contents are never read or kept.
PROTECTED_TARGET = "/myshop/index.html"

_LOGIN_FORM = 'form[action="/exec/front/Member/login/"]'
_STATE_LOGOFF = "xans-layout-statelogoff"
_STATE_LOGON = "xans-layout-statelogon"
_LOGIN_CHECK_REDIRECT = "toMoveLoginCheckModule"
_LOGOUT_ACTION = "/exec/front/Member/logout/"
# Informational only (never decide the verdict): recorded as signal names so a changed page
# layout can be diagnosed from the audit trail without ever storing page content.
_INFORMATIONAL = {"로그아웃": "logout_text", "/member/modify.html": "member_modify_link"}


def login_required(response: ProbeResponse) -> Verdict:
    """The target demands a login: redirect to it, denial, or the logged-off markers."""
    signals: list[str] = []
    if response.location and response.location.startswith("/member/login"):
        signals.append("redirect_to_login")
    if response.status in (401, 403):
        signals.append("access_denied")
    if _STATE_LOGOFF in response.body:
        signals.append("state_logoff")
    if _LOGIN_CHECK_REDIRECT in response.body:
        signals.append("login_check_redirect")
    return bool(signals), tuple(signals)


def authenticated(response: ProbeResponse) -> Verdict:
    """Only a signed-in member's page carries the logged-on module and the logout action."""
    signals: list[str] = []
    if _STATE_LOGON in response.body:
        signals.append("state_logon")
    if _LOGOUT_ACTION in response.body:
        signals.append("logout_action")
    proven = response.status == 200 and len(signals) == 2
    signals.extend(name for marker, name in _INFORMATIONAL.items() if marker in response.body)
    return proven, tuple(signals)


PROFILE = SupplierProfile(
    supplier_key="kmretail",
    display_name="K홀세일",
    base_url="https://kmretail.co.kr",
    auth_required=True,
    # The storefront itself, and Cafe24's secure-login host that encrypts the login form.
    egress_hosts=frozenset({"kmretail.co.kr", "login2.cafe24ssl.com"}),
    request_policy=RequestPolicy(
        max_concurrency=1,
        minimum_request_interval_s=2.0,
        auth_retry_limit=3,
        request_timeout_s=20.0,
    ),
)

DEFINITION = SupplierDefinition(
    profile=PROFILE,
    probe=ProtectedReadProbe(
        target=PROTECTED_TARGET,
        unauthenticated_expectation=login_required,
        authenticated_predicate=authenticated,
    ),
    login=LoginFormSpec(
        path=LOGIN_PATH,
        username_selector=f'{_LOGIN_FORM} input[name="member_id"]',
        password_selector=f'{_LOGIN_FORM} input[name="member_passwd"]',
        submit_selector=f"{_LOGIN_FORM} a.btnLogin",
    ),
)
