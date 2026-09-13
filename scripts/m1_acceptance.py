"""M1 acceptance: K홀세일 CONNECT against the real supplier (Issue #7).

Prerequisite: the operator has saved the K홀세일 login through the ICBM UI (공급처 관리 → 로그인
정보), which puts it in the OS secret store. This script never accepts, prints or stores a
credential; it reads secrets only in memory, to prove they appear nowhere in the artifacts.

Real-account traffic is minimised and bounded (Issue #7 comment 5653567880): exactly two real
logins are expected — the first connection and one forced-expiry refresh. Everything else reuses
the encrypted session. The run stops at the first failed step rather than retrying a login.

    run 1 (fresh data dir)  readiness, zero startup requests, connection test → real login #1,
                            protected-read proof with the unauthenticated control on the same
                            target, second test reuses the session, a second owner is refused
    run 2 (restart)         no proof carried over, zero startup requests, reuse without login
    expiry (no server)      the stored session is invalidated under the ownership lease
    run 3 (restart)         expired session → one bounded re-authentication (real login #2)
    after                   multi-encoding secret scan (counts only), schema/scope checks

Evidence (``evidence.json``) holds classifications, marker names, counters and counts — never
page content, cookies, headers or account data. Browser tracing/HAR/video stay disabled.
"""

import argparse
import contextlib
import json
import os
import platform
import secrets as random_secrets
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.connect.sessions import SESSIONS_DIR_NAME, SupplierSessionStore  # noqa: E402
from app.core.ownership import acquire_data_dir  # noqa: E402
from app.core.secrets import KeyringSecretStore  # noqa: E402
from app.system.secret_scan import scan  # noqa: E402
from integrations.suppliers import kmretail  # noqa: E402
from integrations.suppliers.transport.session_payload import (  # noqa: E402
    decode_session,
    encode_session,
)

KEY = kmretail.PROFILE.supplier_key
CLIENT = {"X-ICBM-Client": "m1-acceptance"}
EXPECTED_TABLES = {
    "alembic_version",
    "jobs",
    "job_attempts",
    "audit_events",
    "supplier_connections",
}


class StepFailed(RuntimeError):
    pass


@dataclass
class Evidence:
    steps: list[dict[str, Any]] = field(default_factory=list)

    def check(self, name: str, ok: bool, **facts: Any) -> None:
        self.steps.append({"step": name, "result": "PASS" if ok else "FAIL", **facts})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}", flush=True)
        if not ok:
            raise StepFailed(name)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()


class Server:
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
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{env['ICBM_PORT']}", timeout=10, trust_env=False
        )

    def wait_ready(self) -> httpx.Response:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise StepFailed(f"server exited with {self.proc.returncode}")
            with contextlib.suppress(httpx.TransportError):
                response = self.http.get("/api/ready")
                if response.status_code in (200, 503):
                    return response
            time.sleep(0.1)
        raise StepFailed("server never answered readiness")

    def stop(self) -> None:
        import signal

        self.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        self.proc.wait(60)
        self.http.close()
        self._output.close()

    def supplier(self) -> dict[str, Any]:
        [supplier] = self.http.get("/api/v1/connect/suppliers").json()
        return dict(supplier)

    def connection_test(self) -> dict[str, Any]:
        queued = self.http.post(f"/api/v1/connect/suppliers/{KEY}/test", headers=CLIENT)
        if queued.status_code != 202:
            raise StepFailed(f"connection test refused: {queued.json().get('error')}")
        job_id = queued.json()["job_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            job: dict[str, Any] = self.http.get(f"/api/v1/system/jobs/{job_id}").json()
            if job["state"] in ("SUCCEEDED", "DEAD"):
                return job
            time.sleep(0.25)
        raise StepFailed("connection test did not finish")

    def audit(self, correlation_id: str) -> list[dict[str, Any]]:
        events = self.http.get(
            "/api/v1/system/audit-events", params={"correlation_id": correlation_id}
        ).json()
        return list(reversed(events))


def _log_lines(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "logs" / "icbm.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _supplier_requests(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = ("request_kind", "transport", "result_class", "http_status", "target", "latency_ms")
    return [
        {k: line.get(k) for k in keep} | {"pid": line["pid"]}
        for line in lines
        if line.get("msg") == "supplier.request"
    ]


def _capability(ready: httpx.Response) -> dict[str, Any]:
    body = ready.json()
    [capability] = [c for c in body["capabilities"] if c["key"] == f"supplier:{KEY}"]
    return {
        "http_status": ready.status_code,
        "core": body["status"],
        "overall": body["overall"],
        "capability": capability["status"],
        "degraded_capabilities": body["degraded_capabilities"],
    }


def _proof(events: list[dict[str, Any]]) -> dict[str, Any]:
    verified = [e for e in events if e["event_type"] == "SUPPLIER_CONNECTION_VERIFIED"]
    if not verified:
        return {}
    details = verified[-1]["details"]
    return {
        k: details.get(k) for k in ("target", "control_result", "authenticated_result", "signals")
    }


def _run_1(ev: Evidence, env: dict[str, str], data_dir: Path, out: Path) -> None:
    server = Server(env, out / "run1.server.log")
    try:
        ready = server.wait_ready()
        cap = _capability(ready)
        ev.check(
            "run 1: core READY; K홀세일 is a DISCONNECTED capability until proven",
            cap["http_status"] == 200
            and cap["core"] == "PASS"
            and cap["capability"] == "DISCONNECTED"
            and cap["degraded_capabilities"] == [f"supplier:{KEY}"],
            readiness=cap,
        )
        ev.check(
            "run 1: startup made zero supplier requests (lazy connection)",
            _supplier_requests(_log_lines(data_dir)) == [],
        )
        job = server.connection_test()
        supplier = server.supplier()
        events = server.audit(job["correlation_id"])
        proof = _proof(events)
        ev.check(
            "run 1: first connection — one real login, protected read proven on the same target",
            job["state"] == "SUCCEEDED"
            and supplier["state"] == "READY"
            and supplier["real_login_attempts"] == 1
            and proof.get("control_result") == "LOGIN_REQUIRED"
            and proof.get("authenticated_result") == "AUTHENTICATED",
            job={
                k: job[k] for k in ("state", "attempt_count", "last_error_class", "last_error_code")
            },
            proof=proof,
            audit=[(e["event_type"], e["outcome"]) for e in events],
            counters={
                k: supplier[k]
                for k in ("real_login_attempts", "session_reuse_count", "reauth_count")
            },
        )
        requests = _supplier_requests(_log_lines(data_dir))
        control = [r for r in requests if r["request_kind"] == "CONTROL_READ"]
        ev.check(
            "run 1: negative control — unauthenticated read of the same target answered "
            "HTTP 200 yet classified LOGIN_REQUIRED",
            bool(control) and control[0]["target"] == kmretail.PROTECTED_TARGET,
            control_request=control[:1],
        )
        cap = _capability(server.http.get("/api/ready"))
        ev.check(
            "run 1: capability READY after the proof",
            cap["capability"] == "READY" and cap["degraded_capabilities"] == [],
            readiness=cap,
        )
        job = server.connection_test()
        supplier = server.supplier()
        ev.check(
            "run 1: second test reuses the session — no login",
            job["state"] == "SUCCEEDED"
            and supplier["real_login_attempts"] == 1
            and supplier["session_reuse_count"] == 1,
            audit=[(e["event_type"], e["outcome"]) for e in server.audit(job["correlation_id"])],
        )
        contender = subprocess.run(
            [sys.executable, "-m", "app", "serve"],
            cwd=REPO_ROOT,
            env=env | {"ICBM_PORT": str(_free_port())},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        ev.check(
            "run 1: a second ICBM process on the same data directory is refused (Issue #4)",
            contender.returncode == 3 and contender.stderr.startswith("DATA_DIR_IN_USE"),
            exit_code=contender.returncode,
        )
        egress = server.http.get("/api/v1/system/egress").json()
        ev.check(
            "run 1: no unauthorised egress; supplier egress only through its grant",
            egress["external_attempts"] == 0
            and set(egress["granted_events"]) <= {f"supplier:{KEY}"},
            egress={k: egress[k] for k in ("policy", "external_attempts", "granted_events")},
        )
    finally:
        server.stop()


def _run_2(ev: Evidence, env: dict[str, str], data_dir: Path, out: Path) -> None:
    before = len(_supplier_requests(_log_lines(data_dir)))
    server = Server(env, out / "run2.server.log")
    try:
        cap = _capability(server.wait_ready())
        supplier = server.supplier()
        ev.check(
            "run 2 (restart): identity kept, no proof carried over, zero startup requests",
            supplier["connection_id"] is not None
            and supplier["state"] == "DISCONNECTED"
            and supplier["session_state"] == "STORED"
            and cap["capability"] == "DISCONNECTED"
            and len(_supplier_requests(_log_lines(data_dir))) == before,
            readiness=cap,
        )
        job = server.connection_test()
        supplier = server.supplier()
        ev.check(
            "run 2 (restart): the encrypted session is reused — no login",
            job["state"] == "SUCCEEDED"
            and supplier["state"] == "READY"
            and supplier["real_login_attempts"] == 1
            and supplier["session_reuse_count"] == 2,
            proof=_proof(server.audit(job["correlation_id"])),
        )
    finally:
        server.stop()


def _expire_session(ev: Evidence, data_dir: Path) -> None:
    """Invalidate the stored session the way a supplier-side expiry would, under ownership."""
    with acquire_data_dir(data_dir, app_version="m1-acceptance"):
        store = SupplierSessionStore(data_dir / SESSIONS_DIR_NAME, KeyringSecretStore())
        payload = store.load(KEY)
        if payload is None:
            raise StepFailed("no stored session to expire")
        cookies, user_agent = decode_session(payload)
        stale = [c | {"value": random_secrets.token_hex(16)} for c in cookies]
        store.save(
            KEY,
            encode_session(stale, user_agent=user_agent, hosts=kmretail.PROFILE.egress_hosts),
        )
    ev.check(
        "expiry: stored session invalidated (cookie values replaced)", True, cookies=len(stale)
    )


def _run_3(ev: Evidence, env: dict[str, str], data_dir: Path, out: Path) -> None:
    server = Server(env, out / "run3.server.log")
    try:
        server.wait_ready()
        job = server.connection_test()
        supplier = server.supplier()
        events = server.audit(job["correlation_id"])
        types = [e["event_type"] for e in events]
        ev.check(
            "run 3: expired session → exactly one bounded re-authentication → proven READY",
            job["state"] == "SUCCEEDED"
            and supplier["state"] == "READY"
            and supplier["real_login_attempts"] == 2
            and supplier["reauth_count"] == 1
            and "SUPPLIER_SESSION_REFRESHED" in types,
            proof=_proof(events),
            audit=[(e["event_type"], e["outcome"]) for e in events],
        )
    finally:
        server.stop()


def _after(ev: Evidence, data_dir: Path, out: Path) -> None:
    keyring = KeyringSecretStore()
    username = keyring.get(f"supplier:{KEY}:username") or ""
    password = keyring.get(f"supplier:{KEY}:password") or ""
    with acquire_data_dir(data_dir, app_version="m1-acceptance"):
        payload = SupplierSessionStore(data_dir / SESSIONS_DIR_NAME, keyring).load(KEY)
    cookies, _ = decode_session(payload) if payload else ([], "")
    secrets = {"username": username, "password": password} | {
        f"cookie:{c['name']}": c["value"] for c in cookies if len(c["value"]) >= 6
    }
    report = scan([data_dir, out], secrets)
    ev.check(
        "secret scan: no credential or session value in any artifact, in any of 5 encodings",
        report["total_hits"] == 0,
        files_scanned=report["files_scanned"],
        hits=report["hits"],
    )
    with contextlib.closing(
        sqlite3.connect(f"{(data_dir / 'icbm.db').as_uri()}?mode=ro", uri=True)
    ) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = db.execute("SELECT COUNT(*) FROM supplier_connections").fetchone()[0]
    ev.check(
        "scope: no ProductFacts/product tables; one supplier connection identity",
        tables == EXPECTED_TABLES and rows == 1,
        tables=sorted(tables),
        supplier_connections=rows,
    )
    requests = _supplier_requests(_log_lines(data_dir))
    kinds: dict[str, int] = {}
    for r in requests:
        kinds[f"{r['request_kind']}/{r['transport']}"] = (
            kinds.get(f"{r['request_kind']}/{r['transport']}", 0) + 1
        )
    ev.check(
        "external-call scope: only the protected target and the login page were requested",
        {r["target"] for r in requests} <= {kmretail.PROTECTED_TARGET, kmretail.LOGIN_PATH}
        and kinds.get("AUTHENTICATE/BROWSER") == 2,
        requests_by_kind=kinds,
        requests=requests,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--channel", default="msedge", help="Playwright browser channel")
    args = parser.parse_args()
    out: Path = args.out.resolve()
    data_dir = out / "data"
    if data_dir.exists():
        print("refusing to reuse an existing acceptance data directory", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    keyring = KeyringSecretStore()
    if not (keyring.get(f"supplier:{KEY}:username") and keyring.get(f"supplier:{KEY}:password")):
        print(
            "save the K홀세일 login in the ICBM UI first (공급처 관리 → 로그인 정보)",
            file=sys.stderr,
        )
        return 2

    env = {k: v for k, v in os.environ.items() if not k.startswith("ICBM_")} | {
        "ICBM_DATA_DIR": str(data_dir),
        "ICBM_PORT": str(_free_port()),
        "ICBM_SECRET_BACKEND": "os",
        "ICBM_BROWSER_CHANNEL": args.channel,
        "PYTHONIOENCODING": "utf-8",
    }
    ev = Evidence()
    started = datetime.now(UTC)
    result = "FAIL"
    try:
        upgrade = subprocess.run(
            [sys.executable, "-m", "app", "db", "upgrade"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        ev.check("fresh data directory migrated to head", upgrade.returncode == 0)
        _run_1(ev, env, data_dir, out)
        _run_2(ev, env, data_dir, out)
        _expire_session(ev, data_dir)
        _run_3(ev, env, data_dir, out)
        _after(ev, data_dir, out)
        result = "PASS"
    except StepFailed as exc:
        print(f"stopped at: {exc}", file=sys.stderr)
    finally:
        evidence = {
            "result": result,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "environment": {
                "git_commit": _git("rev-parse", "HEAD"),
                "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                "git_worktree_clean": _git("status", "--porcelain") == "",
                "python": platform.python_version(),
                "platform": platform.platform(),
                "browser_channel": args.channel,
                "supplier": {"key": KEY, "base_url": kmretail.PROFILE.base_url},
                "tracing_har_video": "disabled",
            },
            "steps": ev.steps,
        }
        (out / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), "utf-8"
        )
    passed = sum(1 for s in ev.steps if s["result"] == "PASS")
    print(f"M1 acceptance: {result} ({passed}/{len(ev.steps)} checks) — {out / 'evidence.json'}")
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
