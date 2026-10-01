"""The extension tests run the real application with a scripted collection gateway.

From E2 an extension run is recorded through the canonical collection pipeline, which fetches the
product's images through the collection gateway. These fixtures replace that gateway with a script:
it sends nothing, knows no host, and records what it was asked for, so a test can prove that the
server read no product page and which images it asked for.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container, build_container
from app.main import create_app
from app.platform.core.ownership import acquire_data_dir
from app.platform.core.secrets import MemorySecretStore
from automation.acceptance.m3.rehearsal.fake_shop import FakeGateway, StubSessions
from tests.conftest import LOCAL
from tests.support.jobs_support import TEST_JOBS, FakeClock


@pytest.fixture
def gateway() -> FakeGateway:
    # No document: the server never reads a product page for an extension run. Every image answers
    # with the script's default bytes.
    return FakeGateway(documents=[])


@pytest.fixture
def container(config: AppConfig, clock: FakeClock, gateway: FakeGateway) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=MemorySecretStore(),
            extra_jobs=TEST_JOBS,
            collection_gateway=gateway,
            collection_sessions=StubSessions(),
        )
        try:
            yield built
        finally:
            built.db.dispose()


@pytest.fixture
def client(config: AppConfig, gateway: FakeGateway) -> Iterator[TestClient]:
    app = create_app(
        config,
        extra_jobs=TEST_JOBS,
        collection_gateway=gateway,
        collection_sessions=StubSessions(),
    )
    with TestClient(app, base_url=LOCAL) as test_client:
        yield test_client
