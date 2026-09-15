"""ICBM-NEW decides its own data directory (Issue #52 comment 5688150031): one resolver, one
per-user root, named after the OS secret-store service that holds the logins."""

from pathlib import Path

import pytest

from app.config import (
    REPO_ROOT,
    AppConfig,
    configured_data_dir,
    default_data_dir,
)
from app.core.secrets import SERVICE_NAME


def test_windows_uses_the_per_user_local_application_data(tmp_path: Path) -> None:
    root = default_data_dir({"LOCALAPPDATA": str(tmp_path)}, platform="win32")
    assert root == (tmp_path / SERVICE_NAME).resolve()


def test_other_platforms_use_the_xdg_data_home(tmp_path: Path) -> None:
    root = default_data_dir({"XDG_DATA_HOME": str(tmp_path)}, platform="linux")
    assert root == (tmp_path / SERVICE_NAME).resolve()


@pytest.mark.parametrize(
    ("platform", "variable", "tail"),
    [
        ("win32", "LOCALAPPDATA", ("AppData", "Local", SERVICE_NAME)),
        ("linux", "XDG_DATA_HOME", (".local", "share", SERVICE_NAME)),
    ],
)
def test_a_missing_or_relative_location_falls_back_to_the_home_directory(
    platform: str, variable: str, tail: tuple[str, ...]
) -> None:
    assert default_data_dir({}, platform=platform).parts[-3:] == tail
    assert default_data_dir({variable: "relative"}, platform=platform).parts[-3:] == tail


def test_the_root_depends_on_neither_the_checkout_nor_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = {"LOCALAPPDATA": str(tmp_path / "a"), "XDG_DATA_HOME": str(tmp_path / "a")}
    before = default_data_dir(environ)
    monkeypatch.chdir(tmp_path)
    assert default_data_dir(environ) == before
    assert not before.is_relative_to(REPO_ROOT)
    assert before.name == SERVICE_NAME  # the data root and the credential owner share a name


def test_icbm_data_dir_stays_an_explicit_override_only(tmp_path: Path) -> None:
    environ = {"LOCALAPPDATA": str(tmp_path / "a"), "XDG_DATA_HOME": str(tmp_path / "a")}
    assert configured_data_dir(environ) == default_data_dir(environ)
    assert AppConfig.from_env(environ).data_dir == default_data_dir(environ)
    override = {**environ, "ICBM_DATA_DIR": str(tmp_path / "dedicated")}
    assert configured_data_dir(override) == (tmp_path / "dedicated").resolve()
    assert AppConfig.from_env(override).data_dir == (tmp_path / "dedicated").resolve()


def test_a_config_without_a_data_dir_uses_the_canonical_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert AppConfig().data_dir == default_data_dir() == (tmp_path / SERVICE_NAME).resolve()
