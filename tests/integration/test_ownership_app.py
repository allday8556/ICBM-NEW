"""Ownership integrated with the application factory, readiness, migrations and the CLI."""

import contextlib
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.config import AppConfig
from app.container import Container, build_container
from app.core.ownership import (
    DATA_DIR_NOT_OWNED,
    DataDirInUseError,
    DataDirLease,
    OwnershipMismatchError,
    acquire_data_dir,
)
from app.core.secrets import MemorySecretStore
from app.db.migrate import head_revision, read_only_revision, upgrade_to_head
from app.main import create_app
from tests.conftest import LOCAL, make_config

pytestmark = pytest.mark.integration

UPGRADE_HINT = "Stop the ICBM server using this data directory, then retry the database upgrade."

# Every production entry point that mutates the data directory it is handed a lease for.
MUTATING_ENTRY_POINTS: dict[str, Callable[[Path, DataDirLease], object]] = {
    "create_app": lambda target, lease: create_app(make_config(target), ownership=lease),
    "build_container": lambda target, lease: build_container(
        make_config(target), ownership=lease, secret_store=MemorySecretStore()
    ),
    "upgrade_to_head": lambda target, lease: upgrade_to_head(
        make_config(target).database_url, ownership=lease
    ),
}


def _owner_status(container: Container) -> str:
    return next(c.status for c in container.readiness.check().checks if c.name == "data_dir_owner")


def _snapshot(directory: Path) -> dict[str, bytes] | None:
    """Every entry under ``directory`` (file bytes, ``b""`` for directories); None if absent."""
    if not directory.exists():
        return None
    return {
        entry.relative_to(directory).as_posix(): entry.read_bytes() if entry.is_file() else b""
        for entry in sorted(directory.rglob("*"))
    }


@pytest.fixture(params=["missing", "empty", "unmigrated"])
def target(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A data directory that some lease might wrongly be used for."""
    directory = tmp_path / "target"
    if request.param != "missing":
        directory.mkdir()
    if request.param == "unmigrated":
        # A database below head, so a misrouted migration would visibly change it.
        with contextlib.closing(sqlite3.connect(directory / "icbm.db")) as raw:
            raw.execute("CREATE TABLE sentinel (x INTEGER)")
            raw.commit()
    return directory


def test_readiness_passes_only_while_ownership_is_held(container: Container) -> None:
    assert _owner_status(container) == "PASS"
    container.ownership.release()
    assert _owner_status(container) == "FAIL", "released"


def test_app_factory_refuses_an_owned_directory_before_touching_it(config: AppConfig) -> None:
    with acquire_data_dir(config.data_dir, app_version="t"), pytest.raises(DataDirInUseError):
        create_app(config)
    assert not (config.data_dir / "logs").exists(), "no log file was configured"


def test_app_owns_the_directory_while_running_and_releases_it_on_stop(config: AppConfig) -> None:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        checks = {c["name"]: c["status"] for c in client.get("/api/ready").json()["checks"]}
        assert checks["data_dir_owner"] == "PASS"
        with pytest.raises(DataDirInUseError):
            acquire_data_dir(config.data_dir, app_version="t")
    with acquire_data_dir(config.data_dir, app_version="t"):
        pass


@pytest.mark.parametrize("lease_for", ["another_directory", "released"])
@pytest.mark.parametrize("entry", sorted(MUTATING_ENTRY_POINTS))
def test_mutation_requires_a_lease_covering_its_target(
    entry: str, lease_for: str, target: Path, tmp_path: Path
) -> None:
    lease = acquire_data_dir(
        tmp_path / "owned" if lease_for == "another_directory" else target, app_version="t"
    )
    if lease_for == "released":
        lease.release()
    before = _snapshot(target)
    try:
        with pytest.raises(OwnershipMismatchError) as caught:
            MUTATING_ENTRY_POINTS[entry](target, lease)
    finally:
        lease.release()
    assert caught.value.reason_code == DATA_DIR_NOT_OWNED
    assert _snapshot(target) == before, "no directory, log, database, schema or migration step"


def test_migrations_run_only_under_ownership(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'icbm.db').as_posix()}"
    with acquire_data_dir(tmp_path, app_version="t") as lease:
        with pytest.raises(DataDirInUseError):
            upgrade_to_head(url)  # e.g. the alembic CLI while a server owns the directory
        upgrade_to_head(url, ownership=lease)
    assert read_only_revision(tmp_path / "icbm.db") == head_revision()


def test_db_upgrade_refuses_an_owned_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ICBM_DATA_DIR", str(tmp_path))
    with acquire_data_dir(tmp_path, app_version="t"):
        assert cli.main(["db", "upgrade"]) == cli.EXIT_DATA_DIR_IN_USE
    err = capsys.readouterr().err
    assert err.startswith("DATA_DIR_IN_USE")
    assert "diagnostic only" in err
    assert UPGRADE_HINT in err
    assert not (tmp_path / "icbm.db").exists()
    assert not (tmp_path / "logs").exists()


def test_db_current_is_read_only_and_needs_no_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_template: Path,
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setenv("ICBM_DATA_DIR", str(missing))
    assert cli.main(["db", "current"]) == 0
    assert capsys.readouterr().out.strip() == "<no database>"
    assert not missing.exists(), "a read-only command creates nothing"

    owned = tmp_path / "owned"
    owned.mkdir()
    shutil.copyfile(migrated_template, owned / "icbm.db")
    before = (owned / "icbm.db").read_bytes()
    monkeypatch.setenv("ICBM_DATA_DIR", str(owned))
    with acquire_data_dir(owned, app_version="t"):
        assert cli.main(["db", "current"]) == 0
    assert capsys.readouterr().out.strip() == head_revision()
    assert (owned / "icbm.db").read_bytes() == before
