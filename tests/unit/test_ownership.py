"""The ownership primitive itself (ADR-0006). Cross-process behaviour is covered by
tests/integration/test_ownership_processes.py."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import ownership
from app.core.ownership import (
    DATA_DIR_IN_USE,
    DATA_DIR_LOCK_UNSUPPORTED,
    DATA_DIR_NOT_OWNED,
    LOCK_FILE_NAME,
    RUNTIME_DIR_NAME,
    DataDirInUseError,
    OwnershipMismatchError,
    OwnershipUnavailableError,
    acquire_data_dir,
    acquire_database_dir,
    owner_lock_path,
    read_owner_metadata,
    require_ownership,
)
from app.db.database import sqlite_database_dir


def test_acquisition_writes_diagnostic_metadata(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    with acquire_data_dir(data_dir, app_version="9.9.9") as lease:
        assert lease.active
        assert lease.verify()[0]
        metadata = read_owner_metadata(owner_lock_path(data_dir))
        assert metadata is not None
        assert metadata["pid"] == os.getpid()
        assert metadata["app_version"] == "9.9.9"
        assert metadata["resolved_data_dir"] == str(data_dir.resolve())
        assert set(metadata) >= {"started_at", "hostname", "lock"}


def test_a_second_owner_is_refused_even_within_one_process(tmp_path: Path) -> None:
    with (
        acquire_data_dir(tmp_path, app_version="t"),
        pytest.raises(DataDirInUseError) as caught,
    ):
        acquire_data_dir(tmp_path, app_version="t")
    assert caught.value.reason_code == DATA_DIR_IN_USE
    assert caught.value.holder is not None and caught.value.holder["pid"] == os.getpid()
    assert str(tmp_path.resolve()) in str(caught.value)


def _alias(kind: str, target: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if kind == "relative":
        monkeypatch.chdir(workdir)
        return Path(target.name)
    if kind == "dotdot":
        (target / "sub").mkdir()
        return target / "sub" / ".."
    link = workdir / "alias"
    if sys.platform == "win32":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if made.returncode != 0:
            pytest.skip("cannot create a directory junction here")
    else:
        os.symlink(target, link, target_is_directory=True)
    return link


@pytest.mark.parametrize("kind", ["relative", "dotdot", "link"])
def test_every_spelling_of_the_same_directory_contends_for_one_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    target = tmp_path / "data"
    target.mkdir()
    spelled = _alias(kind, target, tmp_path, monkeypatch)
    with acquire_data_dir(target, app_version="t"), pytest.raises(DataDirInUseError):
        acquire_data_dir(spelled, app_version="t")


def test_release_allows_reacquisition_and_keeps_the_lock_file(tmp_path: Path) -> None:
    lease = acquire_data_dir(tmp_path, app_version="t")
    lease.release()
    lease.release()  # idempotent
    assert not lease.active
    assert lease.verify() == (False, "ownership lease was released")
    assert owner_lock_path(tmp_path).exists()
    with acquire_data_dir(tmp_path, app_version="t") as again:
        assert again.active


def test_stale_metadata_never_blocks_acquisition(tmp_path: Path) -> None:
    stale = owner_lock_path(tmp_path)
    stale.parent.mkdir()
    stale.write_text(json.dumps({"pid": 999_999, "started_at": "2000"}))
    with acquire_data_dir(tmp_path, app_version="t"):
        metadata = read_owner_metadata(stale)
        assert metadata is not None and metadata["pid"] == os.getpid()


def test_a_platform_without_a_lock_primitive_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ownership, "_detect_platform_lock", lambda: None)
    with pytest.raises(OwnershipUnavailableError) as caught:
        acquire_data_dir(tmp_path / "data", app_version="t")
    assert caught.value.reason_code == DATA_DIR_LOCK_UNSUPPORTED
    assert not (tmp_path / "data").exists(), "nothing is created without ownership"


def test_lease_covers_only_its_own_directory(tmp_path: Path) -> None:
    (tmp_path / "a" / "sub").mkdir(parents=True)
    (tmp_path / "b").mkdir()
    with acquire_data_dir(tmp_path / "a", app_version="t") as lease:
        assert lease.covers(tmp_path / "a" / "sub" / "..")
        assert not lease.covers(tmp_path / "b")


def test_the_owner_lock_lives_in_the_runtime_directory(tmp_path: Path) -> None:
    # Issue #52 comment 5688854287: <root>/runtime holds the database, the sessions and the lock.
    runtime = tmp_path / RUNTIME_DIR_NAME
    with acquire_data_dir(tmp_path, app_version="t") as lease:
        assert lease.lock_path == owner_lock_path(tmp_path) == runtime / LOCK_FILE_NAME
        assert lease.covers(tmp_path) and lease.covers(runtime)
        # A caller that knows only the database directory contends for the very same lock.
        with pytest.raises(DataDirInUseError):
            acquire_database_dir(runtime, app_version="t")
    with acquire_database_dir(runtime, app_version="t") as by_database:
        assert by_database.covers(runtime) and by_database.covers(tmp_path)
        with pytest.raises(DataDirInUseError):
            acquire_data_dir(tmp_path, app_version="t")


def test_require_ownership_accepts_only_an_active_lease_on_that_directory(tmp_path: Path) -> None:
    (tmp_path / "a" / "sub").mkdir(parents=True)
    (tmp_path / "b").mkdir()
    with pytest.raises(OwnershipMismatchError, match="no active ownership lease"):
        require_ownership(None, tmp_path / "a")
    lease = acquire_data_dir(tmp_path / "a", app_version="t")
    try:
        assert require_ownership(lease, tmp_path / "a" / "sub" / "..") is lease
        with pytest.raises(OwnershipMismatchError, match="does not cover") as caught:
            require_ownership(lease, tmp_path / "b")
        assert caught.value.reason_code == DATA_DIR_NOT_OWNED
    finally:
        lease.release()
    with pytest.raises(OwnershipMismatchError, match="no active ownership lease"):
        require_ownership(lease, tmp_path / "a")
    assert list((tmp_path / "b").iterdir()) == [], "checking a target creates nothing in it"


def test_the_database_mutation_target_is_its_directory(tmp_path: Path) -> None:
    assert sqlite_database_dir(f"sqlite:///{(tmp_path / 'icbm.db').as_posix()}") == tmp_path


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:", "postgresql://host/icbm"])
def test_a_database_without_a_data_directory_fails_closed(url: str) -> None:
    with pytest.raises(ValueError, match="not a file-backed SQLite database"):
        sqlite_database_dir(url)
