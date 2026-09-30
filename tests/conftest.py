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
def deliberate_egress_attempts(destination: str) -> Iterator[None]:
    """Take the blocked attempts a test makes **on purpose** to ``destination`` back out of the
    process-global guard when the block ends.

    The egress guard is one process-global object and its audit hook can never be removed
    (``app.platform.core.egress``). A test that blocks an external attempt deliberately would
    therefore leave ``external_attempts`` raised for every later test of the same process, and a
    readiness check that reads it would fail for a reason that is not its own.

    Only what was declared is withdrawn: on exit, the attempts recorded **during the block** to
    exactly ``destination`` are removed and the count is lowered by exactly that many. Nothing is
    restored to a snapshot, so an attempt to any other destination — made by the test, by code it
    calls or by another thread — stays counted, and so does everything recorded before the block.
    Inside the block the guard counts exactly as in production.
    """
    with EGRESS._lock:
        known = list(EGRESS._recent)
    try:
        yield
    finally:
        with EGRESS._lock:
            declared = [
                attempt
                for attempt in EGRESS._recent
                if attempt.destination == destination
                and not any(attempt is earlier for earlier in known)
            ]
            for attempt in declared:
                EGRESS._recent.remove(attempt)
            EGRESS._attempts -= len(declared)


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
