"""COLLECT uses the M1 connection owner for its session (ADR-0010 §3, §11): no login of its own."""

import json
from collections.abc import Iterator

import pytest

from app.config import AppConfig
from app.connect.credentials import SupplierCredentialStore
from app.connect.sessions import SESSIONS_DIR_NAME, SupplierSessionStore
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


def test_the_scan_boundary_hands_out_cookie_material_and_never_a_session(
    connected: Container, gateway: FakeGateway
) -> None:
    # PR #64 review 5217542767 §3: local leak scanning needs the values a collected page could
    # have echoed, and nothing a transport could use. It asks the supplier for nothing.
    assert connected.connect.session_cookies_for_scan(FAKE_KEY) == []
    connected.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True)
    before = (gateway.logins, gateway.count(RequestKind.PROTECTED_READ))
    cookies = connected.connect.session_cookies_for_scan(FAKE_KEY)
    assert cookies and all(set(cookie) == {"name", "value"} for cookie in cookies)
    assert all(isinstance(value, str) for cookie in cookies for value in cookie.values())
    assert (gateway.logins, gateway.count(RequestKind.PROTECTED_READ)) == before
    assert not isinstance(cookies, bytes | bytearray), "no session payload leaves the owner"


def _payload(cookies: list[dict[str, str]]) -> bytes:
    """A payload of the stored format's own version, corrupt only in its cookie entries."""
    return json.dumps({"v": 1, "cookies": cookies, "user_agent": "x"}).encode("utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        b"{",  # malformed JSON
        b'{"v": 2, "cookies": [], "user_agent": "x"}',  # another version
        b'{"v": 1, "cookies": "not-a-list", "user_agent": "x"}',  # malformed schema
        b'{"v": 1, "cookies": [], "user_agent": 7}',  # wrong type
        b"\xff\xfe not utf-8",  # not decodable text at all
        b"",
        # Review 5219631112: corrupt but v1, with entries the shared decoder tolerates.
        _payload([{"domain": "x"}]),  # neither a name nor a value
        _payload([{"name": "SID"}]),  # no value
        _payload([{"value": "s"}]),  # no name
        _payload([{"name": "SID", "value": "s"}, {}]),  # one usable entry, one not
    ],
)
def test_an_unreadable_stored_session_fails_the_scan_boundary_closed(
    connected: Container, gateway: FakeGateway, payload: bytes
) -> None:
    # PR #64 comment 5690832285 §1 and §2: no representation of the stored payload leaves
    # CONNECT, and no sibling path returns one instead.
    connected.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True)
    sessions = SupplierSessionStore(
        connected.config.runtime_dir / SESSIONS_DIR_NAME, connected.secrets
    )
    sessions.save(FAKE_KEY, payload)
    before = (gateway.logins, gateway.count(RequestKind.PROTECTED_READ))
    with pytest.raises(AuthError) as refused:
        connected.connect.session_cookies_for_scan(FAKE_KEY)
    assert refused.value.code == "SUPPLIER_SESSION_UNREADABLE"
    surfaced = f"{refused.value.code} {refused.value.message} {refused.value!r}"
    for fragment in (payload.decode("utf-8", "replace"), payload.hex()):
        if fragment.strip():
            assert fragment not in surfaced
    assert refused.value.__cause__ is None and refused.value.__context__ is None
    assert (gateway.logins, gateway.count(RequestKind.PROTECTED_READ)) == before
