from pathlib import Path

import pytest

from app.config import AppConfig, ConfigError
from app.core.execution import ExecutionMode


def test_defaults_are_loopback_and_dry_run(tmp_path: Path) -> None:
    config = AppConfig(data_dir=tmp_path)
    assert config.host == "127.0.0.1"
    assert config.execution_mode is ExecutionMode.DRY_RUN
    assert config.database_url == f"sqlite:///{(tmp_path / 'runtime' / 'icbm.db').as_posix()}"


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


def test_s17_22_a0_evidence_age_defaults_to_the_canonical_30_days(tmp_path: Path) -> None:
    # PERMISSIONS_SCOPES §8.1 (Issue #32): frozen at 30 days for M2, never "unset".
    assert AppConfig(data_dir=tmp_path).smartstore_a0_max_age_days == 30
    env = {"ICBM_DATA_DIR": str(tmp_path), "ICBM_SMARTSTORE_A0_MAX_AGE_DAYS": "7"}
    assert AppConfig.from_env(env).smartstore_a0_max_age_days == 7


@pytest.mark.parametrize("days", [1, 30])
def test_s17_22_a0_evidence_age_override_may_tighten_down_to_one_day(
    tmp_path: Path, days: int
) -> None:
    config = AppConfig(data_dir=tmp_path, smartstore_a0_max_age_days=days)
    assert config.smartstore_a0_max_age_days == days


@pytest.mark.parametrize("days", [0, -1, 31, 365])
def test_s17_22_a0_evidence_age_override_can_never_extend_past_30_days(
    tmp_path: Path, days: int
) -> None:
    with pytest.raises(ConfigError, match=r"1\.\.30"):
        AppConfig(data_dir=tmp_path, smartstore_a0_max_age_days=days)
    env = {"ICBM_DATA_DIR": str(tmp_path), "ICBM_SMARTSTORE_A0_MAX_AGE_DAYS": str(days)}
    with pytest.raises(ConfigError, match=r"1\.\.30"):
        AppConfig.from_env(env)


def test_the_smartstore_renewal_margin_has_no_code_default(tmp_path: Path) -> None:
    # AUTH.md §15: configured operational policy, never a hard-coded value.
    assert AppConfig(data_dir=tmp_path).smartstore_renewal_margin_s is None
    env = {"ICBM_DATA_DIR": str(tmp_path), "ICBM_SMARTSTORE_RENEWAL_MARGIN_S": "900"}
    assert AppConfig.from_env(env).smartstore_renewal_margin_s == 900


@pytest.mark.parametrize("seconds", [1, 600, 1799])
def test_the_renewal_margin_lies_inside_the_provider_window(tmp_path: Path, seconds: int) -> None:
    config = AppConfig(data_dir=tmp_path, smartstore_renewal_margin_s=seconds)
    assert config.smartstore_renewal_margin_s == seconds


@pytest.mark.parametrize("seconds", [0, -60, 1800, 3600, True])
def test_a_renewal_margin_outside_the_window_is_refused(tmp_path: Path, seconds: int) -> None:
    with pytest.raises(ConfigError, match=r"1\.\.1799"):
        AppConfig(data_dir=tmp_path, smartstore_renewal_margin_s=seconds)


@pytest.mark.parametrize("raw", ["1800", "0", "ten minutes", "600.5"])
def test_a_renewal_margin_from_the_environment_is_validated(tmp_path: Path, raw: str) -> None:
    env = {"ICBM_DATA_DIR": str(tmp_path), "ICBM_SMARTSTORE_RENEWAL_MARGIN_S": raw}
    with pytest.raises(ConfigError):
        AppConfig.from_env(env)
