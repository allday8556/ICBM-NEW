from pathlib import Path

import pytest

from app.config import AppConfig, ConfigError
from app.core.execution import ExecutionMode


def test_defaults_are_loopback_and_dry_run(tmp_path: Path) -> None:
    config = AppConfig(data_dir=tmp_path)
    assert config.host == "127.0.0.1"
    assert config.execution_mode is ExecutionMode.DRY_RUN
    assert config.database_url == f"sqlite:///{(tmp_path / 'icbm.db').as_posix()}"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "example.com", "::"])
def test_non_loopback_bind_is_refused(tmp_path: Path, host: str) -> None:
    with pytest.raises(ConfigError, match="loopback"):
        AppConfig(data_dir=tmp_path, host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_loopback_hosts_are_accepted(tmp_path: Path, host: str) -> None:
    assert AppConfig(data_dir=tmp_path, host=host).host == host


def test_live_mode_is_refused_during_m0(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="DRY_RUN only"):
        AppConfig.from_env({"ICBM_DATA_DIR": str(tmp_path), "ICBM_EXECUTION_MODE": "LIVE"})


def test_from_env_parses_typed_values(tmp_path: Path) -> None:
    config = AppConfig.from_env(
        {
            "ICBM_DATA_DIR": str(tmp_path),
            "ICBM_PORT": "9001",
            "ICBM_DIAGNOSTICS": "yes",
            "ICBM_JOB_MAX_ATTEMPTS": "4",
            "ICBM_JOB_BACKOFF_BASE_S": "1.5",
        }
    )
    assert config.port == 9001
    assert config.diagnostics_enabled is True
    assert config.job_max_attempts == 4
    assert config.job_backoff_base_s == 1.5


def test_invalid_environment_value_names_the_variable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ICBM_DIAGNOSTICS"):
        AppConfig.from_env({"ICBM_DATA_DIR": str(tmp_path), "ICBM_DIAGNOSTICS": "maybe"})


def test_backoff_bounds_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        AppConfig(data_dir=tmp_path, job_backoff_base_s=10, job_backoff_max_s=5)
