import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container, build_container
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.db.migrate import upgrade_to_head
from app.main import create_app
from tests.support import TEST_JOBS, FakeClock

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL = "http://127.0.0.1"


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database at schema head, built once through the real Alembic migrations."""
    directory = tmp_path_factory.mktemp("template")
    database = directory / "icbm.db"
    upgrade_to_head(f"sqlite:///{database.as_posix()}")
    return database


def make_config(data_dir: Path, **overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "data_dir": data_dir,
        "secret_backend": "memory",
        "job_poll_interval_s": 0.02,
        "job_max_attempts": 3,
        "job_backoff_base_s": 0.05,
        "job_backoff_max_s": 1.0,
    }
    values.update(overrides)
    return AppConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
def data_dir(tmp_path: Path, migrated_template: Path) -> Path:
    shutil.copyfile(migrated_template, tmp_path / "icbm.db")
    return tmp_path


@pytest.fixture
def config(data_dir: Path) -> AppConfig:
    return make_config(data_dir)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def container(config: AppConfig, clock: FakeClock) -> Iterator[Container]:
    """The composed services, owning the test data directory like any production process."""
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=MemorySecretStore(),
            extra_jobs=TEST_JOBS,
        )
        try:
            yield built
        finally:
            built.db.dispose()


@pytest.fixture
def client(config: AppConfig) -> Iterator[TestClient]:
    """The real application (system clock, real worker) on a migrated database."""
    app = create_app(config, extra_jobs=TEST_JOBS)
    with TestClient(app, base_url=LOCAL) as test_client:
        yield test_client
