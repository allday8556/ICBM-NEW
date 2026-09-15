"""Application configuration, read from ``ICBM_*`` environment variables.

Validation enforces accepted architecture rules at startup rather than trusting callers:
loopback-only binding (ADR-0001) and DRY_RUN-only execution during M0 (CLAUDE.md §7.1).
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from app.connect.marketplace.attestation import A0_MAX_AGE_DAYS, valid_max_age_days
from app.core.execution import ExecutionMode
from app.core.net import is_loopback_host
from app.core.ownership import runtime_dir
from app.core.secrets import SERVICE_NAME

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UI_DIR = REPO_ROOT / "ui" / "web"

# The local data root (Issue #52 comment 5688854287) is %USERPROFILE%\ICBM-NEW\data. It is
# overridden only explicitly, through ICBM_DATA_DIR, which then names the data root itself.
HOME_ENV = "USERPROFILE"
DATA_DIR_ENV = "ICBM_DATA_DIR"
DATA_DIR_NAME = "data"
DATABASE_FILE_NAME = "icbm.db"
# The data root's layout, in lowercase names only. ``runtime`` belongs to the application: the
# one relational database, the encrypted sessions and the owner lock. The names below are
# reserved for domain data and are neither created nor filled yet. ``api`` is generic: it covers
# marketplace APIs and other APIs, such as customs checks.
RESERVED_DIR_NAMES = ("products", "api", "suppliers", "logs", "backups")

SecretBackend = Literal["os", "memory"]

# AUTH.md §7/§15: NAVER issues a replacement token only once less than 30 minutes remain, and ICBM
# must not request one outside that window. A configured renewal margin therefore lies strictly
# inside it.
SMARTSTORE_RENEWAL_WINDOW_S = 30 * 60


class ConfigError(ValueError):
    """Configuration violates an accepted architecture rule."""


def default_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """ICBM-NEW's canonical local data root: ``%USERPROFILE%\\ICBM-NEW\\data``.

    It is decided here and nowhere else, from ``USERPROFILE`` alone. Packaged (MSIX) host
    processes get the per-user AppData tree redirected, but not the user profile, so every local
    host (the desktop app, a terminal, a launcher) reaches the same physical directory. There is
    no host detection and no other fallback: without an absolute ``USERPROFILE`` the resolution
    fails closed. This is the local desktop/CLI runtime root only; a server deployment has its
    own storage owner.
    """
    env = os.environ if environ is None else environ
    home = env.get(HOME_ENV, "")
    if not home or not Path(home).is_absolute():
        raise ConfigError(
            f"{HOME_ENV} is missing or not absolute; set {DATA_DIR_ENV} to name the data root"
        )
    return (Path(home) / SERVICE_NAME / DATA_DIR_NAME).resolve()


def configured_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The data root a process uses: ``ICBM_DATA_DIR`` when it is set explicitly (tests,
    dedicated acceptance directories), otherwise the canonical root. Ordinary use never sets it."""
    env = os.environ if environ is None else environ
    raw = env.get(DATA_DIR_ENV)
    return Path(raw).resolve() if raw else default_data_dir(env)


def database_path(data_dir: Path) -> Path:
    """The one canonical relational database of a data root: ``<root>/runtime/icbm.db``."""
    return runtime_dir(data_dir) / DATABASE_FILE_NAME


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


_ENV: dict[str, tuple[str, Callable[[str], Any]]] = {
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
    "browser_channel": ("ICBM_BROWSER_CHANNEL", str),
    "smartstore_a0_max_age_days": ("ICBM_SMARTSTORE_A0_MAX_AGE_DAYS", int),
    "smartstore_renewal_margin_s": ("ICBM_SMARTSTORE_RENEWAL_MARGIN_S", int),
}


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path = field(default_factory=default_data_dir)
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
    # Playwright channel for supplier browser logins: the locally installed Edge, so nothing is
    # downloaded (ADR-0001 Chromium engine).
    browser_channel: str = "msedge"
    # Maximum age of operator-attested SmartStore permission evidence (PERMISSIONS_SCOPES §8.1,
    # Issue #32): the canonical 30 days by default; an operational override may only tighten it.
    smartstore_a0_max_age_days: int = A0_MAX_AGE_DAYS
    # SmartStore proactive token renewal margin in seconds (AUTH.md §15): an ICBM operational
    # policy that must come from configuration, never from a code default. While it is unset,
    # SmartStore CONNECT refuses before any provider call.
    smartstore_renewal_margin_s: int | None = None

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
        if not valid_max_age_days(self.smartstore_a0_max_age_days):
            raise ConfigError(
                f"smartstore_a0_max_age_days must be 1..{A0_MAX_AGE_DAYS}: an override may only "
                "tighten the canonical maximum (PERMISSIONS_SCOPES §8.1)"
            )
        margin = self.smartstore_renewal_margin_s
        if margin is not None and not (
            isinstance(margin, int)
            and not isinstance(margin, bool)
            and 1 <= margin < SMARTSTORE_RENEWAL_WINDOW_S
        ):
            raise ConfigError(
                f"smartstore_renewal_margin_s must be 1..{SMARTSTORE_RENEWAL_WINDOW_S - 1}: the "
                "renewal margin lies inside the provider's under-30-minute window (AUTH.md §15)"
            )

    @property
    def runtime_dir(self) -> Path:
        """The application-owned part of the data root: the database, sessions and owner lock."""
        return runtime_dir(self.data_dir)

    @property
    def database_path(self) -> Path:
        return database_path(self.data_dir)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def source_assets_dir(self) -> Path:
        """Content-addressed COLLECT source assets (ADR-0010 §9), inside the owned data
        directory (ADR-0006)."""
        return self.data_dir / "source-assets"

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
        values: dict[str, Any] = (
            {} if "data_dir" in overrides else {"data_dir": configured_data_dir(env)}
        )
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
