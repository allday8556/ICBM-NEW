"""Issue #7 §13 regression floor for Issue #4: a child process — including the supplier-login
browser — never keeps data-directory ownership alive after its ICBM owner dies.

The owner is a real process that takes the lease, starts the child, and is then terminated
abruptly (TerminateProcess / SIGKILL). The lock must become acquirable within the bounded poll
while the child is still running.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.core.ownership import DataDirInUseError, acquire_data_dir

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"

OWNER = textwrap.dedent(
    """
    import os, subprocess, sys, time
    from pathlib import Path
    from app.core.ownership import acquire_data_dir

    lease = acquire_data_dir(Path(sys.argv[1]), app_version="test")
    if sys.argv[2] == "subprocess":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        child_pid = child.pid
    else:
        from playwright.sync_api import sync_playwright
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(channel=sys.argv[3], headless=True)
        except Exception as exc:
            print("SKIP", type(exc).__name__, flush=True)
            raise SystemExit(0)
        child_pid = -1
    print(os.getpid(), child_pid, flush=True)
    time.sleep(120)
    """
)


def _acquirable_within(data_dir: Path, timeout_s: float) -> float:
    started = time.monotonic()
    while True:
        try:
            with acquire_data_dir(data_dir, app_version="probe"):
                return time.monotonic() - started
        except DataDirInUseError:
            if time.monotonic() - started > timeout_s:
                raise
            time.sleep(0.05)


def _kill(pid: int) -> None:
    os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)


@pytest.mark.parametrize("child", ["subprocess", "browser"])
def test_an_owners_children_never_keep_ownership_alive(tmp_path: Path, child: str) -> None:
    owner = subprocess.Popen(
        [sys.executable, "-c", OWNER, str(tmp_path), child, BROWSER_CHANNEL],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        first = owner.stdout.readline().split()
        if first and first[0] == "SKIP":
            pytest.skip(f"no {BROWSER_CHANNEL} browser to launch here ({first[1:]})")
        owner_pid, child_pid = int(first[0]), int(first[1])
        with pytest.raises(DataDirInUseError):
            acquire_data_dir(tmp_path, app_version="probe")
        _kill(owner_pid)
        assert _acquirable_within(tmp_path, timeout_s=5.0) <= 5.0
        if child_pid > 0:
            _kill(child_pid)  # succeeds only because the child outlived its owner
    finally:
        owner.kill()
        owner.wait(30)
