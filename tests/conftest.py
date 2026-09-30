import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig, database_path
from app.container import Container, build_container
from app.main import create_app
from app.platform.core.egress import EGRESS
from app.platform.core.ownership import acquire_data_dir
from app.platform.core.secrets import MemorySecretStore
from app.platform.db.migrate import upgrade_to_head
from tests.support.jobs_support import TEST_JOBS, FakeClock

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL = "http://127.0.0.1"


@contextmanager
def deliberate_egress_attempts() -> Iterator[None]:
    """Keep the blocked attempts a test makes **on purpose** out of later tests' readiness.

    The egress guard is one process-global object and its audit hook can never be removed
    (``app.platform.core.egress``). A test that blocks an external attempt deliberately would
    therefore leave ``external_attempts`` raised for every later test of the same process, and a
    readiness check that reads it would fail for a reason that is not its own. Inside this block
    the guard counts exactly as in production; on exit the count and the recent attempts are what
    they were on entry.

    It is opt-in, for a test that names its own attempt. It is never applied suite-wide: an
    attempt a test did not intend stays counted, so a later readiness check still exposes it.
    """
    with EGRESS._lock:
        attempts, recent = EGRESS._attempts, tuple(EGRESS._recent)
    try:
        yield
    finally:
        with EGRESS._lock:
            EGRESS._attempts = attempts
            EGRESS._recent.clear()
            EGRESS._recent.extend(recent)


@pytest.fixture
def deliberate_egress() -> Iterator[None]:
    """:func:`deliberate_egress_attempts` for the whole test that asks for it."""
    with deliberate_egress_attempts():
        yield


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
    database = database_path(tmp_path)
    database.parent.mkdir(parents=True)
    shutil.copyfile(migrated_template, database)
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
