"""Application configuration, read from ``ICBM_*`` environment variables.

Validation enforces accepted architecture rules at startup rather than trusting callers:
loopback-only binding (ADR-0001) and DRY_RUN-only execution during M0 (CLAUDE.md §7.1).
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from app.core.execution import ExecutionMode
from app.core.net import is_loopback_host

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "var"
DEFAULT_UI_DIR = REPO_ROOT / "ui" / "web"

SecretBackend = Literal["os", "memory"]


class ConfigError(ValueError):
    """Configuration violates an accepted architecture rule."""


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


_ENV: dict[str, tuple[str, Callable[[str], Any]]] = {
    "data_dir": ("ICBM_DATA_DIR", lambda raw: Path(raw).resolve()),
    "host": ("ICBM_HOST", str),
    "port": ("ICBM_PORT", int),
    "execution_mode": ("ICBM_EXECUTION_MODE", ExecutionMode),
    "log_level": ("ICBM_LOG_LEVEL", str),
    "log_to_file": ("ICBM_LOG_TO_FILE", _parse_bool),
    "diagnostics_enabled": ("ICBM_DIAGNOSTICS", _parse_bool),
    "secret_backend": ("ICBM_SECRET_BACKEND", str),
    "operator_name": ("ICBM_OPERATOR_NAME", str),
    "ui_dir": ("ICBM_UI_DIR", lambda raw: Path(raw).resolve()),
    "job_poll_interval_s": ("ICBM_JOB_POLL_INTERVAL_S", float),
    "job_max_attempts": ("ICBM_JOB_MAX_ATTEMPTS", int),
    "job_backoff_base_s": ("ICBM_JOB_BACKOFF_BASE_S", float),
    "job_backoff_factor": ("ICBM_JOB_BACKOFF_FACTOR", float),
    "job_backoff_max_s": ("ICBM_JOB_BACKOFF_MAX_S", float),
    "job_lease_s": ("ICBM_JOB_LEASE_S", float),
}


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path = DEFAULT_DATA_DIR
    host: str = "127.0.0.1"
    # Not 8765: legacy clients still poll that port on operator machines.
    port: int = 8790
    execution_mode: ExecutionMode = ExecutionMode.DRY_RUN
    log_level: str = "INFO"
    log_to_file: bool = True
    diagnostics_enabled: bool = False
    secret_backend: SecretBackend = "os"
    operator_name: str = "관리자"
    ui_dir: Path = DEFAULT_UI_DIR
    job_poll_interval_s: float = 0.25
    job_max_attempts: int = 5
    job_backoff_base_s: float = 2.0
    job_backoff_factor: float = 2.0
    job_backoff_max_s: float = 300.0
    job_lease_s: float = 300.0

    def __post_init__(self) -> None:
        if not is_loopback_host(self.host):
            raise ConfigError(
                f"ICBM v1 binds to loopback only (ADR-0001); {self.host!r} is not a loopback "
                "address."
            )
        if self.execution_mode is not ExecutionMode.DRY_RUN:
            raise ConfigError(
                "M0 permits DRY_RUN only. LIVE requires an explicit GitHub verification scope "
                "and user approval (CLAUDE.md §7.1)."
            )
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"port out of range: {self.port}")
        if self.secret_backend not in ("os", "memory"):
            raise ConfigError(f"unknown secret backend: {self.secret_backend!r}")
        if self.job_max_attempts < 1:
            raise ConfigError("job_max_attempts must be >= 1")
        if self.job_backoff_base_s <= 0 or self.job_backoff_factor < 1:
            raise ConfigError("job backoff requires base > 0 and factor >= 1")
        if self.job_backoff_max_s < self.job_backoff_base_s:
            raise ConfigError("job_backoff_max_s must be >= job_backoff_base_s")
        if self.job_poll_interval_s <= 0 or self.job_lease_s <= 0:
            raise ConfigError("job poll interval and lease must be > 0")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "icbm.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def log_dir(self) -> Path | None:
        return self.data_dir / "logs" if self.log_to_file else None

    @property
    def operator_actor(self) -> str:
        """Audit actor for UI/API-initiated actions. v1 is a single local operator."""
        return "operator:local"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> "AppConfig":
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for field_name, (var, parse) in _ENV.items():
            raw = env.get(var)
            if raw is None or raw == "":
                continue
            try:
                values[field_name] = parse(raw)
            except ValueError as exc:
                raise ConfigError(f"{var}: {exc}") from exc
        values.update(overrides)
        return cls(**values)

    def with_overrides(self, **changes: Any) -> "AppConfig":
        return replace(self, **changes)
