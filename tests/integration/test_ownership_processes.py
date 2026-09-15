"""Real-process evidence for single data-directory ownership (Issue #4, ADR-0006).

Every step synchronises on an explicit signal — readiness reported over HTTP, process exit, or the
OS lock becoming acquirable — never on a fixed sleep. CI runs this module on Windows and Ubuntu.

Process identity comes from the server itself (``GET /api/health`` → ``pid``): on Windows a
virtualenv's ``python.exe`` is a launcher whose child is the real interpreter, so ``Popen.pid`` is
not necessarily the process that owns the lock.
"""

import contextlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import database_path
from app.core.logging import LOG_FILE_NAME
from app.core.ownership import (
    DataDirInUseError,
    acquire_data_dir,
    owner_lock_path,
    read_owner_metadata,
)
from app.db.migrate import head_revision

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT = {"X-ICBM-Client": "pytest"}
UPGRADE_HINT = "Stop the ICBM server using this data directory, then retry the database upgrade."


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _env(data_dir: Path, **extra: object) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ICBM_")}
    env |= {
        "ICBM_DATA_DIR": str(data_dir),
        "ICBM_PORT": str(_free_port()),
        "ICBM_SECRET_BACKEND": "memory",
        "ICBM_JOB_POLL_INTERVAL_S": "0.05",
        "PYTHONIOENCODING": "utf-8",
    }
    return env | {key: str(value) for key, value in extra.items()}


def _icbm(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def _until(predicate: Callable[[], Any], timeout_s: float, what: str) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class Server:
    """`icbm serve` as a separate OS process."""

    def __init__(self, env: dict[str, str], output: Path) -> None:
        self._output = output.open("wb")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app", "serve"],
            cwd=REPO_ROOT,
            env=env,
            stdout=self._output,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        self.output = output
        self.pid: int | None = None
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{env['ICBM_PORT']}", timeout=5, trust_env=False
        )

    def wait_ready(self, timeout_s: float = 60.0) -> dict[str, Any]:
        def ready() -> dict[str, Any] | None:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"server exited with {self.proc.returncode}: {self.output.read_text('utf-8')}"
                )
            with contextlib.suppress(httpx.TransportError):
                response = self.http.get("/api/ready")
                if response.status_code == 200:
                    return dict(response.json())
            return None

        report = dict(_until(ready, timeout_s, "readiness PASS"))
        self.pid = int(self.http.get("/api/health").json()["pid"])
        return report

    def stop(self) -> int:
        # Ctrl+Break reaches the whole process group (launcher and interpreter); uvicorn then
        # runs the lifespan shutdown. POSIX has no launcher, so SIGTERM goes to the server.
        self.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        return self._finish()

    def kill(self) -> int:
        # Abrupt termination of the serving interpreter itself: TerminateProcess / SIGKILL,
        # so no shutdown code and no lock release in Python runs.
        assert self.pid is not None, "kill() needs the server pid from wait_ready()"
        os.kill(self.pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
        return self._finish()

    def _finish(self) -> int:
        code = self.proc.wait(30)
        self.http.close()
        self._output.close()
        return code


def _checks(report: dict[str, Any]) -> dict[str, str]:
    return {check["name"]: check["status"] for check in report["checks"]}


def _log(data_dir: Path) -> list[dict[str, Any]]:
    text = (data_dir / "logs" / LOG_FILE_NAME).read_text("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _owner_pid(data_dir: Path) -> int | None:
    metadata = read_owner_metadata(owner_lock_path(data_dir))
    return None if metadata is None else int(metadata["pid"])


def _journal_mode(data_dir: Path) -> str:
    with contextlib.closing(sqlite3.connect(database_path(data_dir))) as conn:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])


def _wait_acquirable(data_dir: Path, timeout_s: float = 5.0) -> float:
    """Bounded poll until the OS lock can be taken (Windows frees handles asynchronously)."""
    started = time.monotonic()
    while True:
        try:
            with acquire_data_dir(data_dir, app_version="probe"):
                return time.monotonic() - started
        except DataDirInUseError:
            if time.monotonic() - started > timeout_s:
                raise
            time.sleep(0.05)


@pytest.fixture
def fresh(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    upgraded = _icbm("db", "upgrade", env=_env(data_dir))
    assert upgraded.returncode == 0, upgraded.stderr
    return data_dir


def test_second_process_cannot_own_and_graceful_stop_hands_over(
    fresh: Path, tmp_path: Path
) -> None:
    owner = Server(_env(fresh), tmp_path / "owner.out")
    try:
        report = owner.wait_ready()
        assert report["status"] == "PASS"
        assert _checks(report)["data_dir_owner"] == "PASS"
        assert _checks(report)["job_worker"] == "PASS"
        assert _owner_pid(fresh) == owner.pid
        log_before = len(_log(fresh))

        # A second server on the same directory, spelled differently, fails fast.
        contender_env = _env(fresh / "logs" / "..")
        contender = _icbm("serve", env=contender_env)
        assert contender.returncode == 3
        assert contender.stderr.startswith("DATA_DIR_IN_USE")
        assert f"pid={owner.pid}" in contender.stderr
        with pytest.raises(httpx.TransportError):
            httpx.get(
                f"http://127.0.0.1:{contender_env['ICBM_PORT']}/api/health",
                timeout=1,
                trust_env=False,
            )

        # Migration cannot run under the server; the read-only command can.
        upgrade = _icbm("db", "upgrade", env=_env(fresh))
        assert upgrade.returncode == 3
        assert upgrade.stderr.startswith("DATA_DIR_IN_USE")
        assert UPGRADE_HINT in upgrade.stderr
        current = _icbm("db", "current", env=_env(fresh))
        assert (current.returncode, current.stdout.strip()) == (0, head_revision())

        # External read-only SQLite inspection is not blocked by the ownership lock.
        assert _journal_mode(fresh) == "wal"

        # Only the owner wrote to the directory meanwhile, and only the owner ever ran a worker.
        lines = _log(fresh)
        assert {line["pid"] for line in lines[log_before:]} <= {owner.pid}
        assert {line["pid"] for line in lines if line["msg"] == "job.worker.started"} == {owner.pid}
    finally:
        owner.stop()
    stopped = [line for line in _log(fresh) if line["msg"] == "app.stopped"]
    assert [line["pid"] for line in stopped] == [owner.pid]

    successor = Server(_env(fresh), tmp_path / "successor.out")
    try:
        assert _checks(successor.wait_ready())["data_dir_owner"] == "PASS"
        assert _owner_pid(fresh) == successor.pid
    finally:
        successor.stop()


def test_forced_termination_releases_ownership_and_jobs_resume(fresh: Path, tmp_path: Path) -> None:
    jobs = {
        "ICBM_DIAGNOSTICS": 1,
        "ICBM_JOB_MAX_ATTEMPTS": 3,
        "ICBM_JOB_BACKOFF_BASE_S": 1,
        "ICBM_JOB_BACKOFF_MAX_S": 5,
    }
    victim = Server(_env(fresh, **jobs), tmp_path / "victim.out")
    victim.wait_ready()
    job_id = victim.http.post("/api/v1/diagnostics/failing-job", headers=CLIENT).json()["job_id"]

    def retry_scheduled() -> bool:
        job = victim.http.get(f"/api/v1/system/jobs/{job_id}").json()
        return bool(job["state"] == "RETRY_SCHEDULED")

    _until(retry_scheduled, 15, "the first failed attempt")
    victim.kill()

    # The lock file and its stale metadata stay; nobody deletes anything to recover.
    assert _owner_pid(fresh) == victim.pid
    released_after_s = _wait_acquirable(fresh, timeout_s=5.0)
    assert released_after_s <= 5.0

    successor = Server(_env(fresh, **jobs), tmp_path / "successor.out")
    try:
        assert _checks(successor.wait_ready())["data_dir_owner"] == "PASS"
        assert _owner_pid(fresh) == successor.pid != victim.pid

        def dead() -> dict[str, Any] | None:
            job = dict(successor.http.get(f"/api/v1/system/jobs/{job_id}").json())
            return job if job["state"] == "DEAD" else None

        done = _until(dead, 30, "the resumed job to dead-letter")
        assert done["attempt_count"] == 3
        assert [a["attempt_no"] for a in done["attempts"]] == [1, 2, 3]
        assert _journal_mode(fresh) == "wal"
    finally:
        successor.stop()
