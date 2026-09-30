"""The one owner of every browser a repository test launches (Issue #126 ``5909188774`` F-1;
owner amendment ``5909645067`` §3).

A test browser never leaves this machine. Every launch carries ``NETWORK_BLOCK``, so the browser
resolves no host name at all and only the literal loopback address is reachable. A test still
loads a page "at" any host by answering its requests from a route, which happens before a name is
resolved; a request the test did not answer — the follow-up of a routed redirect is never offered
to a route handler — fails in the resolver instead of reaching a real host. That is how two
requests once reached a supplier's host (``documents/acceptance/adaptive/EXTENSION-E1.md`` §6).

No test calls Playwright's ``launch`` or ``launch_persistent_context`` itself:
``tests/contracts/test_repository_rules.py`` refuses a launch anywhere else in the test tree.
"""

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Playwright

NETWORK_BLOCK = "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"
BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"


def _arguments(extra: Sequence[str]) -> list[str]:
    # A caller can add arguments and can never replace or re-map the resolver rule.
    for argument in extra:
        if argument.startswith("--host-resolver-rules") or argument.startswith("--host-rules"):
            raise ValueError("a test browser's host resolution is owned by tests.support.browser")
    return [NETWORK_BLOCK, *extra]


def launch_browser(
    playwright: Playwright, *, channel: str = BROWSER_CHANNEL, args: Sequence[str] = ()
) -> Browser:
    """A headless browser that can reach the loopback and nothing else."""
    return playwright.chromium.launch(channel=channel, headless=True, args=_arguments(args))


def launch_extension_context(
    playwright: Playwright,
    user_data_dir: Path,
    *,
    extension_root: Path,
    channel: str = BROWSER_CHANNEL,
    **options: Any,
) -> BrowserContext:
    """A persistent context with one unpacked extension loaded, and the same network block."""
    return playwright.chromium.launch_persistent_context(
        str(user_data_dir),
        channel=channel,
        headless=True,
        args=_arguments(
            (
                "--headless=new",
                f"--disable-extensions-except={extension_root}",
                f"--load-extension={extension_root}",
            )
        ),
        **options,
    )
