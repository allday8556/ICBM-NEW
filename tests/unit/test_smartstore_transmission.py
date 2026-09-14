"""Transmission phase and remote_outcome (ERRORS.md §15, §23.12-§23.14; Issue #39 §7).

The first half judges trace evidence directly. The second half runs the production HTTP stack —
httpx's transport and httpcore's own connection, TLS and HTTP/1.1 code — over an in-memory
network, so the trace events and exception mapping the rules depend on are the real ones
(§23.14). No socket is opened.
"""

import socket
import ssl

import httpcore
import httpx
import pytest

from app.connect.marketplace.capability import RemoteOutcome
from app.core.egress import EgressBlockedError
from app.core.errors import ErrorClass
from integrations.marketplaces.smartstore.caller import (
    AccountRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
    TokenRequest,
)
from integrations.marketplaces.smartstore.registry import EndpointId
from integrations.marketplaces.smartstore.signing import ApplicationCredentials
from integrations.marketplaces.smartstore.transmission import (
    TRANSMISSION_PRECLUDED,
    Phase,
    TraceRecorder,
    remote_outcome,
    transmission_phase,
)


def _caused(error: BaseException, cause: BaseException) -> BaseException:
    error.__cause__ = cause
    return error


CONNECT_FAILED = ("connection.connect_tcp.started", "connection.connect_tcp.failed")
TCP_OPENED = ("connection.connect_tcp.started", "connection.connect_tcp.complete")
TLS_DONE = (*TCP_OPENED, "connection.start_tls.started", "connection.start_tls.complete")
HEADERS_STARTED = (*TLS_DONE, "http11.send_request_headers.started")
REQUEST_WRITTEN = (
    *HEADERS_STARTED,
    "http11.send_request_headers.complete",
    "http11.send_request_body.started",
    "http11.send_request_body.complete",
)


# ---------------------------------------------------------------- trace evidence


@pytest.mark.parametrize(
    ("events", "error", "phase"),
    [
        pytest.param(
            CONNECT_FAILED,
            _caused(httpx.ConnectError("refused"), EgressBlockedError("egress policy blocks")),
            Phase.EGRESS_BLOCKED,
            id="egress-policy",
        ),
        pytest.param(
            (), EgressBlockedError("egress policy blocks"), Phase.EGRESS_BLOCKED, id="egress-raw"
        ),
        pytest.param(
            CONNECT_FAILED,
            _caused(httpx.ConnectError("getaddrinfo failed"), socket.gaierror(11001, "no host")),
            Phase.DNS_FAILURE,
            id="dns",
        ),
        pytest.param(
            CONNECT_FAILED,
            _caused(httpx.ConnectError("refused"), ConnectionRefusedError(10061, "refused")),
            Phase.TCP_CONNECT_FAILURE,
            id="tcp-refused",
        ),
        pytest.param(
            CONNECT_FAILED,
            _caused(httpx.ConnectTimeout("timed out"), TimeoutError("timed out")),
            Phase.TCP_CONNECT_FAILURE,
            id="tcp-connect-timeout",
        ),
        pytest.param(
            (*TCP_OPENED, "connection.start_tls.started", "connection.start_tls.failed"),
            _caused(httpx.ConnectError("handshake"), ssl.SSLError(1, "handshake failure")),
            Phase.TLS_HANDSHAKE_FAILURE,
            id="tls-handshake",
        ),
    ],
)
def test_whitelisted_pre_send_failures_prove_non_application(
    events: tuple[str, ...], error: BaseException, phase: Phase
) -> None:
    assert transmission_phase(events, error) is phase
    assert remote_outcome(phase) is RemoteOutcome.NOT_APPLIED_PROVEN


@pytest.mark.parametrize(
    ("events", "error", "phase"),
    [
        pytest.param(
            ("connection.close.started", "connection.close.complete"),
            _caused(httpx.ConnectError("connection reset"), ConnectionResetError(104, "reset")),
            Phase.NO_NEW_CONNECTION,
            id="pooled-connection-reset",
        ),
        pytest.param(
            (
                *REQUEST_WRITTEN,
                "http11.receive_response_headers.started",
                "http11.receive_response_headers.failed",
            ),
            httpx.ReadTimeout("timed out"),
            Phase.REQUEST_SENT,
            id="read-timeout",
        ),
        pytest.param(
            (
                *REQUEST_WRITTEN,
                "http11.receive_response_headers.started",
                "http11.receive_response_headers.complete",
                "http11.receive_response_body.started",
                "http11.receive_response_body.failed",
            ),
            _caused(httpx.ReadError("dropped"), ConnectionResetError(104, "reset")),
            Phase.REQUEST_SENT,
            id="drop-while-reading",
        ),
        pytest.param(
            (*HEADERS_STARTED, "http11.send_request_headers.failed"),
            httpx.WriteError("broken pipe"),
            Phase.REQUEST_SENT,
            id="failure-after-the-write-started",
        ),
        pytest.param(
            (
                *HEADERS_STARTED,
                "http11.send_request_headers.complete",
                "http11.send_request_body.started",
                "http11.send_request_body.failed",
            ),
            httpx.WriteTimeout("timed out"),
            Phase.REQUEST_SENT,
            id="write-timeout-after-handoff",
        ),
        pytest.param(
            (
                *TLS_DONE,
                "http2.send_connection_init.started",
                "http2.send_connection_init.complete",
                "http2.send_request_headers.started",
                "http2.send_request_headers.complete",
                "http2.receive_response_headers.started",
                "http2.receive_response_headers.failed",
            ),
            httpx.RemoteProtocolError("<StreamReset stream_id:1, error_code:2>"),
            Phase.REQUEST_SENT,
            id="http2-stream-reset",
        ),
        pytest.param(
            (), httpx.ConnectError("connect failed"), Phase.UNOBSERVED, id="connect-name-only"
        ),
        pytest.param(
            (), httpx.TimeoutException("timed out"), Phase.UNOBSERVED, id="timeout-unknown-phase"
        ),
        pytest.param(
            ("connection.connect_tcp.started",),
            RuntimeError("cancelled"),
            Phase.UNOBSERVED,
            id="connect-never-finished",
        ),
        pytest.param(
            TLS_DONE, httpx.LocalProtocolError("bad"), Phase.UNOBSERVED, id="after-tls-before-send"
        ),
        pytest.param(
            (*HEADERS_STARTED, "http11.send_request_headers.failed"),
            _caused(httpx.ConnectError("getaddrinfo failed"), socket.gaierror(11001, "no host")),
            Phase.REQUEST_SENT,
            id="connect-language-after-send",
        ),
    ],
)
def test_possible_transmission_is_never_proof_of_non_application(
    events: tuple[str, ...], error: BaseException, phase: Phase
) -> None:
    assert transmission_phase(events, error) is phase
    assert remote_outcome(phase) is RemoteOutcome.UNKNOWN


def test_only_the_five_precluding_phases_prove_non_application() -> None:
    whitelist = {
        Phase.LOCAL_PREFLIGHT,
        Phase.EGRESS_BLOCKED,
        Phase.DNS_FAILURE,
        Phase.TCP_CONNECT_FAILURE,
        Phase.TLS_HANDSHAKE_FAILURE,
    }
    assert whitelist == TRANSMISSION_PRECLUDED
    for phase in Phase:
        expected = (
            RemoteOutcome.NOT_APPLIED_PROVEN
            if phase in TRANSMISSION_PRECLUDED
            else RemoteOutcome.UNKNOWN
        )
        assert remote_outcome(phase) is expected


def test_the_trace_recorder_keeps_event_names_only() -> None:
    recorder = TraceRecorder()
    recorder("http11.send_request_headers.started", {"request": object(), "headers": ["x"]})
    assert recorder.events == ("http11.send_request_headers.started",)
    assert list(vars(recorder)) == ["_events"]


# ---------------------------------------------------------------- the production stack


class FakeNetwork(httpcore.NetworkBackend):
    """An in-memory network under httpcore: connection, TLS and HTTP/1.1 run for real."""

    def __init__(
        self,
        *,
        response: bytes = b"",
        connect_error: BaseException | None = None,
        tls_error: BaseException | None = None,
        write_error: BaseException | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.connect_error = connect_error
        self.tls_error = tls_error
        self.write_error = write_error
        self.read_error = read_error
        self.hosts: list[tuple[str, int]] = []
        self.timeouts: dict[str, list[float | None]] = {"connect": [], "read": [], "write": []}
        self.written = b""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore.NetworkStream:
        self.hosts.append((host, port))
        self.timeouts["connect"].append(timeout)
        if self.connect_error is not None:
            _mapped(self.connect_error)
        return FakeStream(self)


def _mapped(cause: BaseException) -> None:
    """Raise ``cause`` the way httpcore's socket backend maps a connect-phase failure: from
    inside the handler, so the original error is both the cause and the context."""
    mapped = httpcore.ConnectTimeout if isinstance(cause, TimeoutError) else httpcore.ConnectError
    try:
        raise cause
    except BaseException as exc:
        raise mapped(str(exc)) from exc


class FakeStream(httpcore.NetworkStream):
    def __init__(self, network: FakeNetwork) -> None:
        self._network = network
        self._chunks = [network.response] if network.response else []

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self._network.timeouts["read"].append(timeout)
        if self._network.read_error is not None:
            raise self._network.read_error
        return self._chunks.pop(0) if self._chunks else b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._network.timeouts["write"].append(timeout)
        if self._network.write_error is not None:
            raise self._network.write_error
        self._network.written += buffer

    def close(self) -> None:
        pass

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        if self._network.tls_error is not None:
            _mapped(self._network.tls_error)
        return self

    def get_extra_info(self, info: str) -> object:
        return None


def _http(body: bytes, status: bytes = b"200 OK", extra: bytes = b"") -> bytes:
    return (
        b"HTTP/1.1 "
        + status
        + b"\r\nContent-Type: application/json\r\n"
        + extra
        + b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


def _caller(network: FakeNetwork) -> SmartStoreEndpointCaller:
    transport = httpx.HTTPTransport()
    transport._pool = httpcore.ConnectionPool(network_backend=network)
    return SmartStoreEndpointCaller(transport=transport)


ACCOUNT_REQUEST = AccountRequest("fixture-bearer", 1, 1)
ACCOUNT_BODY = b'{"accountUid":"uid-fixture-1","accountId":"account-fixture-1"}'
TOKEN_BODY = b'{"access_token":"fixture-token","expires_in":10800,"token_type":"Bearer"}'
CREDENTIALS = ApplicationCredentials("fixture-client-id", "$2a$04$abcdefghijklmnopqrstuu", 1)


def _read_account(network: FakeNetwork) -> SmartStoreCallError:
    with pytest.raises(SmartStoreCallError) as caught:
        _caller(network).call(EndpointId.SMARTSTORE_SELLER_ACCOUNT, ACCOUNT_REQUEST)
    return caught.value


@pytest.mark.parametrize(
    ("network", "phase", "error_class"),
    [
        pytest.param(
            FakeNetwork(connect_error=socket.gaierror(11001, "getaddrinfo failed")),
            Phase.DNS_FAILURE,
            ErrorClass.TRANSIENT,
            id="dns",
        ),
        pytest.param(
            FakeNetwork(connect_error=ConnectionRefusedError(10061, "refused")),
            Phase.TCP_CONNECT_FAILURE,
            ErrorClass.TRANSIENT,
            id="tcp-refused",
        ),
        pytest.param(
            FakeNetwork(connect_error=TimeoutError("timed out")),
            Phase.TCP_CONNECT_FAILURE,
            ErrorClass.TRANSIENT,
            id="tcp-connect-timeout",
        ),
        pytest.param(
            FakeNetwork(connect_error=EgressBlockedError("egress policy blocks")),
            Phase.EGRESS_BLOCKED,
            ErrorClass.POLICY_BLOCKED,
            id="egress-policy",
        ),
        pytest.param(
            FakeNetwork(tls_error=ssl.SSLError(1, "handshake failure")),
            Phase.TLS_HANDSHAKE_FAILURE,
            ErrorClass.TRANSIENT,
            id="tls-handshake",
        ),
    ],
)
def test_the_production_stack_proves_each_whitelisted_phase(
    network: FakeNetwork, phase: Phase, error_class: ErrorClass
) -> None:
    error = _read_account(network)
    assert (error.phase, error.remote_outcome, error.error_class) == (
        phase,
        RemoteOutcome.NOT_APPLIED_PROVEN,
        error_class,
    )
    assert network.written == b""


@pytest.mark.parametrize(
    "network",
    [
        pytest.param(FakeNetwork(write_error=httpcore.WriteError("broken pipe")), id="write"),
        pytest.param(FakeNetwork(read_error=httpcore.ReadTimeout("timed out")), id="read-timeout"),
        pytest.param(FakeNetwork(read_error=httpcore.ReadError("reset")), id="reset-reading"),
        pytest.param(FakeNetwork(response=b""), id="closed-without-response"),
        pytest.param(
            FakeNetwork(response=b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"acc'),
            id="drop-while-reading-body",
        ),
    ],
)
def test_the_production_stack_keeps_any_possible_transmission_unknown(
    network: FakeNetwork,
) -> None:
    error = _read_account(network)
    assert (error.phase, error.remote_outcome) == (Phase.REQUEST_SENT, RemoteOutcome.UNKNOWN)


def test_the_production_stack_sends_the_contract_request_with_the_endpoint_timeouts() -> None:
    network = FakeNetwork(response=_http(ACCOUNT_BODY))
    account = _caller(network).call(EndpointId.SMARTSTORE_SELLER_ACCOUNT, ACCOUNT_REQUEST)
    assert account.account_uid == "uid-fixture-1"
    assert network.hosts == [("api.commerce.naver.com", 443)]
    assert network.written.startswith(b"GET /external/v1/seller/account HTTP/1.1\r\n")
    assert b"\r\nHost: api.commerce.naver.com\r\n" in network.written
    assert b"\r\nAuthorization: Bearer fixture-bearer\r\n" in network.written
    # EM §10: the socket sees the endpoint's timeouts, never a library default.
    assert set(network.timeouts["connect"]) == {5.0}
    assert set(network.timeouts["read"]) == {10.0}
    assert set(network.timeouts["write"]) == {5.0}


def test_the_production_stack_sends_the_token_request_with_the_token_timeouts() -> None:
    network = FakeNetwork(response=_http(TOKEN_BODY))
    _caller(network).call(EndpointId.SMARTSTORE_AUTH_TOKEN, TokenRequest(CREDENTIALS, 1))
    assert network.written.startswith(b"POST /external/v1/oauth2/token HTTP/1.1\r\n")
    assert b"\r\nContent-Type: application/x-www-form-urlencoded\r\n" in network.written
    assert b"account_id" not in network.written
    assert set(network.timeouts["connect"]) == {5.0}
    assert set(network.timeouts["read"]) == {30.0}


def test_the_production_stack_never_follows_a_redirect() -> None:
    network = FakeNetwork(
        response=_http(b"", b"308 Permanent Redirect", b"Location: https://evil.example/x\r\n")
    )
    error = _read_account(network)
    assert (error.http_status, error.code) == (308, "SMARTSTORE_UNEXPECTED_REDIRECT")
    assert network.hosts == [("api.commerce.naver.com", 443)]


def test_every_call_opens_its_own_connection() -> None:
    # No call ever runs on a connection another call left behind, so "a new connection failed
    # before sending" is always about this request (§15.1 item 3).
    network = FakeNetwork(response=_http(ACCOUNT_BODY))
    caller = _caller(network)
    for _ in range(2):
        caller.call(EndpointId.SMARTSTORE_SELLER_ACCOUNT, ACCOUNT_REQUEST)
    assert len(network.hosts) == 2
