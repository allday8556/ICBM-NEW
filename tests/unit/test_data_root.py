"""ICBM-NEW decides its own local data root (Issue #52 comment 5688854287): one resolver, taken
from USERPROFILE alone, and the same physical directory for every local host."""

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import (
    DATA_DIR_NAME,
    REPO_ROOT,
    RESERVED_DIR_NAMES,
    AppConfig,
    ConfigError,
    configured_data_dir,
    database_path,
    default_data_dir,
)
from app.core.ownership import LOCK_FILE_NAME, RUNTIME_DIR_NAME, owner_lock_path
from app.core.secrets import SERVICE_NAME

# The Claude desktop app's package-private AppData, as observed on the operator machine
# (review 5688619112).
PACKAGE_LOCAL = Path("AppData", "Local", "Packages", "Claude_pzs8sxrjxfjjc", "LocalCache", "Local")


def test_the_root_is_icbm_new_data_in_the_user_profile(tmp_path: Path) -> None:
    root = default_data_dir({"USERPROFILE": str(tmp_path)})
    assert root == (tmp_path / SERVICE_NAME / DATA_DIR_NAME).resolve()
    assert root.parts[-2:] == ("ICBM-NEW", "data")


@pytest.mark.parametrize("home", [None, "", "relative-home", "relative/home"])
def test_without_an_absolute_user_profile_the_root_fails_closed(home: str | None) -> None:
    environ = {} if home is None else {"USERPROFILE": home}
    with pytest.raises(ConfigError, match="ICBM_DATA_DIR"):
        default_data_dir(environ)
    with pytest.raises(ConfigError):
        AppConfig.from_env(environ)


def test_appdata_and_other_host_variables_never_move_the_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    native = {"USERPROFILE": str(home), "LOCALAPPDATA": str(home / "AppData" / "Local")}
    packaged = {
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / PACKAGE_LOCAL),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "HOME": str(tmp_path / "elsewhere"),
    }
    assert default_data_dir(native) == default_data_dir(packaged)
    assert default_data_dir(native) == default_data_dir({"USERPROFILE": str(home)})


def test_the_root_depends_on_neither_the_checkout_nor_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = {"USERPROFILE": str(tmp_path / "home")}
    before = default_data_dir(environ)
    monkeypatch.chdir(tmp_path)
    assert default_data_dir(environ) == before
    assert not before.is_relative_to(REPO_ROOT)


def test_icbm_data_dir_names_the_data_root_as_an_explicit_override(tmp_path: Path) -> None:
    environ = {"USERPROFILE": str(tmp_path / "home")}
    assert configured_data_dir(environ) == default_data_dir(environ)
    assert AppConfig.from_env(environ).data_dir == default_data_dir(environ)
    dedicated = tmp_path / "dedicated"
    for override in (
        {**environ, "ICBM_DATA_DIR": str(dedicated)},
        {"ICBM_DATA_DIR": str(dedicated)},
    ):
        config = AppConfig.from_env(override)
        assert configured_data_dir(override) == config.data_dir == dedicated.resolve()
        assert config.database_path == dedicated.resolve() / "runtime" / "icbm.db"


def test_a_config_without_a_data_dir_uses_the_canonical_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert AppConfig().data_dir == default_data_dir() == (tmp_path / "ICBM-NEW" / "data").resolve()


def test_the_runtime_layout_under_the_root(tmp_path: Path) -> None:
    config = AppConfig(data_dir=tmp_path)
    assert config.runtime_dir == tmp_path / "runtime"
    assert config.database_path == database_path(tmp_path) == tmp_path / "runtime" / "icbm.db"
    assert owner_lock_path(tmp_path) == tmp_path / RUNTIME_DIR_NAME / LOCK_FILE_NAME
    assert config.log_dir == tmp_path / "logs"
    names = (RUNTIME_DIR_NAME, *RESERVED_DIR_NAMES)
    assert all(name == name.lower() for name in names) and len(set(names)) == len(names)
    assert set(RESERVED_DIR_NAMES) == {"products", "api", "suppliers", "logs", "backups"}


# ---------------------------------------------------------------- physical identity across hosts

# One caller process per host. Each resolves the root through the product's own resolver. The
# writer creates a sentinel there, and the reader reads that very file: no copy, no migration.
_CALLER = """
import sys
from app.config import default_data_dir
root = default_data_dir()
sentinel = root / "host-identity.sentinel"
if sys.argv[1] == "write":
    root.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(sys.argv[2], "utf-8")
print(root)
print(sentinel.read_text("utf-8"))
"""


def _host(home: Path, local_app_data: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("ICBM_")}
    return env | {
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(local_app_data),
        "PYTHONIOENCODING": "utf-8",
    }


def _call(host: dict[str, str], *args: str) -> list[str]:
    done = subprocess.run(
        [sys.executable, "-c", _CALLER, *args],
        cwd=REPO_ROOT,
        env=host,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


@pytest.mark.parametrize("writer", ["packaged", "native"])
def test_packaged_and_native_hosts_reach_the_same_physical_root(
    tmp_path: Path, writer: str
) -> None:
    # Review 5688619112 and ruling 5688854287 §6: the same file, not only an equal string. The two
    # hosts see different AppData locations and the same user profile.
    home = tmp_path / "home"
    hosts = {
        "packaged": _host(home, home / PACKAGE_LOCAL),
        "native": _host(home, home / "AppData" / "Local"),
    }
    reader = "native" if writer == "packaged" else "packaged"
    token = secrets.token_hex(8)
    written_root, written = _call(hosts[writer], "write", token)
    read_root, read = _call(hosts[reader], "read")
    assert written == read == token
    assert os.path.samefile(written_root, read_root)
    assert [path.name for path in Path(read_root).iterdir()] == ["host-identity.sentinel"]
