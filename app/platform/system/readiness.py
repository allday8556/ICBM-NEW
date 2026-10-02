"""Deterministic readiness: every check is a pure function of current local state."""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from app import MILESTONE, __version__
from app.capabilities.jobs.worker import JobWorker
from app.platform.core.clock import Clock
from app.platform.core.egress import EgressGuard
from app.platform.core.execution import ExecutionMode
from app.platform.core.ownership import DataDirLease
from app.platform.core.secrets import SecretStore
from app.platform.db.database import Database
from app.platform.db.migrate import current_revision
from app.platform.system.execution_mode import ExecutionModeService
from app.stages.connect.contracts import CapabilityReport
from app.stages.connect.state import CapabilityStatus


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class ReadinessCheck(BaseModel):
    name: str
    status: CheckStatus
    detail: str


class ReadinessReport(BaseModel):
    """Core readiness (``status``/``checks``) plus capability readiness (Issue #7 §7).

    A capability such as ``supplier:kmretail`` never fails core readiness: with a healthy core the
    report is ``overall = READY`` (HTTP 200) and lists every non-READY capability in
    ``degraded_capabilities``. Only a core failure answers 503.
    """

    status: CheckStatus
    overall: Literal["READY", "NOT_READY"]
    version: str
    milestone: str
    checked_at: datetime
    checks: list[ReadinessCheck]
    capabilities: list[CapabilityReport]
    degraded_capabilities: list[str]


class ReadinessService:
    def __init__(
        self,
        *,
        db: Database,
        worker: JobWorker,
        secrets: SecretStore,
        egress: EgressGuard,
        execution_mode: ExecutionModeService,
        clock: Clock,
        head_revision: str | None,
        ownership: DataDirLease,
        capabilities: Callable[[], list[CapabilityReport]] = list,
        heartbeat_max_age_s: float = 60.0,
    ) -> None:
        self._ownership = ownership
        self._capabilities = capabilities
        self._db = db
        self._worker = worker
        self._secrets = secrets
        self._egress = egress
        self._execution_mode = execution_mode
        self._clock = clock
        self._head = head_revision
        self._heartbeat_max_age_s = heartbeat_max_age_s

    def schema_at_head(self) -> bool:
        return self._head is not None and current_revision(self._db.engine) == self._head

    def check(self) -> ReadinessReport:
        probes: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
            ("data_dir_owner", self._data_dir_owner),
            ("database", self._database),
            ("sqlite_wal", self._wal),
            ("schema", self._schema),
            ("job_worker", self._job_worker),
            ("secret_store", self._secret_store),
            ("execution_mode", self._mode),
            ("egress_guard", self._egress_guard),
        ]
        checks: list[ReadinessCheck] = []
        for name, probe in probes:
            try:
                ok, detail = probe()
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            checks.append(
                ReadinessCheck(
                    name=name, status=CheckStatus.PASS if ok else CheckStatus.FAIL, detail=detail
                )
            )
        core = (
            CheckStatus.PASS
            if all(c.status is CheckStatus.PASS for c in checks)
            else CheckStatus.FAIL
        )
        capabilities = self._capabilities()
        return ReadinessReport(
            status=core,
            overall="READY" if core is CheckStatus.PASS else "NOT_READY",
            version=__version__,
            milestone=MILESTONE,
            checked_at=self._clock.now(),
            checks=checks,
            capabilities=capabilities,
            degraded_capabilities=[
                c.key for c in capabilities if c.status is not CapabilityStatus.READY
            ],
        )

    def _data_dir_owner(self) -> tuple[bool, str]:
        # PASS only while this process holds the OS lock on the data directory (ADR-0006).
        return self._ownership.verify()

    def _database(self) -> tuple[bool, str]:
        self._db.ping()
        return True, "SELECT 1 succeeded"

    def _wal(self) -> tuple[bool, str]:
        mode = self._db.journal_mode()
        return mode == "wal", f"journal_mode={mode}"

    def _schema(self) -> tuple[bool, str]:
        current = current_revision(self._db.engine)
        if current is not None and current == self._head:
            return True, f"at head {current}"
        return False, f"current={current} head={self._head}; run `icbm db upgrade`"

    def _job_worker(self) -> tuple[bool, str]:
        heartbeat = self._worker.last_heartbeat
        if not self._worker.running or heartbeat is None:
            return False, "job worker is not running"
        age = (self._clock.now() - heartbeat).total_seconds()
        if age > self._heartbeat_max_age_s:
            return False, f"heartbeat stale ({age:.1f}s)"
        return True, f"running; heartbeat {age:.1f}s ago"

    def _secret_store(self) -> tuple[bool, str]:
        status = self._secrets.status()
        return status.available, f"{status.backend} ({status.detail})"

    def _mode(self) -> tuple[bool, str]:
        state = self._execution_mode.state()
        if state.mode is ExecutionMode.DRY_RUN:
            return True, f"{state.mode} (external writes disabled)"
        # A bounded LIVE window (ADR-0018 §2) is a healthy state; it permits no mutation by itself.
        return state.live_until is not None, (
            f"{state.mode} until {state.live_until} (every mutation still gated by the send-time"
            " stack)"
        )

    def _egress_guard(self) -> tuple[bool, str]:
        snapshot = self._egress.snapshot()
        attempts = int(snapshot["external_attempts"])
        ok = bool(snapshot["installed"]) and attempts == 0
        detail = (
            f"{snapshot['policy']}; installed={snapshot['installed']}; external_attempts={attempts}"
        )
        return ok, detail
