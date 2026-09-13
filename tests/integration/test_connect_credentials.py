"""Credential replacement fails closed at every boundary (PR #9 comments 5654839475, 5654916026).

Replacement order: invalidate the old session and any READY state → write the new login as one
secret-store record → record the update. A failure at any boundary leaves at worst "no reusable
session; re-authentication required". Never can new or partial credentials coexist with the old
session able to prove READY, and a restarted process needs a fresh protected-read proof.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from app.config import AppConfig
from app.connect.credentials import SupplierCredentialStore
from app.connect.sessions import SESSIONS_DIR_NAME
from app.connect.state import CapabilityStatus, ConnectionState
from app.container import Container, build_container
from app.core.errors import InputValidationError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from integrations.suppliers.base import Credentials, RequestKind
from tests.suppliers import FAKE_KEY, PASSWORD, USERNAME, FakeGateway, fake_definition
from tests.support import FakeClock

pytestmark = pytest.mark.integration

S = ConnectionState
OPERATOR = "operator:local"
OLD = Credentials(username=USERNAME, password=PASSWORD)
NEW = Credentials(username="replacement-operator", password="replacement-password")


class FlakySecretStore(MemorySecretStore):
    """The OS secret store, able to fail the next write of one entry."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_writes_to: str | None = None

    def set(self, key: str, value: str) -> None:
        if self.fail_writes_to is not None and key.endswith(self.fail_writes_to):
            raise OSError("secret store unavailable")
        super().set(key, value)


@contextmanager
def _process(
    config: AppConfig, gateway: FakeGateway, secrets: MemorySecretStore
) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=FakeClock(),
            secret_store=secrets,
            supplier_gateway=gateway,
            suppliers=(fake_definition(),),
        )
        try:
            built.connect.normalize_on_startup()
            yield built
        finally:
            built.db.dispose()


def _session_file(config: AppConfig) -> bool:
    return (config.data_dir / SESSIONS_DIR_NAME / f"{FAKE_KEY}.enc").exists()


def _boom(*_args: object, **_kwargs: object) -> None:
    raise OSError("injected failure")


# Each case names the boundary and the complete login that must be stored after the failure.
BOUNDARIES: dict[
    str, tuple[Callable[[Container, FlakySecretStore, pytest.MonkeyPatch], None], Credentials]
] = {
    # The session is already invalidated; the new login was never written.
    "after_session_invalidation": (
        lambda app, _secrets, mp: mp.setattr(app.connect._credentials, "save", _boom),
        OLD,
    ),
    # The single credential record fails to write: nothing partial can exist.
    "during_credential_write": (
        lambda _app, secrets, _mp: setattr(secrets, "fail_writes_to", ":credentials"),
        OLD,
    ),
    # The new login is stored; recording the update (DB state/audit) fails.
    "after_credential_write": (
        lambda app, _secrets, mp: mp.setattr(app.connect, "_record_credentials_update", _boom),
        NEW,
    ),
}


@pytest.mark.parametrize("boundary", sorted(BOUNDARIES))
def test_an_interrupted_replacement_never_leaves_the_old_session_able_to_prove_ready(
    boundary: str, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeGateway()
    secrets = FlakySecretStore()
    inject, expected = BOUNDARIES[boundary]
    with _process(config, gateway, secrets) as app:
        app.connect.save_credentials(
            FAKE_KEY, username=OLD.username, password=OLD.password, actor=OPERATOR
        )
        assert app.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True).proven
        assert _session_file(config)
        inject(app, secrets, monkeypatch)
        with pytest.raises(OSError):
            app.connect.save_credentials(
                FAKE_KEY, username=NEW.username, password=NEW.password, actor=OPERATOR
            )
        # In-process: the pre-replacement session is gone and nothing claims READY.
        assert not _session_file(config)
        assert app.connect.supplier_connection(FAKE_KEY).state is S.DISCONNECTED
    monkeypatch.undo()
    secrets.fail_writes_to = None

    # A restarted process holds exactly one complete login and must prove the connection anew.
    assert SupplierCredentialStore(secrets).load(FAKE_KEY) == expected
    gateway.accepted = expected
    with _process(config, gateway, secrets) as restarted:
        summary = restarted.connect.supplier_connection(FAKE_KEY)
        assert (summary.state, summary.session_state) == (S.DISCONNECTED, "NONE")
        assert summary.capability_status is CapabilityStatus.DISCONNECTED
        logins = gateway.logins
        proof = restarted.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True)
        assert proof.proven
        assert gateway.logins == logins + 1, "a fresh login, never the pre-replacement session"
        assert gateway.requests[-2:] == [RequestKind.CONTROL_READ, RequestKind.PROTECTED_READ]


def test_a_failure_before_the_session_is_invalidated_changes_nothing(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Invalidation is the first step: if it fails, the old identity stays whole and consistent.
    gateway = FakeGateway()
    secrets = FlakySecretStore()
    with _process(config, gateway, secrets) as app:
        app.connect.save_credentials(
            FAKE_KEY, username=OLD.username, password=OLD.password, actor=OPERATOR
        )
        app.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True)
        monkeypatch.setattr(app.connect._sessions, "clear", _boom)
        with pytest.raises(OSError):
            app.connect.save_credentials(
                FAKE_KEY, username=NEW.username, password=NEW.password, actor=OPERATOR
            )
        assert SupplierCredentialStore(secrets).load(FAKE_KEY) == OLD
        assert _session_file(config)


def test_partial_or_legacy_credentials_never_drive_a_login(config: AppConfig) -> None:
    gateway = FakeGateway()
    secrets = FlakySecretStore()
    secrets.set(f"supplier:{FAKE_KEY}:username", "new-id")
    secrets.set(f"supplier:{FAKE_KEY}:password", PASSWORD)
    with _process(config, gateway, secrets) as app:
        summary = app.connect.supplier_connection(FAKE_KEY)
        assert (summary.credentials_stored, summary.capability_status) == (
            False,
            CapabilityStatus.NOT_CONFIGURED,
        )
        with pytest.raises(InputValidationError) as caught:
            app.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True)
        assert caught.value.code == "SUPPLIER_CREDENTIALS_MISSING"
        assert gateway.logins == 0 and gateway.requests == []


def test_a_real_password_containing_the_mask_character_is_accepted(config: AppConfig) -> None:
    gateway = FakeGateway()
    secrets = FlakySecretStore()
    with _process(config, gateway, secrets) as app:
        app.connect.save_credentials(
            FAKE_KEY, username=USERNAME, password="real•pass•word", actor=OPERATOR
        )
        stored = SupplierCredentialStore(secrets).load(FAKE_KEY)
        assert stored is not None and stored.password == "real•pass•word"
