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
    LOCK_FILE_NAME,
    DataDirInUseError,
    OwnershipUnavailableError,
    acquire_data_dir,
    read_owner_metadata,
)


def test_acquisition_writes_diagnostic_metadata(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    with acquire_data_dir(data_dir, app_version="9.9.9") as lease:
        assert lease.active
        assert lease.verify()[0]
        metadata = read_owner_metadata(data_dir / LOCK_FILE_NAME)
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
    assert (tmp_path / LOCK_FILE_NAME).exists()
    with acquire_data_dir(tmp_path, app_version="t") as again:
        assert again.active


def test_stale_metadata_never_blocks_acquisition(tmp_path: Path) -> None:
    (tmp_path / LOCK_FILE_NAME).write_text(json.dumps({"pid": 999_999, "started_at": "2000"}))
    with acquire_data_dir(tmp_path, app_version="t"):
        metadata = read_owner_metadata(tmp_path / LOCK_FILE_NAME)
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
