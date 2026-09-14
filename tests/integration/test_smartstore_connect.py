"""SmartStore CONNECT over a fake provider (M2 PR-A; ENDPOINT_MATRIX §5, §8, §14; AUTH §14,
§17, §18; ACCOUNT_IDENTITY §4, §5, §9).

The provider is an ``httpx.MockTransport`` behind the real registry-gated caller; no socket is
opened and no SmartStore account is involved.
"""

import logging
import sqlite3
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from app.audit.models import AuditEventType
from app.audit.service import AuditEntry, AuditLog
from app.config import AppConfig
from app.connect.marketplace.attestation import ApplicationIdentity, Invalidation
from app.connect.marketplace.capability import (
    AuthStatus,
    ContractFreshness,
    WorkflowScope,
    WorkflowState,
)
from app.connect.marketplace.service import MarketplaceCapabilityService
from app.connect.sessions import MARKETPLACE_SESSIONS_DIR_NAME, SupplierSessionStore
from app.connect.smartstore.service import KEY, CommittedSession
from app.container import Container, build_container
from app.core.egress import EGRESS
from app.core.errors import ErrorClass, InputValidationError, PolicyBlockedError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from integrations.marketplaces.smartstore.caller import (
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.signing import client_secret_sign
from tests.support import FakeClock

pytestmark = pytest.mark.integration

# Fixture values; none is a real credential or account.
SECRET = "$2a$04$abcdefghijklmnopqrstuu"
OTHER_SECRET = "$2a$04$zyxwvutsrqponmlkjihgfe"
CLIENT_ID = "fixture-client-id-a1b2"
OTHER_CLIENT_ID = "fixture-client-id-c3d4"
UID_A = "uid-fixture-A"
UID_B = "uid-fixture-B"
ACTOR = "operator:test"
# The renewal margin the test configuration sets (AUTH §15: configured policy, no code default).
MARGIN_S = 600
TOKEN_PATH = "/external/v1/oauth2/token"
ACCOUNT_PATH = "/external/v1/seller/account"
CONNECTION = (
    "SELECT credential_generation_hwm, session_generation_hwm, provider_account_uid,"
    " provider_account_id, bound_credential_generation, bound_session_generation, bound_by"
    " FROM marketplace_connections"
)
UNBOUND = (None, None, None, None, None)


class Provider:
    """A fake SmartStore. Tokens are numbered, and the account it reports can be changed. At
    every account read it records the bearer it received and the session committed on disk."""

    def __init__(self, sessions: SupplierSessionStore) -> None:
        self._sessions = sessions
        self.account_uid = UID_A
        self.account_response: Callable[[], httpx.Response] | None = None
        self.token_response: Callable[[], httpx.Response] | None = None
        self.calls: list[str] = []
        self.issued: list[str] = []
        self.forms: list[dict[str, str]] = []
        self.reads: list[tuple[str, CommittedSession | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            self.calls.append("TOKEN")
            self.forms.append(dict(urllib.parse.parse_qsl(request.content.decode())))
            if self.token_response is not None:
                return self.token_response()
            token = f"fixture-token-{len(self.issued) + 1}"
            self.issued.append(token)
            body = {"access_token": token, "expires_in": 10800, "token_type": "Bearer"}
            return httpx.Response(200, json=body)
        assert request.url.path == ACCOUNT_PATH
        self.calls.append("ACCOUNT")
        payload = self._sessions.load(KEY)
        committed = CommittedSession.decode(payload) if payload is not None else None
        self.reads.append((request.headers["authorization"], committed))
        if self.account_response is not None:
            return self.account_response()
        body = {"accountUid": self.account_uid, "accountId": f"id-{self.account_uid}"}
        return httpx.Response(200, json=body)


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
def provider(config: AppConfig, secrets: MemorySecretStore) -> Provider:
    directory = config.data_dir / MARKETPLACE_SESSIONS_DIR_NAME
    return Provider(SupplierSessionStore(directory, secrets, namespace="marketplace"))


@contextmanager
def _process(
    config: AppConfig,
    clock: FakeClock,
    secrets: MemorySecretStore,
    provider: Provider,
    *,
    margin_s: int | None = MARGIN_S,
) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config.with_overrides(smartstore_renewal_margin_s=margin_s),
            ownership=lease,
            clock=clock,
            secret_store=secrets,
            smartstore_caller=SmartStoreEndpointCaller(transport=httpx.MockTransport(provider)),
        )
        try:
            yield built
        finally:
            built.db.dispose()


@pytest.fixture
def p(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore, provider: Provider
) -> Iterator[Container]:
    with _process(config, clock, secrets, provider) as built:
        yield built


def _current(p: Container) -> None:
    p.marketplace_capability.record_contract_freshness(KEY, ContractFreshness.CURRENT, actor=ACTOR)


def _connection(config: AppConfig) -> tuple[object, ...]:
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        rows = raw.execute(CONNECTION).fetchall()
    assert len(rows) == 1
    return tuple(rows[0])


def _bound_audits(config: AppConfig) -> int:
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        (count,) = raw.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'MARKETPLACE_ACCOUNT_BOUND'"
        ).fetchone()
    return int(count)


def _session_file(config: AppConfig) -> Path:
    return config.data_dir / MARKETPLACE_SESSIONS_DIR_NAME / f"{KEY}.enc"


def _overlays(p: Container) -> list[tuple[WorkflowState, WorkflowScope, object]]:
    view = p.marketplace_capability.capability(KEY)
    return [(o.workflow_state, o.workflow_scope, o.reason_code) for o in view.workflow]


REVIEW = [(WorkflowState.REVIEW_REQUIRED, WorkflowScope.AUTHENTICATION, None)]


# ---------------------------------------------------------------- the M2 graph


def test_connect_commits_the_session_before_the_account_read_and_uses_its_bearer(
    p: Container, provider: Provider, config: AppConfig, clock: FakeClock
) -> None:
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    result = p.smartstore.connect()
    assert provider.calls == ["TOKEN", "ACCOUNT"]
    ((header, committed),) = provider.reads
    # EM §7, §14.6: the bearer is the one durably committed before the read, generation 1.
    assert committed is not None
    assert (committed.credential_generation, committed.session_generation) == (1, 1)
    assert header == f"Bearer {committed.access_token}" == "Bearer fixture-token-1"
    assert (result.credential_generation, result.session_generation) == (1, 1)
    timestamp_ms = int(clock.now().timestamp() * 1000)
    assert provider.forms == [
        {
            "client_id": CLIENT_ID,
            "timestamp": str(timestamp_ms),
            "client_secret_sign": client_secret_sign(CLIENT_ID, SECRET, timestamp_ms),
            "grant_type": "client_credentials",
            "type": "SELF",
        }
    ]


def test_a_successful_read_never_binds_the_account(
    p: Container, provider: Provider, config: AppConfig
) -> None:
    _current(p)
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    result = p.smartstore.connect()
    assert (result.bound, result.observed_account_uid) == (False, UID_A)
    assert result.capability.auth is AuthStatus.NOT_BOUND
    assert _connection(config)[2:] == UNBOUND


def test_the_first_binding_is_explicit_fresh_and_atomic(
    p: Container, provider: Provider, config: AppConfig
) -> None:
    _current(p)
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    p.smartstore.connect()
    reads = len(provider.reads)
    result = p.smartstore.bind_account(UID_A, actor=ACTOR)
    # ACCOUNT_IDENTITY §5: a fresh authenticated read under the committed session.
    assert len(provider.reads) == reads + 1
    assert provider.calls.count("TOKEN") == 1
    assert result.bound and result.capability.auth is AuthStatus.READY
    assert _connection(config) == (1, 1, UID_A, f"id-{UID_A}", 1, 1, ACTOR)
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        ((actor, details),) = raw.execute(
            "SELECT actor, details_json FROM audit_events"
            " WHERE event_type = 'MARKETPLACE_ACCOUNT_BOUND'"
        ).fetchall()
    assert actor == ACTOR
    assert '"session_generation": 1' in details and UID_A not in details


def test_a_first_binding_waits_for_a_current_contract_and_binds_nothing(
    p: Container, provider: Provider, config: AppConfig
) -> None:
    # Blocker 2 (review 5200019078): refused before any provider call, nothing bound, and a
    # retry after the contract is CURRENT binds normally (it is not ALREADY_BOUND).
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    with pytest.raises(PolicyBlockedError) as caught:
        p.smartstore.bind_account(UID_A, actor=ACTOR)
    assert caught.value.code == "MARKETPLACE_CONTRACT_NOT_CURRENT"
    assert provider.calls == []
    assert _connection(config)[2:] == UNBOUND
    assert _bound_audits(config) == 0
    assert p.marketplace_capability.capability(KEY).auth is AuthStatus.NOT_BOUND
    _current(p)
    assert p.smartstore.bind_account(UID_A, actor=ACTOR).capability.auth is AuthStatus.READY
    assert _connection(config)[2] == UID_A


def test_a_contract_change_during_the_bind_refuses_it_and_binds_nothing(
    p: Container, provider: Provider, config: AppConfig
) -> None:
    # The early check passes; then the contract goes STALE while the fresh read is in flight.
    # The decision taken inside the commit transaction refuses, and nothing is left bound.
    _current(p)
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)

    def stale_during_the_read() -> httpx.Response:
        p.marketplace_capability.record_contract_freshness(
            KEY, ContractFreshness.STALE, actor=ACTOR
        )
        return httpx.Response(200, json={"accountUid": UID_A, "accountId": f"id-{UID_A}"})

    provider.account_response = stale_during_the_read
    with pytest.raises(PolicyBlockedError) as caught:
        p.smartstore.bind_account(UID_A, actor=ACTOR)
    assert caught.value.code == "MARKETPLACE_CONTRACT_NOT_CURRENT"
    assert provider.calls == ["TOKEN", "ACCOUNT"]
    assert _connection(config)[2:] == UNBOUND
    assert _bound_audits(config) == 0
    view = p.marketplace_capability.capability(KEY)
    assert (view.auth, view.auth_verified_at) == (AuthStatus.NOT_BOUND, None)


@pytest.mark.parametrize("crash", ["binding-audit", "capability-save"])
def test_a_failure_inside_the_commit_boundary_leaves_nothing_bound(
    p: Container,
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    crash: str,
) -> None:
    # The gate has accepted and the binding row is written in the transaction, then the commit
    # boundary fails: the caller sees the failure and nothing is bound or promoted.
    _current(p)
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    if crash == "binding-audit":
        append = AuditLog.append

        def failing_append(self: AuditLog, entry: AuditEntry, **kwargs: object) -> object:
            if entry.event_type is AuditEventType.MARKETPLACE_ACCOUNT_BOUND:
                raise RuntimeError("failed while committing the binding")
            return append(self, entry, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(AuditLog, "append", failing_append)
    else:

        def failing_save(self: MarketplaceCapabilityService, *args: object) -> object:
            raise RuntimeError("failed after the binding row was written")

        monkeypatch.setattr(MarketplaceCapabilityService, "_save", failing_save)
    with pytest.raises(RuntimeError):
        p.smartstore.bind_account(UID_A, actor=ACTOR)
    monkeypatch.undo()
    assert _connection(config)[2:] == UNBOUND
    assert _bound_audits(config) == 0
    view = p.marketplace_capability.capability(KEY)
    assert (view.auth, view.auth_verified_at) == (AuthStatus.NOT_BOUND, None)
    assert p.smartstore.bind_account(UID_A, actor=ACTOR).capability.auth is AuthStatus.READY


def test_an_unconfirmed_binding_binds_nothing(p: Container, config: AppConfig) -> None:
    _current(p)
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    with pytest.raises(InputValidationError) as caught:
        p.smartstore.bind_account(UID_B, actor=ACTOR)
    assert caught.value.code == "SMARTSTORE_BINDING_NOT_CONFIRMED"
    assert _connection(config)[2:] == UNBOUND
    assert p.marketplace_capability.capability(KEY).auth is AuthStatus.NOT_BOUND


_AT = "'2026-09-15 00:00:00'"


@pytest.mark.parametrize(
    "binding",
    [
        "'uid-x', NULL, NULL, NULL, NULL, NULL",
        "'uid-x', NULL, 1, 1, NULL, 'op'",
        f"NULL, NULL, 1, 1, {_AT}, 'op'",
        f"'', NULL, 1, 1, {_AT}, 'op'",
        f"'uid-x', NULL, 0, 1, {_AT}, 'op'",
        f"'uid-x', NULL, 1, NULL, {_AT}, 'op'",
        f"'uid-x', NULL, 1, 1, {_AT}, ''",
        "NULL, 'id-x', NULL, NULL, NULL, NULL",
    ],
)
def test_a_partial_binding_can_never_be_stored(data_dir: Path, binding: str) -> None:
    # ACCOUNT_IDENTITY §5: binding_commit_not_proven -> NOT_BOUND, by construction.
    row = f"('smartstore', 1, 1, {binding}, {_AT}, {_AT})"
    with sqlite3.connect(data_dir / "icbm.db") as raw, pytest.raises(sqlite3.IntegrityError):
        raw.execute(f"INSERT INTO marketplace_connections VALUES {row}")


def test_a_complete_binding_or_none_is_stored(data_dir: Path) -> None:
    with sqlite3.connect(data_dir / "icbm.db") as raw:
        raw.execute(
            "INSERT INTO marketplace_connections VALUES"
            f" ('smartstore', 1, 1, 'uid-x', NULL, 1, 1, {_AT}, 'op', {_AT}, {_AT})"
        )
        raw.execute(
            "INSERT INTO marketplace_connections VALUES"
            f" ('other', 0, 0, NULL, NULL, NULL, NULL, NULL, NULL, {_AT}, {_AT})"
        )


def test_a_different_account_is_auth_mismatch_and_never_rebinds(
    p: Container, provider: Provider, config: AppConfig
) -> None:
    _current(p)
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    p.smartstore.bind_account(UID_A, actor=ACTOR)
    provider.account_uid = UID_B
    result = p.smartstore.connect()
    assert result.capability.auth is AuthStatus.AUTH_MISMATCH
    assert _overlays(p) == REVIEW
    assert _connection(config)[2] == UID_A
    with pytest.raises(PolicyBlockedError) as caught:
        p.smartstore.bind_account(UID_B, actor=ACTOR)
    assert caught.value.code == "SMARTSTORE_ACCOUNT_ALREADY_BOUND"
    assert _connection(config)[2] == UID_A


# ---------------------------------------------------------------- session boundary


def test_a_malformed_token_response_commits_no_session(
    p: Container, provider: Provider, config: AppConfig
) -> None:
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    body = {"access_token": "fixture-token", "expires_in": 0, "token_type": "Bearer"}
    provider.token_response = lambda: httpx.Response(200, json=body)
    with pytest.raises(SmartStoreCallError) as caught:
        p.smartstore.connect()
    assert caught.value.code == "SMARTSTORE_SUCCESS_PREDICATE_FAILED"
    assert provider.calls == ["TOKEN"]
    assert not _session_file(config).exists()
    assert _connection(config)[1] == 0  # EM §6: session_generation never advanced


def test_a_failed_session_commit_never_reaches_the_account_read(
    p: Container, provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)

    def fail(self: SupplierSessionStore, key: str, payload: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(SupplierSessionStore, "save", fail)
    with pytest.raises(OSError):
        p.smartstore.connect()
    assert provider.calls == ["TOKEN"]


def test_a_committed_session_is_reused_until_the_renewal_margin(
    p: Container, provider: Provider, clock: FakeClock
) -> None:
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    p.smartstore.connect()
    clock.advance(10800 - MARGIN_S - 1)
    p.smartstore.connect()
    assert provider.calls.count("TOKEN") == 1
    clock.advance(1)
    result = p.smartstore.connect()
    assert provider.calls.count("TOKEN") == 2
    assert result.session_generation == 2
    header, committed = provider.reads[-1]
    assert committed is not None and committed.session_generation == 2
    assert header == "Bearer fixture-token-2"


def test_the_configured_renewal_margin_decides_session_reuse(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore, provider: Provider
) -> None:
    # Blocker 1 (review 5200019078): production wiring takes the margin from configuration.
    # With 300 s configured, a session is still reused 10 minutes before expiry.
    with _process(config, clock, secrets, provider, margin_s=300) as p:
        p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
        p.smartstore.connect()
        clock.advance(10800 - 300 - 1)
        p.smartstore.connect()
        assert provider.calls.count("TOKEN") == 1
        clock.advance(1)
        p.smartstore.connect()
        assert provider.calls.count("TOKEN") == 2


def test_without_a_configured_renewal_margin_connect_refuses_before_any_provider_call(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore, provider: Provider
) -> None:
    with _process(config, clock, secrets, provider, margin_s=None) as p:
        _current(p)
        p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
        for action in (p.smartstore.connect, lambda: p.smartstore.bind_account(UID_A, actor=ACTOR)):
            with pytest.raises(PolicyBlockedError) as caught:
                action()
            assert caught.value.code == "SMARTSTORE_RENEWAL_POLICY_NOT_CONFIGURED"
    assert provider.calls == []


def test_restart_reuses_the_committed_session_but_never_the_persisted_ready(
    config: AppConfig, clock: FakeClock, secrets: MemorySecretStore, provider: Provider
) -> None:
    with _process(config, clock, secrets, provider) as first:
        _current(first)
        first.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
        first.smartstore.bind_account(UID_A, actor=ACTOR)
    with _process(config, clock, secrets, provider) as second:
        second.marketplace_capability.normalize_on_startup()
        assert second.marketplace_capability.capability(KEY).auth is AuthStatus.NOT_READY
        calls = list(provider.calls)
        result = second.smartstore.connect()
    # AUTH §17 Case A: the committed token is reused, and READY needs a fresh read.
    assert provider.calls[len(calls) :] == ["ACCOUNT"]
    assert result.capability.auth is AuthStatus.READY


def test_credential_rotation_starts_a_new_generation_and_ends_the_old_session(
    p: Container, provider: Provider, config: AppConfig, secrets: MemorySecretStore
) -> None:
    _current(p)
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    p.smartstore.bind_account(UID_A, actor=ACTOR)
    assert p.smartstore.save_credentials(OTHER_CLIENT_ID, OTHER_SECRET, actor=ACTOR) == 2
    assert not _session_file(config).exists()
    assert p.marketplace_capability.capability(KEY).auth is AuthStatus.NOT_READY
    # A session committed under the old generation is never current (AUTH §18, §19).
    stale = provider.reads[-1][1]
    assert stale is not None and stale.credential_generation == 1
    directory = config.data_dir / MARKETPLACE_SESSIONS_DIR_NAME
    SupplierSessionStore(directory, secrets, namespace="marketplace").save(KEY, stale.encode())
    result = p.smartstore.connect()
    assert provider.forms[-1]["client_id"] == OTHER_CLIENT_ID
    assert (result.credential_generation, result.session_generation) == (2, 2)
    assert result.capability.auth is AuthStatus.READY


@pytest.mark.parametrize(
    ("status", "body", "error_class", "overlays"),
    [
        (404, {"code": "STORE_NOT_FOUND"}, ErrorClass.UNKNOWN, REVIEW),
        (401, {"code": "GW.AUTHN"}, ErrorClass.UNKNOWN, REVIEW),
        (500, {"code": "GW.INTERNAL_SERVER_ERROR"}, ErrorClass.TRANSIENT, []),
        (429, {"code": "GW.RATE_LIMIT"}, ErrorClass.RATE_LIMITED, []),
    ],
)
def test_account_failures_are_classified_once_without_retry_or_reissue(
    p: Container,
    provider: Provider,
    status: int,
    body: dict[str, str],
    error_class: ErrorClass,
    overlays: list[object],
) -> None:
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    provider.account_response = lambda: httpx.Response(status, json=body)
    with pytest.raises(SmartStoreCallError) as caught:
        p.smartstore.connect()
    assert caught.value.error_class is error_class is not ErrorClass.NOT_FOUND
    assert provider.calls == ["TOKEN", "ACCOUNT"]
    view = p.marketplace_capability.capability(KEY)
    assert (view.error_class, view.remote_outcome) == (error_class, None)
    assert _overlays(p) == overlays


# ---------------------------------------------------------------- wiring and safety


def test_the_production_wiring_supplies_the_identity_and_the_revision(p: Container) -> None:
    assert p.smartstore.current_identity() is None
    refused = p.permission_attestation.attestation(KEY)
    assert refused.recording_refusal is Invalidation.APPLICATION_NOT_CONFIGURED
    p.smartstore.save_credentials(f"  {CLIENT_ID} ", SECRET, actor=ACTOR)
    assert p.smartstore.current_identity() == ApplicationIdentity("SMARTSTORE", "SELF", CLIENT_ID)
    available = p.permission_attestation.attestation(KEY)
    assert (available.recording_available, available.recording_refusal) == (True, None)
    assert p.permission_attestation.context(KEY).endpoint_mapping_revision == "m2-connect-r1"


@pytest.mark.parametrize(
    ("client_id", "secret", "code"),
    [
        ("", SECRET, "SMARTSTORE_CREDENTIALS_INCOMPLETE"),
        (CLIENT_ID, "  ", "SMARTSTORE_CREDENTIALS_INCOMPLETE"),
        (CLIENT_ID, "not-a-bcrypt-salt", "SMARTSTORE_CLIENT_SECRET_UNUSABLE"),
    ],
)
def test_an_unusable_credential_bundle_is_never_committed(
    p: Container, client_id: str, secret: str, code: str
) -> None:
    with pytest.raises(InputValidationError) as caught:
        p.smartstore.save_credentials(client_id, secret, actor=ACTOR)
    assert caught.value.code == code
    assert p.smartstore.current_identity() is None


def test_connect_persists_logs_and_audits_no_secret_token_or_signature(
    p: Container,
    provider: Provider,
    config: AppConfig,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _current(p)
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    p.smartstore.bind_account(UID_A, actor=ACTOR)
    p.smartstore.connect()
    timestamp_ms = int(clock.now().timestamp() * 1000)
    secrets = [SECRET, client_secret_sign(CLIENT_ID, SECRET, timestamp_ms), CLIENT_ID]
    secrets += provider.issued
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        tables = [
            name for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        database = "\n".join(
            str(raw.execute(f"SELECT * FROM {table}").fetchall()) for table in tables
        )
        audit = str(raw.execute("SELECT * FROM audit_events").fetchall())
    logs = caplog.text + "\n".join(
        str(value) for record in caplog.records for value in vars(record).values()
    )
    session_file = _session_file(config).read_bytes()
    for secret in secrets:
        assert secret not in database, secret
        assert secret not in logs, secret
        assert secret.encode() not in session_file, secret
    for identity in (UID_A, f"id-{UID_A}"):  # the provider identity is stored, never logged
        assert identity not in logs and identity not in audit


def test_smartstore_connect_makes_no_external_attempt(p: Container) -> None:
    EGRESS.install()
    before = EGRESS.snapshot()
    _current(p)
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    p.smartstore.bind_account(UID_A, actor=ACTOR)
    p.smartstore.connect()
    after = EGRESS.snapshot()
    assert after["external_attempts"] == before["external_attempts"]
    assert after["granted_events"] == before["granted_events"]
