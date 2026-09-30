"""The test-browser owner always applies the loopback-only rule (owner amendment 5909645067 §3)."""

from pathlib import Path
from typing import Any

import pytest

from tests.support.browser import NETWORK_BLOCK, launch_browser, launch_extension_context


class _Chromium:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def launch(self, **options: Any) -> str:
        self.calls.append(options)
        return "browser"

    def launch_persistent_context(self, user_data_dir: str, **options: Any) -> str:
        self.calls.append({"user_data_dir": user_data_dir, **options})
        return "context"


class _Playwright:
    def __init__(self) -> None:
        self.chromium = _Chromium()


def test_every_launch_starts_with_the_network_block() -> None:
    playwright = _Playwright()
    launch_browser(playwright, args=["--window-size=800,600"])  # type: ignore[arg-type]
    launch_extension_context(
        playwright,  # type: ignore[arg-type]
        Path("profile"),
        extension_root=Path("ui/extension"),
    )
    plain, extension = playwright.chromium.calls
    assert plain["args"] == [NETWORK_BLOCK, "--window-size=800,600"]
    assert extension["args"][0] == NETWORK_BLOCK
    assert any(a.startswith("--load-extension=") for a in extension["args"])
    assert plain["headless"] is extension["headless"] is True
    assert "~NOTFOUND" in NETWORK_BLOCK and "EXCLUDE 127.0.0.1" in NETWORK_BLOCK


@pytest.mark.parametrize(
    "argument",
    ["--host-resolver-rules=MAP * 127.0.0.1", "--host-rules=MAP example.invalid 10.0.0.1"],
)
def test_a_caller_cannot_bring_its_own_host_resolution(argument: str) -> None:
    playwright = _Playwright()
    with pytest.raises(ValueError):
        launch_browser(playwright, args=[argument])  # type: ignore[arg-type]
    assert playwright.chromium.calls == []
