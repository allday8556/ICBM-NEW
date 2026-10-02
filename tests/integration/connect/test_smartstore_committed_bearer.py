"""ROADMAP §14 item 4: the CONNECT owner's read-only committed bearer and its four seams.

The bearer is the current committed session of a proven, bound account with more than the renewal
margin left, and nothing else. Asking for it never issues, renews or commits a token, never takes a
single-flight lock and never clears a session. Every provider here is the fake one of the CONNECT
suite; no real SmartStore is reached.
"""

from pathlib import Path

import pytest

from app.config import AppConfig
from app.container import Container
from app.platform.core.secrets import MemorySecretStore
from app.stages.connect.marketplace.capability import AuthStatus
from app.stages.connect.smartstore.service import KEY
from tests.integration.connect.test_smartstore_connect import (  # noqa: F401 - fixtures
    ACTOR,
    CLIENT_ID,
    MARGIN_S,
    OTHER_CLIENT_ID,
    OTHER_SECRET,
    SECRET,
    UID_A,
    UID_B,
    Provider,
    _current,
    _process,
    _session_file,
    p,
    provider,
    secrets,
)
from tests.support.jobs_support import FakeClock

pytestmark = pytest.mark.integration


def _bound(p: Container) -> None:  # noqa: F811
    _current(p)
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    p.smartstore.bind_account(UID_A, actor=ACTOR)


def test_no_credentials_means_no_bearer_and_no_provider_call(
    p: Container,  # noqa: F811
    provider: Provider,  # noqa: F811
) -> None:
    assert p.smartstore.committed_bearer() is None
    assert provider.calls == []


def test_the_bearer_is_the_proven_bound_current_session_and_asking_calls_nothing(
    p: Container,  # noqa: F811
    provider: Provider,  # noqa: F811
) -> None:
    _bound(p)
    calls = list(provider.calls)
    bearer = p.smartstore.committed_bearer()
    assert bearer is not None
    assert bearer.access_token == "fixture-token-1"
    assert (bearer.credential_generation, bearer.session_generation) == (1, 1)
    # Reading it is not a CONNECT: no token is issued or renewed and no account is read.
    for _ in range(3):
        assert p.smartstore.committed_bearer() == bearer
    assert provider.calls == calls
    # The token never appears in a representation.
    assert bearer.access_token not in repr(bearer)


def test_a_session_that_is_not_bound_or_not_proven_is_no_bearer(
    p: Container,  # noqa: F811
    provider: Provider,  # noqa: F811
) -> None:
    p.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
    p.smartstore.connect()  # a committed session, but no binding: auth is not READY
    assert p.marketplace_capability.capability(KEY).auth is not AuthStatus.READY
    assert p.smartstore.committed_bearer() is None


def test_a_session_for_another_account_is_no_bearer(
    p: Container,  # noqa: F811
    provider: Provider,  # noqa: F811
) -> None:
    _bound(p)
    provider.account_uid = UID_B
    p.smartstore.connect()
    assert p.marketplace_capability.capability(KEY).auth is AuthStatus.AUTH_MISMATCH
    assert p.smartstore.committed_bearer() is None


def test_a_session_inside_the_renewal_margin_is_no_bearer_and_is_not_renewed(
    p: Container,  # noqa: F811
    provider: Provider,  # noqa: F811
    clock: FakeClock,
) -> None:
    _bound(p)
    clock.advance(10800 - MARGIN_S - 1)
    assert p.smartstore.committed_bearer() is not None
    clock.advance(1)
    tokens = provider.calls.count("TOKEN")
    assert p.smartstore.committed_bearer() is None
    assert provider.calls.count("TOKEN") == tokens


def test_without_a_renewal_margin_there_is_never_a_bearer(
    config: AppConfig,
    clock: FakeClock,
    secrets: MemorySecretStore,  # noqa: F811
    provider: Provider,  # noqa: F811
) -> None:
    with _process(config, clock, secrets, provider) as first:
        _bound(first)
        assert first.smartstore.committed_bearer() is not None
    with _process(config, clock, secrets, provider, margin_s=None) as second:
        assert second.smartstore.committed_bearer() is None


def test_a_restart_answers_no_bearer_until_connect_proves_the_identity_again(
    config: AppConfig,
    clock: FakeClock,
    secrets: MemorySecretStore,  # noqa: F811
    provider: Provider,  # noqa: F811
) -> None:
    with _process(config, clock, secrets, provider) as first:
        _bound(first)
    with _process(config, clock, secrets, provider) as second:
        second.marketplace_capability.normalize_on_startup()
        assert second.smartstore.committed_bearer() is None
        second.smartstore.connect()
        bearer = second.smartstore.committed_bearer()
        assert bearer is not None and bearer.session_generation == 1


def test_an_older_session_than_the_committed_generation_is_no_bearer(
    p: Container,  # noqa: F811
    provider: Provider,  # noqa: F811
    config: AppConfig,
    clock: FakeClock,
) -> None:
    _bound(p)
    first_session = _session_file(config).read_bytes()
    clock.advance(10800 - MARGIN_S)
    p.smartstore.connect()  # renews: session generation 2
    renewed = p.smartstore.committed_bearer()
    assert renewed is not None and renewed.session_generation == 2
    _session_file(config).write_bytes(first_session)
    assert p.smartstore.committed_bearer() is None


def test_rotating_the_credentials_ends_the_bearer(
    p: Container,  # noqa: F811
    provider: Provider,  # noqa: F811
) -> None:
    _bound(p)
    p.smartstore.save_credentials(OTHER_CLIENT_ID, OTHER_SECRET, actor=ACTOR)
    assert p.smartstore.committed_bearer() is None


def test_an_unreadable_session_is_no_bearer_and_is_never_cleared_by_the_reader(
    p: Container,  # noqa: F811
    provider: Provider,  # noqa: F811
    config: AppConfig,
) -> None:
    _bound(p)
    path: Path = _session_file(config)
    path.write_bytes(b"not a session")
    assert p.smartstore.committed_bearer() is None
    # Discarding a session is its owner's decision, never a reader's.
    assert path.read_bytes() == b"not a session"


def test_the_four_seams_read_the_one_canonical_bearer_source_and_stay_dry_run(
    p: Container,  # noqa: F811
    provider: Provider,  # noqa: F811
) -> None:
    execution = p.registration_execution
    seams = (
        execution._sender,
        execution._readback,
        execution._lookup,
        p.asset_uploads._sender,
    )
    for seam in seams:
        assert seam._bearer == p.smartstore.committed_bearer, type(seam).__name__
    assert execution.readback_available() is False
    assert p.asset_uploads._sender.available() is False
    _bound(p)
    calls = list(provider.calls)
    # With a proven session the read seams can read, and still nothing was sent by asking.
    assert execution.readback_available() is True
    assert p.asset_uploads._sender.available() is True
    assert provider.calls == calls
    # A bearer permits no mutation: the execution mode stays M0's.
    state = p.execution_mode.state()
    assert state.live_writes_permitted is False and state.mode.value == "DRY_RUN"
