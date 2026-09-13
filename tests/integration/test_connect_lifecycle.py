"""Core CONNECT lifecycle against a fake supplier: no network, no browser, exact login counts."""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit.models import AuditEventType
from app.config import AppConfig
from app.connect.contracts import SupplierConnectionSummary
from app.connect.proof import ProtectedReadProof
from app.connect.sessions import SESSIONS_DIR_NAME
from app.connect.state import CapabilityStatus, ConnectionState
from app.container import Container, build_container
from app.core.errors import AuthError, PolicyBlockedError, TransientError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.system.secret_scan import scan
from integrations.suppliers.base import RequestKind
from tests.suppliers import (
    FAKE_KEY,
    PASSWORD,
    SESSION_TOKEN,
    USERNAME,
    FakeGateway,
    fake_definition,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration

S = ConnectionState
E = AuditEventType
OPERATOR = "operator:local"


@contextmanager
def _process(
    config: AppConfig, gateway: FakeGateway, secrets: MemorySecretStore, *, limit: int = 3
) -> Iterator[Container]:
    """One ICBM process on ``config.data_dir``; ``secrets`` plays the OS store across restarts."""
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=FakeClock(),
            secret_store=secrets,
            supplier_gateway=gateway,
            suppliers=(fake_definition(auth_retry_limit=limit),),
        )
        try:
            built.connect.normalize_on_startup()
            yield built
        finally:
            built.db.dispose()


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
def app(config: AppConfig, gateway: FakeGateway, secrets: MemorySecretStore) -> Iterator[Container]:
    with _process(config, gateway, secrets) as built:
        yield built


def _save(app: Container, password: str = PASSWORD) -> None:
    app.connect.save_credentials(FAKE_KEY, username=USERNAME, password=password, actor=OPERATOR)


def _verify(app: Container, *, allow_login: bool = True) -> ProtectedReadProof:
    return app.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=allow_login)


def _summary(app: Container) -> SupplierConnectionSummary:
    return app.connect.supplier_connection(FAKE_KEY)


def _events(app: Container) -> list[str]:
    return [e.event_type for e in reversed(app.audit.list_events(limit=500))]


def test_a_first_connection_logs_in_once_and_is_proven_by_both_reads(
    app: Container, gateway: FakeGateway, config: AppConfig
) -> None:
    _save(app)
    proof = _verify(app)
    assert proof.proven
    assert gateway.logins == 1
    assert gateway.requests == [RequestKind.CONTROL_READ, RequestKind.PROTECTED_READ]
    summary = _summary(app)
    assert (summary.state, summary.capability_status, summary.session_state) == (
        S.READY,
        CapabilityStatus.READY,
        "VERIFIED",
    )
    assert (summary.real_login_attempts, summary.reauth_count, summary.session_reuse_count) == (
        1,
        0,
        0,
    )
    assert summary.last_verified_at is not None
    assert _events(app) == [
        E.SUPPLIER_CREDENTIALS_UPDATED,
        E.SUPPLIER_AUTH_SUCCEEDED,
        E.SUPPLIER_CONNECTION_VERIFIED,
    ]
    verified = app.audit.list_events(limit=1)[0]
    assert verified.details["control_result"] == "LOGIN_REQUIRED"
    assert verified.details["authenticated_result"] == "AUTHENTICATED"
    assert verified.details["target"] == "/member"
    blob = (config.data_dir / SESSIONS_DIR_NAME / f"{FAKE_KEY}.enc").read_bytes()
    assert SESSION_TOKEN.encode() not in blob


def test_a_valid_session_is_reused_without_a_login(app: Container, gateway: FakeGateway) -> None:
    _save(app)
    _verify(app)
    assert _verify(app).proven
    assert gateway.logins == 1
    summary = _summary(app)
    assert (summary.state, summary.session_reuse_count) == (S.READY, 1)
    assert _events(app)[-2:] == [E.SUPPLIER_SESSION_REUSED, E.SUPPLIER_CONNECTION_VERIFIED]


def test_an_expired_session_is_refreshed_by_one_bounded_reauthentication(
    app: Container, gateway: FakeGateway
) -> None:
    _save(app)
    _verify(app)
    gateway.expire_sessions()
    assert _verify(app).proven
    assert gateway.logins == 2
    summary = _summary(app)
    assert (summary.state, summary.reauth_count, summary.real_login_attempts) == (S.READY, 1, 2)
    assert _events(app)[-3:] == [
        E.SUPPLIER_AUTH_SUCCEEDED,
        E.SUPPLIER_SESSION_REFRESHED,
        E.SUPPLIER_CONNECTION_VERIFIED,
    ]


@pytest.mark.parametrize("limit", [1, 2, 3, 5])
def test_rejected_logins_pause_at_exactly_the_auth_retry_limit(
    config: AppConfig, gateway: FakeGateway, secrets: MemorySecretStore, limit: int
) -> None:
    with _process(config, gateway, secrets, limit=limit) as app:
        _save(app, password="wrong-password")
        for attempt in range(1, limit + 1):
            with pytest.raises(AuthError) as caught:
                _verify(app)
            summary = _summary(app)
            assert summary.consecutive_auth_failures == attempt
            if attempt < limit:
                assert caught.value.code == "SUPPLIER_LOGIN_REJECTED"
                assert summary.state is S.DISCONNECTED
        assert caught.value.code == "SUPPLIER_AUTH_PAUSED"
        assert (_summary(app).state, _summary(app).capability_status) == (
            S.PAUSED,
            CapabilityStatus.PAUSED,
        )
        assert gateway.logins == limit
        # Paused: no further automatic or manual authentication reaches the supplier.
        with pytest.raises(PolicyBlockedError):
            _verify(app)
        with pytest.raises(PolicyBlockedError):
            app.connect.request_connection_test(FAKE_KEY, actor=OPERATOR)
        assert gateway.logins == limit
        assert gateway.requests.count(RequestKind.AUTHENTICATE) == 0
        denied = app.audit.list_events(limit=1)[0]
        assert (denied.event_type, denied.outcome) == (
            E.SUPPLIER_CONNECTION_TEST_REQUESTED,
            "DENIED",
        )


def test_resume_is_an_audited_operator_action_and_restores_login(
    config: AppConfig, gateway: FakeGateway, secrets: MemorySecretStore
) -> None:
    with _process(config, gateway, secrets, limit=2) as app:
        _save(app, password="wrong-password")
        for _ in range(2):
            with pytest.raises(AuthError):
                _verify(app)
        assert _summary(app).state is S.PAUSED
        # Saving the right login does not by itself lift the pause.
        _save(app)
        assert _summary(app).state is S.PAUSED
        resumed = app.connect.resume(FAKE_KEY, actor=OPERATOR)
        assert (resumed.state, resumed.consecutive_auth_failures) == (S.DISCONNECTED, 0)
        event = app.audit.list_events(limit=1)[0]
        assert (event.event_type, event.actor, event.outcome) == (
            E.SUPPLIER_AUTH_RESUMED,
            OPERATOR,
            "ALLOWED",
        )
        assert event.details == event.details | {
            "supplier_key": FAKE_KEY,
            "prior_state": "PAUSED",
            "consecutive_auth_failures": 2,
            "auth_retry_limit": 2,
        }
        assert "resumed_at" in event.details
        assert _verify(app).proven
        assert gateway.logins == 3


def test_a_login_that_does_not_authenticate_counts_as_a_failure(
    app: Container, gateway: FakeGateway
) -> None:
    _save(app)
    gateway.login_effective = False
    with pytest.raises(AuthError, match="did not accept"):
        _verify(app)
    summary = _summary(app)
    assert (summary.state, summary.consecutive_auth_failures, summary.last_error_code) == (
        S.DISCONNECTED,
        1,
        "SUPPLIER_LOGIN_NOT_EFFECTIVE",
    )


def test_a_target_that_is_not_authentication_gated_can_never_prove_readiness(
    app: Container, gateway: FakeGateway, config: AppConfig
) -> None:
    _save(app)
    gateway.target_is_public = True
    with pytest.raises(PolicyBlockedError) as caught:
        _verify(app)
    assert caught.value.code == "SUPPLIER_TARGET_NOT_AUTH_GATED"
    assert _summary(app).state is S.DEGRADED
    assert not (config.data_dir / SESSIONS_DIR_NAME / f"{FAKE_KEY}.enc").exists()
    assert gateway.count(RequestKind.PROTECTED_READ) == 0, "no authenticated read without a gate"


def test_an_unrecognized_protected_read_is_never_ready(
    app: Container, gateway: FakeGateway
) -> None:
    _save(app)
    gateway.unrecognized = True
    with pytest.raises(TransientError, match="neither"):
        _verify(app)
    assert _summary(app).state is S.DEGRADED
    assert _summary(app).consecutive_auth_failures == 0


def test_a_transient_login_failure_is_not_an_auth_failure(
    app: Container, gateway: FakeGateway
) -> None:
    _save(app)
    gateway.login_error = TransientError("SUPPLIER_TIMEOUT", "slow")
    with pytest.raises(TransientError):
        _verify(app)
    summary = _summary(app)
    assert (summary.state, summary.consecutive_auth_failures) == (S.DEGRADED, 0)
    gateway.login_error = None
    assert _verify(app).proven


def test_concurrent_callers_share_one_real_login(app: Container, gateway: FakeGateway) -> None:
    _save(app)
    gateway.login_gate = threading.Event()
    proofs: list[ProtectedReadProof] = []
    errors: list[BaseException] = []

    def call() -> None:
        try:
            proofs.append(_verify(app))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while gateway.logins == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    gateway.login_gate.set()
    for thread in threads:
        thread.join(10)
    assert not errors
    assert len(proofs) == 4 and all(p.proven for p in proofs)
    assert gateway.logins == 1


def test_a_restart_holds_no_proof_but_reuses_the_encrypted_session(
    config: AppConfig, gateway: FakeGateway, secrets: MemorySecretStore
) -> None:
    with _process(config, gateway, secrets) as first:
        _save(first)
        _verify(first)
    requests_before = len(gateway.requests)
    with _process(config, gateway, secrets) as second:
        summary = _summary(second)
        assert (summary.state, summary.session_state) == (S.DISCONNECTED, "STORED")
        assert len(gateway.requests) == requests_before, "startup makes no supplier request"
        assert _verify(second).proven
        assert gateway.logins == 1
        assert _summary(second).session_reuse_count == 1


def test_a_corrupt_session_is_discarded_and_replaced_by_a_fresh_login(
    app: Container, gateway: FakeGateway, config: AppConfig
) -> None:
    _save(app)
    _verify(app)
    path = config.data_dir / SESSIONS_DIR_NAME / f"{FAKE_KEY}.enc"
    path.write_bytes(b"ICBMSESS1" + b"\0" * 40)
    assert _verify(app).proven
    assert gateway.logins == 2
    assert path.read_bytes() != b"ICBMSESS1" + b"\0" * 40


def test_auto_connect_off_never_logs_in_automatically(app: Container, gateway: FakeGateway) -> None:
    _save(app)
    app.connect.set_auto_connect(FAKE_KEY, enabled=False, actor=OPERATOR)
    with pytest.raises(AuthError) as caught:
        app.connect.ensure_connected(FAKE_KEY)
    assert caught.value.code == "SUPPLIER_AUTO_CONNECT_DISABLED"
    assert gateway.logins == 0
    assert _verify(app).proven, "the manual connection test stays available"
    assert gateway.logins == 1


def test_automatic_connection_reuses_the_session_first(
    app: Container, gateway: FakeGateway
) -> None:
    _save(app)
    _verify(app)
    assert app.connect.ensure_connected(FAKE_KEY).proven
    assert gateway.logins == 1


def test_a_fresh_data_directory_adopts_credentials_already_in_the_os_store(
    app: Container, gateway: FakeGateway, secrets: MemorySecretStore
) -> None:
    secrets.set(f"supplier:{FAKE_KEY}:username", USERNAME)
    secrets.set(f"supplier:{FAKE_KEY}:password", PASSWORD)
    assert _summary(app).capability_status is CapabilityStatus.DISCONNECTED
    app.connect.request_connection_test(FAKE_KEY, actor=OPERATOR)
    assert _summary(app).connection_id is not None
    assert _verify(app).proven


def test_connecting_without_credentials_is_refused(app: Container, gateway: FakeGateway) -> None:
    with pytest.raises(Exception, match="save the supplier login"):
        _verify(app)
    assert gateway.requests == []


def test_ready_without_a_recorded_proof_is_not_representable(app: Container) -> None:
    _save(app)
    with pytest.raises(IntegrityError), app.db.write() as session:
        session.execute(text("UPDATE supplier_connections SET state = 'READY'"))


def test_no_secret_reaches_the_database_the_session_file_or_the_logs(
    app: Container,
    gateway: FakeGateway,
    config: AppConfig,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    with caplog.at_level("DEBUG"):
        _save(app)
        _verify(app)
        gateway.expire_sessions()
        _verify(app)
        _save(app, password="wrong-password")
        with pytest.raises(AuthError):
            _verify(app)
    log_dump = tmp_path / "captured.log"
    log_dump.write_text(
        "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records), "utf-8"
    )
    report = scan(
        [config.data_dir, log_dump],
        {
            "password": PASSWORD,
            "wrong_password": "wrong-password",
            "username": USERNAME,
            "session_token": SESSION_TOKEN,
        },
    )
    assert report["files_scanned"] >= 3
    assert report["total_hits"] == 0, report["hits"]
