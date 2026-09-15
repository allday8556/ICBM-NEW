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
    return (config.runtime_dir / SESSIONS_DIR_NAME / f"{FAKE_KEY}.enc").exists()


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


def _ready_with_old_login(app: Container, config: AppConfig) -> None:
    app.connect.save_credentials(
        FAKE_KEY, username=OLD.username, password=OLD.password, actor=OPERATOR
    )
    assert app.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True).proven
    assert app.connect.supplier_connection(FAKE_KEY).state is S.READY
    assert _session_file(config)


def test_a_failed_state_demotion_leaves_the_old_identity_whole(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inside step 1 the canonical state leaves READY first. If that DB write fails, nothing has
    # changed: old login, old session, and a READY that the session still backs (re-audit
    # 5655076870 / 5655122594).
    gateway = FakeGateway()
    secrets = FlakySecretStore()
    with _process(config, gateway, secrets) as app:
        _ready_with_old_login(app, config)
        monkeypatch.setattr(app.connect, "_demote_for_replacement", _boom)
        with pytest.raises(OSError):
            app.connect.save_credentials(
                FAKE_KEY, username=NEW.username, password=NEW.password, actor=OPERATOR
            )
        assert SupplierCredentialStore(secrets).load(FAKE_KEY) == OLD
        assert _session_file(config)
        summary = app.connect.supplier_connection(FAKE_KEY)
        assert (summary.state, summary.capability_status) == (S.READY, CapabilityStatus.READY)


def test_a_failed_session_deletion_after_demotion_cannot_make_readiness_lie(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The DB left READY, then deleting the old session fails: the replacement stops, the old
    # login stays, and nothing reports READY until a new protected-read proof.
    gateway = FakeGateway()
    secrets = FlakySecretStore()
    with _process(config, gateway, secrets) as app:
        _ready_with_old_login(app, config)
        monkeypatch.setattr(app.connect._sessions, "clear", _boom)
        with pytest.raises(OSError):
            app.connect.save_credentials(
                FAKE_KEY, username=NEW.username, password=NEW.password, actor=OPERATOR
            )
        assert SupplierCredentialStore(secrets).load(FAKE_KEY) == OLD
        summary = app.connect.supplier_connection(FAKE_KEY)
        assert summary.state is S.DISCONNECTED
        assert summary.capability_status is not CapabilityStatus.READY
        ready = {c.key: c.status for c in app.connect.capabilities()}
        assert ready[f"supplier:{FAKE_KEY}"] is not CapabilityStatus.READY
    monkeypatch.undo()
    with _process(config, gateway, secrets) as restarted:
        assert restarted.connect.supplier_connection(FAKE_KEY).state is S.DISCONNECTED
        assert restarted.connect.verify(
            FAKE_KEY, trigger="operator_test", allow_login=True
        ).proven, "READY again only through a new proof (the old login is consistent)"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_ready_without_a_usable_session_heals_itself_and_needs_a_fresh_proof(
    config: AppConfig, damage: str
) -> None:
    # Self-healing invariant (comment 5655122594 §1): READY is reported only while the persisted
    # session is actually usable; otherwise it is demoted, audited, and proven afresh.
    gateway = FakeGateway()
    secrets = FlakySecretStore()
    with _process(config, gateway, secrets) as app:
        _ready_with_old_login(app, config)
        path = config.runtime_dir / SESSIONS_DIR_NAME / f"{FAKE_KEY}.enc"
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"ICBMSESS1" + b"\0" * 40)
        summary = app.connect.supplier_connection(FAKE_KEY)
        assert (summary.state, summary.capability_status, summary.session_state) == (
            S.DISCONNECTED,
            CapabilityStatus.DISCONNECTED,
            "NONE",
        )
        demoted = [
            e
            for e in app.audit.list_events(limit=50)
            if e.event_type == "SUPPLIER_CONNECTION_DEMOTED"
        ]
        assert len(demoted) == 1
        assert demoted[0].details | {"state_from": "READY", "state_to": "DISCONNECTED"} == (
            demoted[0].details
        )
        logins = gateway.logins
        assert app.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True).proven
        assert gateway.logins == logins + 1, "a fresh login and proof, not the lost session"


def test_a_restart_never_carries_ready_over_a_damaged_session(config: AppConfig) -> None:
    gateway = FakeGateway()
    secrets = FlakySecretStore()
    with _process(config, gateway, secrets) as app:
        _ready_with_old_login(app, config)
    (config.runtime_dir / SESSIONS_DIR_NAME / f"{FAKE_KEY}.enc").write_bytes(b"not a session")
    with _process(config, gateway, secrets) as restarted:
        summary = restarted.connect.supplier_connection(FAKE_KEY)
        assert (summary.state, summary.session_state) == (S.DISCONNECTED, "NONE")
        logins = gateway.logins
        assert restarted.connect.verify(FAKE_KEY, trigger="operator_test", allow_login=True).proven
        assert gateway.logins == logins + 1


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


@pytest.mark.parametrize("password", [" •••••••• ", "••••••••x", "•••••••• · 저장됨 "])
def test_only_the_exact_sentinels_are_refused(config: AppConfig, password: str) -> None:
    # Raw comparison: a password that merely resembles a sentinel is a real password.
    gateway = FakeGateway()
    secrets = FlakySecretStore()
    with _process(config, gateway, secrets) as app:
        app.connect.save_credentials(FAKE_KEY, username=USERNAME, password=password, actor=OPERATOR)
        stored = SupplierCredentialStore(secrets).load(FAKE_KEY)
        assert stored is not None and stored.password == password
