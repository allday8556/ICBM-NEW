"""Deterministic readiness: every check is a pure function of current local state."""

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from app import MILESTONE, __version__
from app.core.clock import Clock
from app.core.egress import EgressGuard
from app.core.execution import ExecutionMode
from app.core.ownership import DataDirLease
from app.core.secrets import SecretStore
from app.db.database import Database
from app.db.migrate import current_revision
from app.jobs.worker import JobWorker
from app.system.execution_mode import ExecutionModeService


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class ReadinessCheck(BaseModel):
    name: str
    status: CheckStatus
    detail: str


class ReadinessReport(BaseModel):
    status: CheckStatus
    version: str
    milestone: str
    checked_at: datetime
    checks: list[ReadinessCheck]


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
        ownership: DataDirLease | None = None,
        heartbeat_max_age_s: float = 60.0,
    ) -> None:
        self._ownership = ownership
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
        overall = (
            CheckStatus.PASS
            if all(c.status is CheckStatus.PASS for c in checks)
            else CheckStatus.FAIL
        )
        return ReadinessReport(
            status=overall,
            version=__version__,
            milestone=MILESTONE,
            checked_at=self._clock.now(),
            checks=checks,
        )

    def _data_dir_owner(self) -> tuple[bool, str]:
        # PASS only while this process holds the OS lock on the data directory (ADR-0006).
        if self._ownership is None:
            return False, "data-directory ownership was never acquired"
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
        mode = self._execution_mode.state().mode
        return mode is ExecutionMode.DRY_RUN, f"{mode} (external writes disabled)"

    def _egress_guard(self) -> tuple[bool, str]:
        snapshot = self._egress.snapshot()
        attempts = int(snapshot["external_attempts"])
        ok = bool(snapshot["installed"]) and attempts == 0
        detail = (
            f"{snapshot['policy']}; installed={snapshot['installed']}; external_attempts={attempts}"
        )
        return ok, detail
