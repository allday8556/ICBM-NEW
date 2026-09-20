"""The acceptance transport gate under the real caller (Issue #46 §2.1; comment 5669037896):
a request is durably reserved before any transport receives it; an over-cap, forbidden or
unapproved request never reaches one; and the live transport cannot exist under tests or CI.
Every inner transport here is a spy. No test sends anything anywhere."""

from pathlib import Path

import httpx
import pytest

from app.connect.marketplace.capability import RemoteOutcome
from integrations.marketplaces.smartstore.caller import (
    AccountRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
    TokenRequest,
)
from integrations.marketplaces.smartstore.registry import (
    ADOPTED,
    BASE_URL,
    NOT_ADOPTED,
    EndpointContract,
    EndpointId,
)
from integrations.marketplaces.smartstore.signing import ApplicationCredentials
from integrations.marketplaces.smartstore.transmission import Phase as Transmission
from scripts.m2harness import transport
from scripts.m2harness.fake_provider import Scenario
from scripts.m2harness.ledger import CAPS, SELLER, TOKEN, UNRECOGNIZED, Ledger, Mode, Phase, State
from scripts.m2harness.transport import (
    CI_MARKERS,
    BudgetedTransport,
    BudgetGateRefused,
    LiveProviderRefused,
    ci_or_test,
    live_inner,
    resolve_target,
)

ALL = frozenset(Phase)
SCENARIO = Scenario.fixture()
CREDENTIALS = ApplicationCredentials(SCENARIO.client_id, SCENARIO.client_secret, 1)
TOKEN_REQUEST = TokenRequest(CREDENTIALS, 1_726_000_000_000)
ACCOUNT_REQUEST = AccountRequest("spy-bearer-value", 1, 1)
TOKEN_URL = BASE_URL + ADOPTED[EndpointId.SMARTSTORE_AUTH_TOKEN].path
ACCOUNT_URL = BASE_URL + ADOPTED[EndpointId.SMARTSTORE_SELLER_ACCOUNT].path
HOST = httpx.URL(BASE_URL).host


class Spy(httpx.BaseTransport):
    """Records what reached it and the budget already spent at that moment."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.received: list[httpx.Request] = []
        self.spent_at_send: list[dict[str, int]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request)
        self.spent_at_send.append(self.ledger.counts())
        if resolve_target(request) == TOKEN:
            body = {"access_token": "spy-token-value", "expires_in": 10800, "token_type": "Bearer"}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"accountUid": "spy-account"})


def _ledger(tmp_path: Path, *, mode: Mode = Mode.DRY, running: bool = True) -> Ledger:
    ledger = Ledger.create(
        tmp_path / "ledger.sqlite3", campaign_id="m2-unit-transport", mode=mode, nonce="n"
    )
    ledger.mark_preflight_passed("d" * 64, "a" * 40)
    if running:
        ledger.begin_dry_run()
    return ledger


def _caller(ledger: Ledger, inner: httpx.BaseTransport) -> SmartStoreEndpointCaller:
    gate = BudgetedTransport(ledger, phases=ALL, inner=lambda label: inner)
    return SmartStoreEndpointCaller(transport=gate)


def test_the_caller_request_is_reserved_before_the_transport_receives_it(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.open_phase(Phase.BASELINE_CONNECT)
    spy = Spy(ledger)
    grant = _caller(ledger, spy).call(EndpointId.SMARTSTORE_AUTH_TOKEN, TOKEN_REQUEST)
    assert grant.token_type == "Bearer"
    [sent] = spy.received
    assert resolve_target(sent) == TOKEN
    # When the transport received the request, its budget was already durably spent.
    assert spy.spent_at_send == [{TOKEN: 1, SELLER: 0}]
    [row] = ledger.rows("requests")
    assert (row["label"], row["outcome"], row["http_status"]) == ("T1", "RESPONDED", 200)


def test_an_over_cap_request_never_reaches_the_transport(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for _ in range(CAPS[TOKEN]):
        attempt = ledger.open_phase(Phase.BASELINE_CONNECT)
        ledger.reserve(TOKEN, phases=ALL, pid=1)
        ledger.close_phase(Phase.BASELINE_CONNECT, attempt, "PASS")
    ledger.open_phase(Phase.BASELINE_CONNECT)
    spy = Spy(ledger)
    with pytest.raises(SmartStoreCallError) as caught:
        _caller(ledger, spy).call(EndpointId.SMARTSTORE_AUTH_TOKEN, TOKEN_REQUEST)
    # The caller classifies the refusal for what it is: transmission precluded.
    assert caught.value.phase is Transmission.EGRESS_BLOCKED
    assert caught.value.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert spy.received == []
    assert ledger.counts()[TOKEN] == CAPS[TOKEN]
    assert ledger.campaign().state is State.BUDGET_EXHAUSTED


def test_a_passed_preflight_without_approval_sends_nothing(tmp_path: Path) -> None:
    """Comment 5669037896: preflight PASS without explicit approval means 0 provider calls."""
    ledger = _ledger(tmp_path, mode=Mode.REAL, running=False)
    assert ledger.campaign().state is State.AWAITING_REAL_PROVIDER_APPROVAL
    spy = Spy(ledger)
    caller = _caller(ledger, spy)
    for endpoint, request in (
        (EndpointId.SMARTSTORE_AUTH_TOKEN, TOKEN_REQUEST),
        (EndpointId.SMARTSTORE_SELLER_ACCOUNT, ACCOUNT_REQUEST),
    ):
        with pytest.raises(SmartStoreCallError) as caught:
            caller.call(endpoint, request)
        assert caught.value.remote_outcome is RemoteOutcome.NOT_APPLIED_PROVEN
    assert spy.received == []
    assert ledger.counts() == {TOKEN: 0, SELLER: 0}
    assert ledger.campaign().state is State.AWAITING_REAL_PROVIDER_APPROVAL
    assert {row["reason"] for row in ledger.rows("refusals")} == {"NOT_RUNNING"}


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", TOKEN_URL.replace("https://", "http://")),
        ("POST", TOKEN_URL.replace(HOST, "example.invalid")),
        ("POST", TOKEN_URL.replace(HOST, f"{HOST}:8443")),
        ("POST", TOKEN_URL.replace("https://", "https://user:password@")),
        ("POST", f"{TOKEN_URL}?grant_type=client_credentials"),
        ("GET", TOKEN_URL),
        ("POST", ACCOUNT_URL),
        ("POST", f"{BASE_URL}/v2/products"),
        ("GET", f"{ACCOUNT_URL}/../../oauth2/token"),
    ],
)
def test_a_forbidden_target_never_reaches_a_transport(
    tmp_path: Path, method: str, url: str
) -> None:
    ledger = _ledger(tmp_path)
    ledger.open_phase(Phase.BASELINE_CONNECT)
    spy = Spy(ledger)
    gate = BudgetedTransport(ledger, phases=ALL, inner=lambda label: spy)
    assert resolve_target(httpx.Request(method, url)) == UNRECOGNIZED
    with pytest.raises(BudgetGateRefused) as caught:
        gate.handle_request(httpx.Request(method, url))
    assert caught.value.reason == "FORBIDDEN_TARGET"
    assert spy.received == []
    assert ledger.counts() == {TOKEN: 0, SELLER: 0}
    assert ledger.campaign().state is State.BUDGET_EXHAUSTED


def _wire_path(contract: EndpointContract, value: str = "1234567890") -> str:
    """The endpoint's path with each placeholder filled, as the caller would compose it."""
    path = contract.path
    for name in contract.path_params:
        path = path.replace("{" + name + "}", value)
    return path


def test_the_adopted_registry_endpoints_are_the_only_recognized_targets() -> None:
    for contract in ADOPTED.values():
        request = httpx.Request(contract.method.value, BASE_URL + _wire_path(contract))
        assert resolve_target(request) == contract.endpoint_id.value


@pytest.mark.parametrize("value", ["", "a/b", "../../v1/seller/account", "x" * 65])
def test_a_templated_target_is_recognized_only_for_one_conservative_segment(value: str) -> None:
    # The harness recognizes exactly what the caller may send: one safe path segment, never a
    # wider match that would let an unadopted path be counted as an adopted one.
    contract = ADOPTED[EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2]
    request = httpx.Request(contract.method.value, BASE_URL + _wire_path(contract, value))
    assert resolve_target(request) == UNRECOGNIZED


@pytest.mark.parametrize("endpoint", sorted(NOT_ADOPTED))
def test_an_unadopted_endpoint_fails_in_the_caller_before_the_gate(
    tmp_path: Path, endpoint: EndpointId
) -> None:
    ledger = _ledger(tmp_path)
    ledger.open_phase(Phase.BASELINE_CONNECT)
    spy = Spy(ledger)
    with pytest.raises(SmartStoreCallError) as caught:
        _caller(ledger, spy).call(endpoint, TOKEN_REQUEST)
    assert caught.value.phase is Transmission.LOCAL_PREFLIGHT
    assert spy.received == []
    assert ledger.rows("requests") == [] and ledger.rows("refusals") == []


def test_a_request_without_a_response_stays_spent(tmp_path: Path) -> None:
    class Down(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

    ledger = _ledger(tmp_path)
    ledger.open_phase(Phase.BASELINE_CONNECT)
    with pytest.raises(SmartStoreCallError):
        _caller(ledger, Down()).call(EndpointId.SMARTSTORE_AUTH_TOKEN, TOKEN_REQUEST)
    [row] = ledger.rows("requests")
    assert (row["outcome"], row["http_status"]) == ("NO_RESPONSE", None)
    assert ledger.counts()[TOKEN] == 1


def test_the_live_transport_never_exists_under_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ci_or_test({}) == "pytest"
    with pytest.raises(LiveProviderRefused):
        live_inner()
    # Even a factory obtained outside a test run refuses at the moment of use.
    monkeypatch.setattr(transport, "ci_or_test", lambda environ=None: None)
    build = live_inner()
    monkeypatch.undo()
    with pytest.raises(LiveProviderRefused):
        build("T1")


@pytest.mark.parametrize("marker", CI_MARKERS)
def test_every_ci_marker_blocks_the_live_transport(marker: str) -> None:
    assert ci_or_test({marker: "true"}) == marker
    with pytest.raises(LiveProviderRefused):
        live_inner({marker: "true"})


def test_the_live_inner_transport_is_the_one_the_caller_would_build_itself() -> None:
    """``live_inner`` builds ``httpx.HTTPTransport(trust_env=False)`` (pinned statically in
    test_m2_harness_static.py): the same pool httpx.Client(trust_env=False) builds when the
    caller passes no transport. Nothing is connected here."""
    client = httpx.Client(trust_env=False)
    ours = httpx.HTTPTransport(trust_env=False)
    default = client._transport
    assert isinstance(default, httpx.HTTPTransport)
    try:
        for name in (
            "_max_connections",
            "_max_keepalive_connections",
            "_keepalive_expiry",
            "_http1",
            "_http2",
            "_retries",
        ):
            assert getattr(ours._pool, name) == getattr(default._pool, name), name
        ours_tls, default_tls = ours._pool._ssl_context, default._pool._ssl_context
        assert (ours_tls.verify_mode, ours_tls.check_hostname) == (
            default_tls.verify_mode,
            default_tls.check_hostname,
        )
        assert ours._pool._retries == 0  # no automatic retries
    finally:
        ours.close()
        client.close()
