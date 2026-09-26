"""The Gate 3 area 3 populated visual and responsive acceptance run (ADR-0018 §9).

One run, over one fresh dedicated root:

1. the checkout is probed (``scripts.m4accept.checkout``): its commit is the report's ``code_sha``
   and the digest of the code the application executes and serves is its ``code_digest``;
2. the populated first-vertical scenario is written through the application's own owners
   (``scenario.py``) into ``<root>/data``, in-process and provider-zero;
3. the application is served from that root by ``python -m app serve`` on a free loopback port;
4. a real browser opens every required surface (``app.live.visual.REQUIRED_TARGETS``) at every
   required viewport (``REQUIRED_VIEWPORTS``), measures it (``measure.js``), judges it
   (``checker.judge``) and saves a full-page screenshot under ``<root>/screens``;
5. every request the browser makes is classified — any non-loopback request fails the run — and
   the served application's own egress counter must stay at zero;
6. the checkout is probed again, and the sealed report is written to
   ``<root>/g3-visual-report.json``.

The report holds identities, codes, counts and digests only — no product text, no URL, no secret —
and verifies against ``app.live.visual.verify_report``. A PASSED report is still not a proof: only
the reviewed ``icbm live record-visual-acceptance`` command records one.
"""

import contextlib
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx
from fastapi.testclient import TestClient

from app.config import AppConfig, ConfigError, configured_data_dir, default_data_dir
from app.core.code_identity import code_digest
from app.db.migrate import head_revision, upgrade_to_head
from app.live import visual
from app.main import create_app
from scripts.g3visual.checker import MEASURE, judge
from scripts.g3visual.scenario import DECLARED_SEAMS, populate
from scripts.m4accept.checkout import probe_checkout
from scripts.m4accept.root import REPO_ROOT, names_preserved_campaign

REPORT: Final = "g3-visual-report.json"
DATA: Final = "data"
SCREENS: Final = "screens"
LOCAL: Final = "127.0.0.1"
_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+|//\S+", re.I)


class RootRefused(RuntimeError):
    pass


def claim_root(root: Path) -> Path:
    """A fresh root dedicated to this run: absolute, outside the repository and every ICBM data
    directory, never a preserved campaign, and new or empty."""
    if not root.is_absolute():
        raise RootRefused("the root is an absolute path")
    if names_preserved_campaign(root):
        raise RootRefused("the root names a preserved campaign")
    resolved = root.resolve()
    reserved = [REPO_ROOT.resolve()]
    for resolve in (default_data_dir, configured_data_dir):
        with contextlib.suppress(ConfigError):
            reserved.append(resolve().resolve())
    for other in reserved:
        if resolved == other or resolved.is_relative_to(other) or other.is_relative_to(resolved):
            raise RootRefused("the root overlaps the repository or an ICBM data directory")
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise RootRefused("the root is not fresh")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOCAL, 0))
        return int(probe.getsockname()[1])


def _scrub(text: str) -> str:
    """A console or page error as the report may hold it: no URL, ASCII, short."""
    plain = _URL.sub("<url>", text)
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in plain)[:160]


class Server:
    """``python -m app serve`` over the run's data root, on a free loopback port."""

    def __init__(self, data: Path, log: Path) -> None:
        self.port = _free_port()
        self.base_url = f"http://{LOCAL}:{self.port}"
        self._log = log
        self._env = {
            **os.environ,
            "ICBM_DATA_DIR": str(data),
            "ICBM_PORT": str(self.port),
            "ICBM_SECRET_BACKEND": "memory",
        }
        self._process: subprocess.Popen[bytes] | None = None
        self.http = httpx.Client(base_url=self.base_url, timeout=15, trust_env=False)

    def __enter__(self) -> "Server":
        self._out = self._log.open("wb")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self._process = subprocess.Popen(
            [sys.executable, "-m", "app", "serve"],
            cwd=REPO_ROOT,
            env=self._env,
            stdout=self._out,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(f"the server exited with {self._process.returncode}")
            with contextlib.suppress(httpx.TransportError):
                if self.http.get("/api/health").status_code == 200:
                    return self
            time.sleep(0.25)
        raise RuntimeError("the server did not become healthy in time")

    def external_attempts(self) -> int:
        found = self.http.get("/api/v1/system/egress").json()
        return int(found["external_attempts"])

    def __exit__(self, *_: object) -> None:
        assert self._process is not None
        self._process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        try:
            self._process.wait(30)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self.http.close()
        self._out.close()


def _populate(data: Path) -> Any:
    config = AppConfig(data_dir=data, secret_backend="memory")
    upgrade_to_head(config.database_url)
    with TestClient(create_app(config), base_url=f"http://{LOCAL}") as api:
        return populate(api.app.state.container, api)


def _browse(
    base_url: str, routes: Mapping[str, str], screens: Path, channel: str
) -> tuple[str, list[dict[str, Any]], int, list[str], list[str]]:
    from playwright.sync_api import sync_playwright

    results: list[dict[str, Any]] = []
    external_total = 0
    console_all: list[str] = []
    page_all: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        try:
            version = f"{channel} {browser.version}"
            for viewport, (width, height) in visual.REQUIRED_VIEWPORTS.items():
                (screens / viewport).mkdir(parents=True, exist_ok=True)
                for target in visual.REQUIRED_TARGETS:
                    page = browser.new_page(viewport={"width": width, "height": height})
                    console: list[str] = []
                    errors: list[str] = []
                    external = [0]

                    def on_request(request: Any, count: list[int] = external) -> None:
                        url = str(request.url)
                        if not (url.startswith(base_url) or url.startswith(("data:", "blob:"))):
                            count[0] += 1

                    page.on(
                        "console",
                        lambda message, sink=console: (
                            sink.append(_scrub(message.text)) if message.type == "error" else None
                        ),
                    )
                    page.on("pageerror", lambda error, sink=errors: sink.append(_scrub(str(error))))
                    page.on("request", on_request)
                    page.goto(f"{base_url}/{routes[target.name]}")
                    page.wait_for_selector(
                        f'#content[data-page="{target.screen}"]:not([aria-busy])', timeout=30_000
                    )
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(400)
                    measurement = page.evaluate(
                        MEASURE,
                        [list(visual.STATE_SELECTORS), list(target.populated), target.screen],
                    )
                    shot = screens / viewport / f"{target.name}.png"
                    page.screenshot(path=str(shot), full_page=True)
                    checks = judge(
                        measurement,
                        target,
                        console_errors=[*console, *errors],
                        external_requests=external[0],
                    )
                    results.append(
                        {
                            "target": target.name,
                            "screen": target.screen,
                            "viewport": viewport,
                            "route": routes[target.name],
                            "screenshot_sha256": hashlib.sha256(shot.read_bytes()).hexdigest(),
                            "state_count": len(measurement.get("states") or []),
                            "populated": measurement.get("populated") or {},
                            "checks": checks,
                        }
                    )
                    external_total += external[0]
                    console_all.extend(console)
                    page_all.extend(errors)
                    page.close()
        finally:
            browser.close()
    return version, results, external_total, console_all, page_all


def run(root: Path, *, channel: str, allow_dirty: bool = False) -> dict[str, Any]:
    checkout = probe_checkout()
    if checkout.problems and not allow_dirty:
        raise RootRefused("the checkout is not clean: " + "; ".join(checkout.problems))
    root = claim_root(root)
    data = root / DATA
    head = head_revision() or ""
    ui_dir = AppConfig(data_dir=data, secret_backend="memory").ui_dir
    digest = code_digest(ui_dir)
    populated = _populate(data)
    with Server(data, root / "server.log") as server:
        version, results, external, console, page_errors = _browse(
            server.base_url, populated.routes(), root / SCREENS, channel
        )
        after = server.external_attempts()
    again = probe_checkout()
    clean = not checkout.problems and again == checkout and code_digest(ui_dir) == digest
    failures = sorted(
        f"{name}:{r['target']}@{r['viewport']}"
        for r in results
        for name, outcome in r["checks"].items()
        if not outcome["passed"]
    )
    report: dict[str, Any] = {
        "report_version": visual.REPORT_VERSION,
        "harness_version": visual.HARNESS_VERSION,
        "scenario": visual.SCENARIO,
        "code_sha": checkout.code_sha or "",
        "code_digest": digest,
        "schema_head": head,
        "checkout_clean": clean,
        "browser": version,
        "declared_seams": list(DECLARED_SEAMS),
        "state_selectors": list(visual.STATE_SELECTORS),
        "viewports": {name: list(size) for name, size in visual.REQUIRED_VIEWPORTS.items()},
        "results": results,
        "external_request_count": external,
        "console_errors": console,
        "page_errors": page_errors,
        "server_external_attempts": after,
        "failures": failures,
    }
    passed = (
        not failures
        and clean
        and external == 0
        and not console
        and not page_errors
        and report["server_external_attempts"] == 0
    )
    report["verdict"] = "PASSED" if passed else "FAILED"
    report[visual.DIGEST_FIELD] = visual.report_digest(report)
    (root / REPORT).write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n", "utf-8"
    )
    return report


__all__ = ["REPORT", "RootRefused", "claim_root", "run"]
