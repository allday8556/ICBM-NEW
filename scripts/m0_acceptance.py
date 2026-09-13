"""M0 end-to-end acceptance run (Issue #1).

Runs the real application as a separate OS process against a fresh data directory and writes
evidence to ``--out``. It demonstrates, rather than asserts:

* an empty database migrated to head in WAL mode, and the application starting on it;
* deterministic readiness;
* the deliberately failing job retrying on the configured schedule into dead-letter while the
  API keeps answering;
* one correlation_id traced through the log file, the Job row and AuditEvent rows;
* a protected action (LIVE request) denied with a persisted AuditEvent the database refuses to
  update or delete;
* all ten screen contracts reporting EMPTY (and, with ``--visual``, rendering in a browser);
* full restarts: queued work survives a stop and completes afterwards, and a second restart comes
  back ready with unchanged state;
* zero external connection attempts in every server run (process-wide egress guard).

The database is inspected directly with ``sqlite3``, independently of the application.
"""

import argparse
import contextlib
import itertools
import json
import os
import platform
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SCREENS = [
    "dashboard",
    "collect",
    "db",
    "register",
    "orders",
    "inquiry",
    "soldout",
    "ai-insight",
    "analytics",
    "settings",
]
CLIENT = {"X-ICBM-Client": "m0-acceptance"}
MAX_ATTEMPTS = 4
EXPECTED_DELAYS_S = [1.0, 2.0, 4.0]
POLL_INTERVAL_S = 0.1
START_LAG_TOLERANCE_S = 0.5
TRACE_MESSAGES = {
    "http.request",
    "job.enqueued",
    "job.attempt.started",
    "job.attempt.failed",
    "job.dead_lettered",
    "audit.appended",
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def db_time(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


class Evidence:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {}

    def check(self, name: str, passed: bool, **details: Any) -> bool:
        result = "PASS" if passed else "FAIL"
        self.steps.append({"step": name, "result": result, "at": now_iso(), **details})
        print(f"[{result}] {name}", flush=True)
        return passed

    @property
    def passed(self) -> bool:
        return bool(self.steps) and all(s["result"] == "PASS" for s in self.steps)


class Database:
    """Direct SQLite inspection, independent of the application process."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params)]

    def value(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        rows = self.rows(sql, params)
        return next(iter(rows[0].values())) if rows else None

    def job(self, job_id: str) -> dict[str, Any]:
        return self.rows("SELECT * FROM jobs WHERE job_id = ?", (job_id,))[0]

    def attempts(self, job_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM job_attempts WHERE job_id = ? ORDER BY attempt_no", (job_id,)
        )


class Server:
    def __init__(self, env: dict[str, str], port: int, stdout_path: Path) -> None:
        self.env = env
        self.base_url = f"http://127.0.0.1:{port}"
        self.stdout_path = stdout_path
        self.proc: subprocess.Popen[bytes] | None = None
        self.http = httpx.Client(base_url=self.base_url, timeout=10, trust_env=False)

    def start(self, timeout_s: float = 60.0) -> None:
        self._stdout = self.stdout_path.open("wb")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app", "serve"],
            cwd=REPO_ROOT,
            env=self.env,
            stdout=self._stdout,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited with {self.proc.returncode}: {self.stdout_path}")
            with contextlib.suppress(httpx.TransportError):
                if self.http.get("/api/health").status_code == 200:
                    return
            time.sleep(0.2)
        raise RuntimeError("server did not become healthy in time")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout_s: float = 30.0) -> str:
        assert self.proc is not None
        # Ctrl+Break on Windows / SIGTERM elsewhere: uvicorn runs the lifespan shutdown.
        self.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        try:
            self.proc.wait(timeout_s)
            how = "graceful"
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            how = "killed"
        self.http.close()
        self._stdout.close()
        return how


def wait_for(predicate: Callable[[], Any], timeout_s: float, interval_s: float = 0.2) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    return None


def log_lines(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "logs" / "icbm.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- steps


def step_environment(ev: Evidence, run_id: str, port: int, data_dir: Path) -> None:
    freeze = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, check=False
    ).stdout.splitlines()
    legacy = [line for line in freeze if "icbm-project" in line.lower() or "icbm_project" in line]
    ev.data["environment"] = {
        "run_id": run_id,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_worktree_clean": git("status", "--porcelain") == "",
        "port": port,
        "data_dir": str(data_dir),
        "installed_distributions": freeze,
    }
    ev.check(
        "no runtime dependency on ICBM-PROJECT / #86",
        not legacy,
        legacy_distributions=legacy,
        distributions=len(freeze),
    )


def step_migrate(ev: Evidence, env: dict[str, str], db: Database, out: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "app", "db", "upgrade"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    (out / "migrate.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    tables = sorted(
        r["name"] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'table'")
    )
    triggers = sorted(
        r["name"] for r in db.rows("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    )
    counts = {
        t: db.value(f"SELECT COUNT(*) FROM {t}") for t in ("jobs", "job_attempts", "audit_events")
    }
    revision = db.value("SELECT version_num FROM alembic_version")
    journal = db.value("PRAGMA journal_mode")
    ev.check(
        "clean database migrated to head (SQLite WAL), canonical tables empty",
        completed.returncode == 0
        and revision == "0001_m0_foundation"
        and journal == "wal"
        and {"jobs", "job_attempts", "audit_events", "alembic_version"} <= set(tables)
        and all(v == 0 for v in counts.values()),
        revision=revision,
        journal_mode=journal,
        tables=tables,
        triggers=triggers,
        row_counts=counts,
    )


def step_started(ev: Evidence, server: Server, label: str) -> None:
    health = server.http.get("/api/health").json()
    ev.check(
        f"{label}: application process started and healthy",
        health["status"] == "ok",
        pid=server.proc.pid if server.proc else None,
        health=health,
    )


def step_readiness(ev: Evidence, server: Server, label: str) -> None:
    first = server.http.get("/api/ready")
    second = server.http.get("/api/ready")
    a, b = first.json(), second.json()
    same = [(c["name"], c["status"]) for c in a["checks"]] == [
        (c["name"], c["status"]) for c in b["checks"]
    ]
    ev.check(
        f"{label}: readiness PASS, identical on repeat",
        first.status_code == second.status_code == 200 and a["status"] == "PASS" and same,
        checks=a["checks"],
    )


def step_contracts(ev: Evidence, server: Server, label: str) -> None:
    shell = server.http.get("/api/v1/shell").json()
    screens = {}
    for screen in SCREENS:
        meta = server.http.get(f"/api/v1/screens/{screen}").json()["meta"]
        screens[screen] = {"state": meta["state"], "empty_reason": meta["empty_reason"]}
    index = server.http.get("/")
    csp = index.headers.get("content-security-policy", "")
    ev.check(
        f"{label}: all ten screen contracts EMPTY; UI shell served with self-only CSP",
        len(screens) == 10
        and all(s["state"] == "EMPTY" for s in screens.values())
        and index.status_code == 200
        and "default-src 'self'" in csp,
        screens=screens,
        marketplace_identities=[m["key"] for m in shell["marketplaces"]],
        execution_mode=shell["execution_mode"],
    )


def step_dead_letter(ev: Evidence, server: Server, db: Database, run_id: str) -> dict[str, Any]:
    correlation_id = f"m0-acc-{run_id}-deadletter"
    response = server.http.post(
        "/api/v1/diagnostics/failing-job", headers=CLIENT | {"X-Correlation-ID": correlation_id}
    )
    job_id = response.json()["job_id"]
    latencies: list[float] = []

    def dead() -> bool:
        started = time.perf_counter()
        server.http.get("/api/health").raise_for_status()
        latencies.append((time.perf_counter() - started) * 1000)
        return bool(db.value("SELECT state FROM jobs WHERE job_id = ?", (job_id,)) == "DEAD")

    wait_for(dead, timeout_s=60)
    job = db.job(job_id)
    attempts = db.attempts(job_id)
    schedule = [
        {
            "attempt_no": cur["attempt_no"],
            "planned_delay_s": round(
                (db_time(cur["scheduled_for"]) - db_time(prev["finished_at"])).total_seconds(), 3
            ),
            "start_lag_s": round(
                (db_time(cur["started_at"]) - db_time(cur["scheduled_for"])).total_seconds(), 3
            ),
        }
        for prev, cur in itertools.pairwise(attempts)
    ]
    ev.check(
        "deliberately failing job retries on schedule and reaches dead-letter at the cap",
        response.status_code == 202
        and job["state"] == "DEAD"
        and job["attempt_count"] == MAX_ATTEMPTS
        and [a["outcome"] for a in attempts] == ["FAILED"] * MAX_ATTEMPTS
        and job["last_error_class"] == "TRANSIENT"
        and [round(s["planned_delay_s"], 2) for s in schedule] == EXPECTED_DELAYS_S
        and all(0 <= s["start_lag_s"] <= START_LAG_TOLERANCE_S for s in schedule),
        job_id=job_id,
        correlation_id=correlation_id,
        configured={"max_attempts": MAX_ATTEMPTS, "expected_delays_s": EXPECTED_DELAYS_S},
        job={
            k: job[k]
            for k in (
                "state",
                "attempt_count",
                "max_attempts",
                "last_error_class",
                "last_error_code",
                "created_at",
                "finished_at",
            )
        },
        attempts=[
            {
                k: a[k]
                for k in (
                    "attempt_no",
                    "scheduled_for",
                    "started_at",
                    "finished_at",
                    "outcome",
                    "error_class",
                    "error_code",
                    "retry_at",
                )
            }
            for a in attempts
        ],
        schedule=schedule,
    )
    ev.check(
        "API stays responsive while the in-process worker runs the job",
        bool(latencies) and max(latencies) < 1000,
        health_probes=len(latencies),
        max_latency_ms=round(max(latencies), 1) if latencies else None,
        median_latency_ms=round(statistics.median(latencies), 1) if latencies else None,
    )
    return {"job_id": job_id, "correlation_id": correlation_id}


def step_trace(ev: Evidence, db: Database, data_dir: Path, out: Path, dead: dict[str, Any]) -> None:
    correlation_id = dead["correlation_id"]
    traced = [line for line in log_lines(data_dir) if line.get("correlation_id") == correlation_id]
    (out / "trace-deadletter.jsonl").write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in traced) + "\n", encoding="utf-8"
    )
    job_correlation = db.value(
        "SELECT correlation_id FROM jobs WHERE job_id = ?", (dead["job_id"],)
    )
    audit = db.rows(
        "SELECT seq, event_id, occurred_at, actor, event_type, action, outcome, reason_code, "
        "target_ref FROM audit_events WHERE correlation_id = ? ORDER BY seq",
        (correlation_id,),
    )
    messages = sorted({line["msg"] for line in traced})
    ev.check(
        "one correlation_id traced across log output, Job record and AuditEvent",
        set(messages) >= TRACE_MESSAGES
        and job_correlation == correlation_id
        and {a["event_type"] for a in audit} == {"DIAGNOSTIC_REQUEST", "JOB_DEAD_LETTERED"},
        correlation_id=correlation_id,
        log_file="logs/icbm.jsonl",
        log_lines_with_id=len(traced),
        log_messages=messages,
        job_correlation_id=job_correlation,
        audit_events=audit,
        excerpt="trace-deadletter.jsonl",
    )


def step_protected_action(ev: Evidence, server: Server, db: Database, run_id: str) -> None:
    correlation_id = f"m0-acc-{run_id}-protected"
    response = server.http.post(
        "/api/v1/system/execution-mode",
        json={
            "target_mode": "LIVE",
            "reason": "M0 acceptance: protected-action audit demonstration",
        },
        headers=CLIENT | {"X-Correlation-ID": correlation_id},
    )
    rows = db.rows("SELECT * FROM audit_events WHERE correlation_id = ?", (correlation_id,))
    refusals: dict[str, str | None] = {"update": None, "delete": None}
    with contextlib.closing(sqlite3.connect(db.path, timeout=10)) as conn:
        for name, sql in (
            ("update", "UPDATE audit_events SET outcome = 'ALLOWED' WHERE correlation_id = ?"),
            ("delete", "DELETE FROM audit_events WHERE correlation_id = ?"),
        ):
            try:
                conn.execute(sql, (correlation_id,))
                conn.commit()
            except sqlite3.DatabaseError as exc:
                conn.rollback()
                refusals[name] = str(exc)
    after = db.rows("SELECT * FROM audit_events WHERE correlation_id = ?", (correlation_id,))
    mode = server.http.get("/api/v1/system/execution-mode").json()["mode"]
    error = response.json().get("error", {})
    ev.check(
        "protected action denied; AuditEvent persisted and append-only",
        response.status_code == 403
        and error.get("code") == "M0_LIVE_FORBIDDEN"
        and len(rows) == 1
        and rows[0]["event_type"] == "PROTECTED_ACTION"
        and rows[0]["outcome"] == "DENIED"
        and bool(refusals["update"])
        and bool(refusals["delete"])
        and after == rows
        and mode == "DRY_RUN",
        correlation_id=correlation_id,
        http_status=response.status_code,
        error=error,
        audit_event=rows[0] if rows else None,
        database_refusals=refusals,
        execution_mode_after=mode,
    )


def step_visual(ev: Evidence, server: Server, out: Path, channel: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "visual_check.py"),
            "--base-url",
            server.base_url,
            "--out",
            str(out / "visual"),
            "--channel",
            channel,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    (out / "visual.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    report_path = out / "visual" / "visual-report.json"
    report = json.loads(report_path.read_text("utf-8")) if report_path.exists() else {}
    ev.check(
        "UI renders all ten screens (landscape + portrait) with no external request or error",
        completed.returncode == 0,
        report="visual/visual-report.json",
        failures=report.get("failures"),
        browser=report.get("browser"),
    )


def step_egress(ev: Evidence, server: Server, label: str) -> None:
    snapshot = server.http.get("/api/v1/system/egress").json()
    check = next(
        c for c in server.http.get("/api/ready").json()["checks"] if c["name"] == "egress_guard"
    )
    ev.check(
        f"{label}: zero external connection attempts (process-wide egress guard)",
        snapshot["installed"] and snapshot["external_attempts"] == 0 and check["status"] == "PASS",
        egress=snapshot,
    )


def state_snapshot(db: Database) -> dict[str, Any]:
    return {
        "jobs_by_state": {
            r["state"]: r["n"]
            for r in db.rows("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
        },
        "job_attempts": db.value("SELECT COUNT(*) FROM job_attempts"),
        "audit_events": db.value("SELECT COUNT(*) FROM audit_events"),
    }


def stopped_cleanly(
    ev: Evidence, server: Server, data_dir: Path, label: str, expected_stops: int
) -> None:
    how = server.stop()
    stops = sum(1 for line in log_lines(data_dir) if line.get("msg") == "app.stopped")
    ev.check(
        f"{label}: stopped gracefully (lifespan shutdown ran)",
        how == "graceful" and stops == expected_stops,
        stop=how,
        app_stopped_log_lines=stops,
    )


def run(args: argparse.Namespace) -> Evidence:
    out: Path = args.out.resolve()
    data_dir = out / "data"
    if data_dir.exists():
        raise SystemExit(f"{data_dir} already exists; acceptance needs a fresh data directory")
    out.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("ICBM_")} | {
        "ICBM_DATA_DIR": str(data_dir),
        "ICBM_PORT": str(port),
        "ICBM_DIAGNOSTICS": "1",
        "ICBM_JOB_MAX_ATTEMPTS": str(MAX_ATTEMPTS),
        "ICBM_JOB_BACKOFF_BASE_S": "1",
        "ICBM_JOB_BACKOFF_FACTOR": "2",
        "ICBM_JOB_BACKOFF_MAX_S": "30",
        "ICBM_JOB_POLL_INTERVAL_S": str(POLL_INTERVAL_S),
        "PYTHONIOENCODING": "utf-8",
    }
    ev = Evidence()
    ev.data["started_at"] = now_iso()
    db = Database(data_dir / "icbm.db")
    servers: list[Server] = []
    try:
        step_environment(ev, run_id, port, data_dir)
        step_migrate(ev, env, db, out)

        first = Server(env, port, out / "server-run1.stdout.jsonl")
        servers.append(first)
        first.start()
        step_started(ev, first, "run 1")
        step_readiness(ev, first, "run 1")
        step_contracts(ev, first, "run 1")
        dead = step_dead_letter(ev, first, db, run_id)
        step_trace(ev, db, data_dir, out, dead)
        step_protected_action(ev, first, db, run_id)
        if args.visual:
            step_visual(ev, first, out, args.channel)
        step_egress(ev, first, "run 1")

        pending_id = f"m0-acc-{run_id}-restart"
        pending = first.http.post(
            "/api/v1/diagnostics/failing-job", headers=CLIENT | {"X-Correlation-ID": pending_id}
        ).json()["job_id"]
        wait_for(
            lambda: db.job(pending)["state"] == "RETRY_SCHEDULED", timeout_s=15, interval_s=0.05
        )
        stopped_cleanly(ev, first, data_dir, "run 1", expected_stops=1)
        at_stop = db.job(pending)
        ev.check(
            "queued work is durable across the stop",
            at_stop["state"] in {"QUEUED", "RETRY_SCHEDULED"}
            and 1 <= at_stop["attempt_count"] < MAX_ATTEMPTS,
            job_id=pending,
            correlation_id=pending_id,
            job_at_stop={k: at_stop[k] for k in ("state", "attempt_count", "next_attempt_at")},
        )

        second = Server(env, port, out / "server-run2.stdout.jsonl")
        servers.append(second)
        second.start()
        step_started(ev, second, "run 2 (restart)")
        step_readiness(ev, second, "run 2 (restart)")
        wait_for(lambda: db.job(pending)["state"] == "DEAD", timeout_s=30)
        resumed = db.job(pending)
        earlier = db.job(dead["job_id"])
        ev.check(
            "after restart the pending job resumes and dead-letters; earlier dead-letter unchanged",
            resumed["state"] == "DEAD"
            and [a["attempt_no"] for a in db.attempts(pending)] == list(range(1, MAX_ATTEMPTS + 1))
            and earlier["state"] == "DEAD"
            and earlier["attempt_count"] == MAX_ATTEMPTS,
            resumed_job={k: resumed[k] for k in ("state", "attempt_count", "finished_at")},
            earlier_dead_job={k: earlier[k] for k in ("state", "attempt_count", "finished_at")},
        )
        step_egress(ev, second, "run 2 (restart)")
        before = state_snapshot(db)
        stopped_cleanly(ev, second, data_dir, "run 2 (restart)", expected_stops=2)

        third = Server(env, port, out / "server-run3.stdout.jsonl")
        servers.append(third)
        third.start()
        step_started(ev, third, "run 3 (second restart)")
        step_readiness(ev, third, "run 3 (second restart)")
        step_contracts(ev, third, "run 3 (second restart)")
        after = state_snapshot(db)
        ev.check(
            "full restart repeats successfully with canonical state unchanged",
            before == after,
            state_before=before,
            state_after=after,
        )
        step_egress(ev, third, "run 3 (second restart)")
        stopped_cleanly(ev, third, data_dir, "run 3 (second restart)", expected_stops=3)
    except Exception as exc:
        ev.check(
            "acceptance run completed without an unexpected error",
            False,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        for server in servers:
            if server.running:
                server.stop()
    ev.data["finished_at"] = now_iso()
    return ev


def main() -> int:
    parser = argparse.ArgumentParser(description="ICBM-NEW M0 acceptance run")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "var" / "acceptance" / stamp)
    parser.add_argument("--visual", action="store_true", help="also run scripts/visual_check.py")
    parser.add_argument("--channel", default="msedge", help="Playwright browser channel")
    args = parser.parse_args()
    ev = run(args)
    result = "PASS" if ev.passed else "FAIL"
    evidence = {"result": result, **ev.data, "steps": ev.steps}
    out: Path = args.out.resolve()
    (out / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        f"\nM0 acceptance: {result} ({sum(s['result'] == 'PASS' for s in ev.steps)}/"
        f"{len(ev.steps)} checks) — evidence: {out / 'evidence.json'}"
    )
    return 0 if ev.passed else 1


if __name__ == "__main__":
    sys.exit(main())
