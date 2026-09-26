"""A sealed Gate 3 area 3 report for tests: every required surface at every required viewport."""

import hashlib
from typing import Any

from app.live import visual

SHA = "a" * 40


def result(target: str, viewport: str, **overrides: Any) -> dict[str, Any]:
    surface = next(t for t in visual.REQUIRED_TARGETS if t.name == target)
    values: dict[str, Any] = {
        "target": target,
        "screen": surface.screen,
        "viewport": viewport,
        "route": f"#/{surface.screen}",
        "screenshot_sha256": hashlib.sha256(f"{target}@{viewport}".encode()).hexdigest(),
        "state_count": 12,
        "populated": dict.fromkeys(surface.populated, 1),
        "checks": {check: {"passed": True, "failures": []} for check in visual.REQUIRED_CHECKS},
    }
    values.update(overrides)
    return values


def sealed(
    *,
    code_digest: str,
    schema_head: str,
    code_sha: str = SHA,
    results: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "report_version": visual.REPORT_VERSION,
        "harness_version": visual.HARNESS_VERSION,
        "scenario": visual.SCENARIO,
        "code_sha": code_sha,
        "code_digest": code_digest,
        "schema_head": schema_head,
        "checkout_clean": True,
        "browser": "msedge-test",
        "state_selectors": list(visual.STATE_SELECTORS),
        "viewports": {name: list(size) for name, size in visual.REQUIRED_VIEWPORTS.items()},
        "results": results
        if results is not None
        else [
            result(target.name, viewport)
            for target in visual.REQUIRED_TARGETS
            for viewport in visual.REQUIRED_VIEWPORTS
        ],
        "external_request_count": 0,
        "console_errors": [],
        "page_errors": [],
        "server_external_attempts": 0,
        "verdict": "PASSED",
        "failures": [],
    }
    report.update(overrides)
    return reseal(report)


def reseal(report: dict[str, Any]) -> dict[str, Any]:
    report[visual.DIGEST_FIELD] = visual.report_digest(report)
    return report
