"""The registry-gated SmartStore caller (ENDPOINT_MATRIX.md §13, §14; Issue #39 §2-§8).

Every provider response here comes from a fake transport; no socket is opened.
"""

import logging
import urllib.parse
from collections.abc import Callable

import httpx
import pytest

from app.connect.marketplace.capability import RemoteOutcome
from app.core import egress
from app.core.egress import EGRESS, EgressBlockedError, EgressGuard
from app.core.errors import ErrorClass
from integrations.marketplaces.smartstore.caller import (
    EGRESS_OWNER,
    AccountRequest,
    SellerAccount,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
    TokenGrant,
    TokenRequest,
)
from integrations.marketplaces.smartstore.registry import NOT_ADOPTED, PROVIDER_HOST, EndpointId
from integrations.marketplaces.smartstore.signing import ApplicationCredentials, client_secret_sign
from integrations.marketplaces.smartstore.transmission import Phase

TOKEN = EndpointId.SMARTSTORE_AUTH_TOKEN
ACCOUNT = EndpointId.SMARTSTORE_SELLER_ACCOUNT
# Fixture values; none is a real credential.
SECRET = "$2a$04$abcdefghijklmnopqrstuu"
CLIENT_ID = "fixture-client-id-5d1e"
TIMESTAMP_MS = 1757894400000
SIGN = client_secret_sign(CLIENT_ID, SECRET, TIMESTAMP_MS)
BEARER = "fixture-access-token-Qx7"
ACCOUNT_UID = "uid-fixture-1"
CREDENTIALS = ApplicationCredentials(CLIENT_ID, SECRET, 3)
TOKEN_URL = "https://api.commerce.naver.com/external/v1/oauth2/token"
ACCOUNT_URL = "https://api.commerce.naver.com/external/v1/seller/account"
TOKEN_BODY = {"access_token": BEARER, "expires_in": 10800, "token_type": "Bearer"}
ACCOUNT_BODY = {"accountUid": ACCOUNT_UID, "accountId": "account-fixture-1"}

Respond = Callable[[httpx.Request], httpx.Response]


class Provider:
    """A fake SmartStore: records every request that reached the transport and the egress grant
    in force while it was handled."""

    def __init__(self, respond: Respond) -> None:
        self._respond = respond
        self.requests: list[httpx.Request] = []
        self.grants: list[object] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.grants.append(egress._active_grant.get())
        return self._respond(request)


def _json(body: object, status: int = 200, headers: dict[str, str] | None = None) -> Respond:
    return lambda request: httpx.Response(status, json=body, headers=headers)


def _caller(provider: Provider) -> SmartStoreEndpointCaller:
    return SmartStoreEndpointCaller(transport=httpx.MockTransport(provider))


def _token(provider: Provider) -> TokenGrant:
    return _caller(provider).call(TOKEN, TokenRequest(CREDENTIALS, TIMESTAMP_MS))


def _account(provider: Provider) -> SellerAccount:
    return _caller(provider).call(ACCOUNT, AccountRequest(BEARER, 3, 7))


def _failure(call: Callable[[Provider], object], provider: Provider) -> SmartStoreCallError:
    with pytest.raises(SmartStoreCallError) as caught:
        call(provider)
    return caught.value


def _evidence(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "smartstore.request"]


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any client construction or egress grant is recorded and refused."""
    reached: list[str] = []

    def refuse(name: str) -> Callable[..., object]:
        def refused(*args: object, **kwargs: object) -> object:
            reached.append(name)
            raise AssertionError(f"{name} was reached")

        return refused

    monkeypatch.setattr(httpx, "Client", refuse("httpx.Client"))
    monkeypatch.setattr(EGRESS, "grant", refuse("egress grant"))
    return reached


# ---------------------------------------------------------------- request shape


def test_em13_4_the_token_request_is_the_self_form_on_the_contract_url() -> None:
    provider = Provider(_json(TOKEN_BODY))
    grant = _token(provider)
    (request,) = provider.requests
    assert (request.method, str(request.url)) == ("POST", TOKEN_URL)
    assert request.url.query == b""
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert "authorization" not in request.headers
    form = dict(urllib.parse.parse_qsl(request.content.decode(), strict_parsing=True))
    assert form == {
        "client_id": CLIENT_ID,
        "timestamp": str(TIMESTAMP_MS),
        "client_secret_sign": SIGN,
        "grant_type": "client_credentials",
        "type": "SELF",
    }
    assert grant == TokenGrant(BEARER, "Bearer", 10800, 3)


def test_em13_5_the_account_read_carries_the_given_bearer_and_no_body() -> None:
    provider = Provider(_json(ACCOUNT_BODY))
    account = _account(provider)
    (request,) = provider.requests
    assert (request.method, str(request.url)) == ("GET", ACCOUNT_URL)
    assert request.headers["authorization"] == f"Bearer {BEARER}"
    assert request.content == b""
    assert "content-type" not in request.headers
    assert account == SellerAccount(ACCOUNT_UID, "account-fixture-1", 3, 7)


@pytest.mark.parametrize(
    ("call", "body", "read"), [(_token, TOKEN_BODY, 30.0), (_account, ACCOUNT_BODY, 10.0)]
)
def test_em13_6_each_endpoint_applies_its_own_timeouts(
    call: Callable[[Provider], object], body: object, read: float
) -> None:
    provider = Provider(_json(body))
    call(provider)
    applied = provider.requests[0].extensions["timeout"]
    assert applied == {"connect": 5.0, "read": read, "write": 5.0, "pool": 5.0}
    assert applied != httpx.Timeout(5.0).as_dict()  # never the library default


# ---------------------------------------------------------------- NOT_ADOPTED and preflight


@pytest.mark.parametrize(
    "endpoint",
    [
        *sorted(NOT_ADOPTED),
        "SMARTSTORE_PRODUCT_CREATE_V2",
        "https://api.commerce.naver.com/external/v2/products",
        None,
    ],
)
def test_s17_08_em13_2_not_adopted_fails_before_network_io(
    endpoint: object, no_network: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    # CAPABILITY_MAPPING §17 target 8 (owner PR-A): the real registry-gated caller with a
    # transport spy, a client-construction spy and an egress-grant spy.
    caplog.set_level(logging.INFO, logger="icbm.connect.smartstore")
    provider = Provider(_json(TOKEN_BODY))
    before = EGRESS.snapshot()
    error = _failure(
        lambda p: _caller(p).call(endpoint, TokenRequest(CREDENTIALS, TIMESTAMP_MS)), provider
    )
    assert provider.requests == [] and no_network == []
    after = EGRESS.snapshot()
    assert (after["external_attempts"], after["granted_events"]) == (
        before["external_attempts"],
        before["granted_events"],
    )
    assert (error.error_class, error.code, error.phase, error.remote_outcome) == (
        ErrorClass.FATAL,
        "SMARTSTORE_ENDPOINT_NOT_ADOPTED",
        Phase.LOCAL_PREFLIGHT,
        RemoteOutcome.NOT_APPLIED_PROVEN,
    )
    (record,) = _evidence(caplog)
    assert (record.result_class, record.transmission_events) == (
        "SMARTSTORE_ENDPOINT_NOT_ADOPTED",
        [],
    )


@pytest.mark.parametrize(
    ("endpoint", "request_data", "code"),
    [
        (TOKEN, AccountRequest(BEARER, 3, 7), "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"),
        (ACCOUNT, TokenRequest(CREDENTIALS, TIMESTAMP_MS), "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"),
        (TOKEN, {"account_id": "x"}, "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"),
        (TOKEN, TokenRequest(CREDENTIALS, 0), "SMARTSTORE_REQUEST_CONTRACT_VIOLATION"),
        (
            TOKEN,
            TokenRequest(ApplicationCredentials(CLIENT_ID, "not-a-bcrypt-salt", 3), TIMESTAMP_MS),
            "SMARTSTORE_SIGNATURE_UNAVAILABLE",
        ),
        (ACCOUNT, AccountRequest("", 3, 7), "SMARTSTORE_BEARER_UNUSABLE"),
        (ACCOUNT, AccountRequest("two words", 3, 7), "SMARTSTORE_BEARER_UNUSABLE"),
        (ACCOUNT, AccountRequest("token\r\nX-Injected: 1", 3, 7), "SMARTSTORE_BEARER_UNUSABLE"),
        (ACCOUNT, AccountRequest(BEARER, 3, 0), "SMARTSTORE_SESSION_NOT_COMMITTED"),
        (ACCOUNT, AccountRequest(BEARER, 0, 7), "SMARTSTORE_SESSION_NOT_COMMITTED"),
    ],
)
def test_a_request_breaking_the_endpoint_contract_never_reaches_the_transport(
    endpoint: EndpointId, request_data: object, code: str, no_network: list[str]
) -> None:
    provider = Provider(_json(TOKEN_BODY))
    error = _failure(lambda p: _caller(p).call(endpoint, request_data), provider)
    assert provider.requests == [] and no_network == []
    assert (error.error_class, error.code, error.remote_outcome) == (
        ErrorClass.FATAL,
        code,
        RemoteOutcome.NOT_APPLIED_PROVEN,
    )


# ---------------------------------------------------------------- success predicates


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["access_token"]),
        httpx.Response(200, json={**TOKEN_BODY, "access_token": ""}),
        httpx.Response(200, json={**TOKEN_BODY, "expires_in": 0}),
        httpx.Response(200, json={**TOKEN_BODY, "expires_in": "10800"}),
        httpx.Response(200, json={**TOKEN_BODY, "token_type": "mac"}),
        httpx.Response(201, json=TOKEN_BODY),
    ],
)
def test_em13_9_a_malformed_token_2xx_fails_closed(response: httpx.Response) -> None:
    provider = Provider(lambda request: response)
    error = _failure(_token, provider)
    assert (error.error_class, error.code) == (
        ErrorClass.UNKNOWN,
        "SMARTSTORE_SUCCESS_PREDICATE_FAILED",
    )
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>"),
        httpx.Response(200, json={"accountId": "account-fixture-1"}),
        httpx.Response(200, json={"accountUid": ""}),
        httpx.Response(200, json={"accountUid": 42}),
    ],
)
def test_em13_9_a_malformed_account_2xx_fails_closed(response: httpx.Response) -> None:
    error = _failure(_account, Provider(lambda request: response))
    assert (error.error_class, error.code) == (
        ErrorClass.UNKNOWN,
        "SMARTSTORE_SUCCESS_PREDICATE_FAILED",
    )


@pytest.mark.parametrize(
    ("body", "account_id"),
    [
        ({"accountUid": ACCOUNT_UID}, None),
        ({"accountUid": ACCOUNT_UID, "accountId": 12345}, None),
        ({"accountUid": ACCOUNT_UID, "accountId": ""}, None),
        (ACCOUNT_BODY, "account-fixture-1"),
    ],
)
def test_em14_8_account_id_is_secondary_evidence_when_available(
    body: dict[str, object], account_id: str | None
) -> None:
    account = _account(Provider(_json(body)))
    assert (account.account_uid, account.account_id) == (ACCOUNT_UID, account_id)


# ---------------------------------------------------------------- classification


@pytest.mark.parametrize("code", ["STORE_NOT_FOUND", "CHANNEL_NOT_FOUND", "GW.NOT_FOUND", None])
def test_a_provider_404_is_unknown_never_not_found(code: str | None) -> None:
    error = _failure(_account, Provider(_json({"code": code} if code else {}, 404)))
    assert error.error_class is ErrorClass.UNKNOWN
    assert error.error_class is not ErrorClass.NOT_FOUND
    assert (error.http_status, error.remote_outcome) == (404, RemoteOutcome.UNKNOWN)


@pytest.mark.parametrize(
    ("status", "code", "error_class"),
    [
        (404, "STORE_NOT_FOUND", ErrorClass.UNKNOWN),
        (401, "GW.AUTHN", ErrorClass.UNKNOWN),
        (400, None, ErrorClass.UNKNOWN),
        (500, "GW.INTERNAL_SERVER_ERROR", ErrorClass.TRANSIENT),
        (429, "GW.RATE_LIMIT", ErrorClass.RATE_LIMITED),
    ],
)
@pytest.mark.parametrize("call", [_token, _account])
def test_a_failure_is_classified_once_and_never_replayed(
    status: int, code: str | None, error_class: ErrorClass, call: Callable[[Provider], object]
) -> None:
    provider = Provider(_json({"code": code} if code else {}, status))
    error = _failure(call, provider)
    assert error.error_class is error_class
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("connection reset"),
    ],
)
def test_a_transport_error_without_phase_evidence_stays_unknown(exception: Exception) -> None:
    # The fake transport emits no trace: an exception's name alone proves no phase.
    def respond(request: httpx.Request) -> httpx.Response:
        raise exception

    provider = Provider(respond)
    error = _failure(_token, provider)
    assert (error.error_class, error.phase, error.remote_outcome) == (
        ErrorClass.TRANSIENT,
        Phase.UNOBSERVED,
        RemoteOutcome.UNKNOWN,
    )
    assert len(provider.requests) == 1


def test_an_unfamiliar_failure_is_unknown() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected")

    error = _failure(_account, Provider(respond))
    assert (error.error_class, error.remote_outcome) == (ErrorClass.UNKNOWN, RemoteOutcome.UNKNOWN)


# ---------------------------------------------------------------- redirects (EM §14.12)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "location", ["https://evil.example/collect", f"https://{PROVIDER_HOST}/external/v2/products"]
)
@pytest.mark.parametrize(("call", "url"), [(_token, TOKEN_URL), (_account, ACCOUNT_URL)])
def test_em13_7_a_redirect_is_never_followed_and_is_kept_as_evidence(
    status: int,
    location: str,
    call: Callable[[Provider], object],
    url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="icbm.connect.smartstore")
    provider = Provider(lambda request: httpx.Response(status, headers={"Location": location}))
    error = _failure(call, provider)
    # One request, to the contract URL: nothing — credential or body — was sent anywhere else.
    assert [str(r.url) for r in provider.requests] == [url]
    assert (error.error_class, error.code, error.http_status) == (
        ErrorClass.UNKNOWN,
        "SMARTSTORE_UNEXPECTED_REDIRECT",
        status,
    )
    (record,) = _evidence(caplog)
    assert (record.http_status, record.result_class) == (status, "SMARTSTORE_UNEXPECTED_REDIRECT")


# ---------------------------------------------------------------- egress (Issue #39 §5)


def test_the_grant_covers_the_provider_host_for_this_call_only() -> None:
    provider = Provider(_json(ACCOUNT_BODY))
    assert egress._active_grant.get() is None
    _account(provider)
    (grant,) = provider.grants
    assert isinstance(grant, egress._Grant)
    assert (grant.owner, grant.hosts) == (EGRESS_OWNER, frozenset({PROVIDER_HOST}))
    assert EGRESS_OWNER == "marketplace:smartstore"
    assert egress._active_grant.get() is None  # nothing leaks past the call


def test_the_grant_replaces_an_outer_grant_without_widening_it_and_restores_it() -> None:
    provider = Provider(_json(ACCOUNT_BODY))
    with EGRESS.grant("supplier:kmretail", {"kmretail.example"}):
        _account(provider)
        outer = egress._active_grant.get()
        assert outer is not None and outer.owner == "supplier:kmretail"
    (inner,) = provider.grants
    assert isinstance(inner, egress._Grant)
    assert inner.hosts == frozenset({PROVIDER_HOST})


def test_inside_the_grant_only_the_provider_host_is_reachable() -> None:
    guard = EgressGuard()
    decisions: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        for host in (PROVIDER_HOST, "evil.example", "www.naver.com", "kmretail.example"):
            try:
                guard._hook("socket.getaddrinfo", (host, 443))
                decisions[host] = "allowed"
            except EgressBlockedError:
                decisions[host] = "blocked"
        return httpx.Response(200, json=ACCOUNT_BODY)

    _account(Provider(respond))
    assert decisions == {
        PROVIDER_HOST: "allowed",
        "evil.example": "blocked",
        "www.naver.com": "blocked",
        "kmretail.example": "blocked",
    }
    assert guard.snapshot()["granted_events"] == {EGRESS_OWNER: 1}
    with pytest.raises(EgressBlockedError):  # and after the call, not even the provider host
        guard._hook("socket.getaddrinfo", (PROVIDER_HOST, 443))


def test_an_egress_refusal_is_policy_blocked_and_proves_non_application() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise EgressBlockedError("egress policy blocks external destination")

    error = _failure(_account, Provider(respond))
    assert (error.error_class, error.phase, error.remote_outcome) == (
        ErrorClass.POLICY_BLOCKED,
        Phase.EGRESS_BLOCKED,
        RemoteOutcome.NOT_APPLIED_PROVEN,
    )


# ---------------------------------------------------------------- evidence (EM §13 #10)


def test_em13_10_evidence_records_the_endpoint_generations_and_policy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="icbm.connect.smartstore")
    _token(Provider(_json(TOKEN_BODY)))
    _account(Provider(_json(ACCOUNT_BODY)))
    token, account = _evidence(caplog)
    assert (token.endpoint_id, token.method, token.credential_generation) == (
        "SMARTSTORE_AUTH_TOKEN",
        "POST",
        3,
    )
    assert token.session_generation is None  # a token call has no session yet
    assert (token.connect_timeout_s, token.read_timeout_s) == (5.0, 30.0)
    assert (account.endpoint_id, account.method) == ("SMARTSTORE_SELLER_ACCOUNT", "GET")
    assert (account.credential_generation, account.session_generation) == (3, 7)
    assert (account.connect_timeout_s, account.read_timeout_s) == (5.0, 10.0)
    assert (account.redirect_policy, account.predicate_revision) == ("NO_FOLLOW", "em7-account-r1")
    assert (account.http_status, account.result_class, account.error_class) == (
        200,
        "SUCCEEDED",
        None,
    )


def test_failure_evidence_records_the_classification_and_the_outcome_decision(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="icbm.connect.smartstore")
    body = {"code": "STORE_NOT_FOUND", "message": "x", "traceId": "trace-0a1b"}
    _failure(_account, Provider(_json(body, 404)))
    (record,) = _evidence(caplog)
    assert (record.http_status, record.provider_code, record.provider_trace_id) == (
        404,
        "STORE_NOT_FOUND",
        "trace-0a1b",
    )
    assert (record.error_class, record.failure_layer, record.classification_basis) == (
        "UNKNOWN",
        "API_SERVER_DOMAIN",
        "UNKNOWN",
    )
    assert (record.transmission_phase, record.remote_outcome) == ("RESPONSE_RECEIVED", "UNKNOWN")


def test_provider_markers_are_kept_only_in_their_safe_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="icbm.connect.smartstore")
    body = {"code": "not a code; DROP", "traceId": "x" * 101}
    headers = {"GNCP-GW-Trace-ID": "gw-trace-77"}
    error = _failure(_account, Provider(_json(body, 400, headers)))
    assert error.classification.provider_code is None
    (record,) = _evidence(caplog)
    assert (record.provider_code, record.provider_trace_id) == (None, "gw-trace-77")


# ---------------------------------------------------------------- secrets (Issue #39 §8)


def _all_text(caplog: pytest.LogCaptureFixture, errors: list[SmartStoreCallError]) -> str:
    parts = [caplog.text]
    for record in caplog.records:
        parts.extend(str(value) for value in vars(record).values())
    for error in errors:
        parts += [str(error), repr(error), str(error.details), repr(error.__cause__)]
    return "\n".join(parts)


def test_no_secret_signature_bearer_or_identity_reaches_logs_or_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)  # every logger, the HTTP libraries' included
    grant = _token(Provider(_json(TOKEN_BODY)))
    account = _account(Provider(_json(ACCOUNT_BODY)))
    # A provider that echoes request material back in its error body.
    echo = {"code": "GW.AUTHN", "message": f"{BEARER} {SIGN} {SECRET} {CLIENT_ID} {ACCOUNT_UID}"}
    errors = [_failure(call, Provider(_json(echo, 401))) for call in (_token, _account)]
    unusable = ApplicationCredentials(CLIENT_ID, "$2a$04$not-a-valid-salt-value", 3)
    errors.append(
        _failure(
            lambda p: _caller(p).call(TOKEN, TokenRequest(unusable, TIMESTAMP_MS)),
            Provider(_json(TOKEN_BODY)),
        )
    )
    text = _all_text(caplog, errors)
    for secret in (SECRET, SIGN, BEARER, CLIENT_ID, ACCOUNT_UID, "$2a$04$not-a-valid-salt-value"):
        assert secret not in text
    for value in (grant, account, AccountRequest(BEARER, 3, 7), TokenRequest(CREDENTIALS, 1)):
        assert BEARER not in repr(value) and SECRET not in repr(value)
        assert ACCOUNT_UID not in repr(value)
