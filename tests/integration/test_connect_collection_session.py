"""COLLECT uses the M1 connection owner for its session (ADR-0010 §3, §11): no login of its own."""

from collections.abc import Iterator

import pytest

from app.config import AppConfig
from app.connect.credentials import SupplierCredentialStore
from app.container import Container, build_container
from app.core.errors import AuthError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from integrations.suppliers.base import Credentials, RequestKind
from tests.suppliers import FAKE_KEY, PASSWORD, USERNAME, FakeGateway, fake_definition
from tests.support import FakeClock

pytestmark = pytest.mark.integration


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def connected(config: AppConfig, gateway: FakeGateway) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=FakeClock(),
            secret_store=MemorySecretStore(),
            supplier_gateway=gateway,
            suppliers=(fake_definition(),),
        )
        SupplierCredentialStore(built.secrets).save(FAKE_KEY, Credentials(USERNAME, PASSWORD))
        try:
            yield built
        finally:
            built.db.dispose()


def test_collection_reuses_the_proven_session(connected: Container, gateway: FakeGateway) -> None:
    connected.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True)
    first = connected.connect.collection_session(FAKE_KEY)
    second = connected.connect.collection_session(FAKE_KEY)
    assert first == second and first in gateway.valid_sessions
    assert gateway.logins == 1  # reuse, never a login of COLLECT's own
    assert gateway.count(RequestKind.PROTECTED_READ) == 3  # each use re-proves the session


def test_an_expired_session_is_recovered_by_the_connection_owner_once(
    connected: Container, gateway: FakeGateway
) -> None:
    connected.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True)
    gateway.expire_sessions()
    renewed = connected.connect.collection_session(FAKE_KEY)
    assert renewed in gateway.valid_sessions and gateway.logins == 2


def test_no_automatic_login_for_a_connection_never_made(
    connected: Container, gateway: FakeGateway
) -> None:
    with pytest.raises(AuthError):
        connected.connect.collection_session(FAKE_KEY)
    assert gateway.logins == 0


def test_operator_initiated_collection_logs_in_at_most_once(
    connected: Container, gateway: FakeGateway
) -> None:
    session = connected.connect.collection_session(FAKE_KEY, operator_initiated=True)
    assert session in gateway.valid_sessions and gateway.logins == 1
    assert connected.connect.collection_session(FAKE_KEY, operator_initiated=True) == session
    assert gateway.logins == 1
