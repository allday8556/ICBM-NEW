"""The populated visual and responsive acceptance (ADR-0018 §9; Gate 3 area 3).

``VISUAL_ACCEPTANCE_RECORDED`` is never asserted. It holds only while a **reviewed** record exists
for **exactly the accepted code SHA** the process runs at — the commit its checkout is at — and, as
an additional integrity binding, exactly its running code digest (``app.core.code_identity``), at
exactly its schema head. Any new commit, a documents-only, tests-only or harness-only one included,
makes every earlier record stale (ADR-0018 §9). A record exists only through this module's one
path:

1. a harness (``scripts/g3_visual_acceptance.py``) drives the populated first-vertical scenario in a
   real browser over a served application, at every required viewport, through every required
   screen, and writes one digest-sealed report;
2. ``verify_report`` checks that report against the **contract below** — the required screens and
   viewports cannot be dropped, every required check must be present and passed, the required
   populated state must have rendered, no external request, console or page error may exist, and
   nothing in it may carry secret material or a URL;
3. ``VisualAcceptanceService.record`` records it only when, in addition, its commit is the commit
   this process runs at, its code digest is the running code's, its schema head is the current
   head, and a reviewer and a review reference (a GitHub comment identity) are named.

A passing browser run alone never records anything, and no HTTP route or UI action reaches this
module: the only caller is the ``icbm live record-visual-acceptance`` command (a repository rule
keeps it so). A cosmetic difference from the approved prototype is not a failure; a hidden,
truncated, covered or clipped server-owned state is.
"""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from app.core.errors import InputValidationError
from app.live.model import (
    VISUAL_CODE_NOT_CURRENT,
    VISUAL_COMMIT_NOT_CHECKED_OUT,
    VISUAL_REPORT_INVALID,
    VISUAL_REVIEW_MISSING,
    VISUAL_SCHEMA_NOT_CURRENT,
)
from app.live.store import LiveAuthorityStore
from app.register.sanitize import problems

REPORT_VERSION: Final = "g3-visual-report/v1"
HARNESS_VERSION: Final = "g3-visual-acceptance/v1"
SCENARIO: Final = "g3-populated-first-vertical/v1"
DIGEST_FIELD: Final = "report_digest"

# ADR-0018 §9's accepted viewport set: at least the two established sizes. More may be added.
REQUIRED_VIEWPORTS: Final[Mapping[str, tuple[int, int]]] = {
    "landscape-1920x1080": (1920, 1080),
    "portrait-1080x1920": (1080, 1920),
}


@dataclass(frozen=True)
class Target:
    """One required populated surface: a screen, and the server-owned state that must render."""

    name: str
    screen: str
    populated: tuple[str, ...]


# ADR-0018 §9's populated first-vertical surfaces. Each ``populated`` selector must match at least
# one rendered element, so an empty or unpopulated screen can never pass.
REQUIRED_TARGETS: Final[tuple[Target, ...]] = (
    Target(
        "collect",
        "collect",
        ('[data-role="recent-runs"] tr[data-run]', '[data-role="run-focus"] [data-facts-status]'),
    ),
    Target(
        "db",
        "db",
        (
            '[data-role="product-detail"] .db-member[data-member]',
            '[data-role="product-detail"] [data-fact="images"]',
            '[data-role="product-review"] [data-role="review-items"]',
        ),
    ),
    Target(
        "register",
        "register",
        (
            ".register-unit[data-unit]",
            ".register-preflight[data-preflight]",
            ".register-scope[data-scope-state]",
            '[data-role="register-review"]',
            ".register-canary[data-canary]",
            '[data-role="live-brake"][data-brake-state]',
            '[data-role="live-grants"] [data-grant-state]',
            '[data-role="live-proofs"] [data-proof]',
        ),
    ),
    Target("dashboard", "dashboard", ('[data-role="dashboard-review"] tr[data-kind][data-state]',)),
    Target("soldout", "soldout", ('[data-role="soldout-review"] tr[data-kind][data-state]',)),
    Target("settings-policy", "settings", (".target-policy[data-account] [data-policy-state]",)),
    Target("settings-metadata", "settings", (".category-metadata [data-metadata-key]",)),
)

# Every element carrying server-owned state: each one rendered must be visible, untruncated,
# uncovered and inside the viewport's width. A new state attribute joins this list.
STATE_SELECTORS: Final[tuple[str, ...]] = (
    "[data-reason]",
    "[data-state]",
    "[data-review-state]",
    "[data-requirement]",
    "[data-scope-state]",
    "[data-preflight]",
    "[data-outcome]",
    "[data-facts-status]",
    "[data-policy-state]",
    "[data-canary]",
    "[data-capability]",
    "[data-brake-state]",
    "[data-grant-state]",
    "[data-proof]",
    "[data-missing]",
    "[data-coverage]",
    ".chip",
    ".note",
)

REQUIRED_CHECKS: Final[tuple[str, ...]] = (
    "rendered",
    "populated",
    "no_console_error",
    "no_external_request",
    "no_horizontal_overflow",
    "state_visible",
    "state_not_truncated",
    "state_not_covered",
    "title_help_icon",
    "navigation_intact",
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_COMMENT_ID = re.compile(r"^[0-9]{6,20}$")


def canonical(report: Mapping[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in report.items() if key != DIGEST_FIELD},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def report_digest(report: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(report).encode("ascii")).hexdigest()


def verify_report(report: Mapping[str, Any]) -> list[str]:
    """Every way ``report`` falls short of the contract, as codes. Empty only for a complete,
    sealed, passing report of every required surface at every required viewport."""
    found: list[str] = []
    if report.get(DIGEST_FIELD) != report_digest(report):
        found.append("digest_mismatch")
    for key, expected in (
        ("report_version", REPORT_VERSION),
        ("harness_version", HARNESS_VERSION),
        ("scenario", SCENARIO),
    ):
        if report.get(key) != expected:
            found.append(f"{key}_unexpected")
    if not _SHA.match(str(report.get("code_sha", ""))):
        found.append("code_sha_missing")
    if not _HEX64.match(str(report.get("code_digest", ""))):
        found.append("code_digest_missing")
    if not report.get("schema_head"):
        found.append("schema_head_missing")
    if report.get("checkout_clean") is not True:
        found.append("checkout_not_clean")
    if list(report.get("state_selectors") or ()) != list(STATE_SELECTORS):
        found.append("state_selectors_not_the_contract")
    viewports = report.get("viewports")
    viewports = viewports if isinstance(viewports, Mapping) else {}
    for name, size in REQUIRED_VIEWPORTS.items():
        if list(viewports.get(name) or ()) != list(size):
            found.append(f"viewport_missing:{name}")
    results = report.get("results") or []
    seen: dict[tuple[str, str], int] = {}
    for result in results:
        pair = (str(result.get("target")), str(result.get("viewport")))
        seen[pair] = seen.get(pair, 0) + 1
    # Exactly one result for every required surface at every viewport the run declares (the two
    # required ones included): none can be dropped, and none can be counted twice.
    for surface in REQUIRED_TARGETS:
        for viewport in sorted(set(REQUIRED_VIEWPORTS) | set(viewports)):
            if seen.get((surface.name, viewport), 0) != 1:
                found.append(f"result_missing:{surface.name}@{viewport}")
    required = {t.name: t for t in REQUIRED_TARGETS}
    for result in results:
        where = f"{result.get('target')}@{result.get('viewport')}"
        target = required.get(str(result.get("target")))
        if target is None:
            continue
        checks = result.get("checks") or {}
        for check in REQUIRED_CHECKS:
            outcome = checks.get(check)
            if not isinstance(outcome, Mapping):
                found.append(f"check_missing:{check}:{where}")
            elif outcome.get("passed") is not True or outcome.get("failures"):
                found.append(f"check_failed:{check}:{where}")
        populated = result.get("populated") or {}
        for selector in target.populated:
            if not isinstance(populated.get(selector), int) or populated[selector] < 1:
                found.append(f"not_populated:{target.name}:{where}")
        if not _HEX64.match(str(result.get("screenshot_sha256", ""))):
            found.append(f"screenshot_missing:{where}")
        if not isinstance(result.get("state_count"), int) or result["state_count"] < 1:
            found.append(f"no_state_checked:{where}")
    if report.get("external_request_count") != 0:
        found.append("external_requests")
    if report.get("console_errors") or report.get("page_errors"):
        found.append("console_or_page_errors")
    if report.get("server_external_attempts") != 0:
        found.append("server_egress")
    if report.get("verdict") != "PASSED" or report.get("failures"):
        found.append("verdict_not_passed")
    if problems(dict(report)):
        found.append("unsanitized")
    return sorted(set(found))


def evidence_of(report: Mapping[str, Any]) -> dict[str, Any]:
    """The sanitized summary kept with a record: identities, sets, counts and digests only."""
    return {
        "report_version": report.get("report_version"),
        "browser": report.get("browser"),
        "viewports": report.get("viewports"),
        "targets": sorted({str(r.get("target")) for r in report.get("results") or []}),
        "screenshots": sorted(
            f"{r.get('target')}@{r.get('viewport')}:{r.get('screenshot_sha256')}"
            for r in report.get("results") or []
        ),
        "state_counts": {
            f"{r.get('target')}@{r.get('viewport')}": r.get("state_count")
            for r in report.get("results") or []
        },
    }


class VisualAcceptanceService:
    def __init__(
        self,
        *,
        store: LiveAuthorityStore,
        code_sha: Callable[[], str | None],
        code_identity: Callable[[], str],
        schema_head: Callable[[], str | None],
    ) -> None:
        self._store = store
        self._sha = code_sha
        self._code = code_identity
        self._head = schema_head

    def record(
        self,
        report: Mapping[str, Any],
        *,
        approved_by: str,
        authorization_ref: str,
        actor: str,
        correlation_id: str,
    ) -> str:
        """Record one reviewed acceptance, or refuse and record nothing."""
        found = verify_report(report)
        if found:
            raise InputValidationError(
                VISUAL_REPORT_INVALID,
                "the report does not satisfy the visual acceptance contract",
                details={"problems": found},
            )
        if report["code_digest"] != self._code():
            raise InputValidationError(
                VISUAL_CODE_NOT_CURRENT, "the report accepted other code than this application runs"
            )
        if report["schema_head"] != self._head():
            raise InputValidationError(
                VISUAL_SCHEMA_NOT_CURRENT, "the report was taken at another schema head"
            )
        sha = self._sha()
        if not sha or sha != report["code_sha"]:
            raise InputValidationError(
                VISUAL_COMMIT_NOT_CHECKED_OUT,
                "a report is recorded only at the exact commit it was taken at",
            )
        if not approved_by or not _COMMENT_ID.match(authorization_ref or ""):
            raise InputValidationError(
                VISUAL_REVIEW_MISSING,
                "a visual acceptance names its reviewer and the GitHub comment that accepted it",
            )
        results = report["results"]
        with self._store.transaction() as unit:
            return unit.record_visual_acceptance(
                code_sha=report["code_sha"],
                code_digest=report["code_digest"],
                schema_head=report["schema_head"],
                report_digest=report[DIGEST_FIELD],
                harness_version=report["harness_version"],
                scenario=report["scenario"],
                target_count=len(results),
                check_count=sum(len(r["checks"]) for r in results),
                evidence=evidence_of(report),
                approved_by=approved_by,
                authorization_ref=authorization_ref,
                actor=actor,
                correlation_id=correlation_id,
            )

    def recorded(self) -> bool:
        """``VISUAL_ACCEPTANCE_RECORDED``: a reviewed record of exactly this commit and running
        code at this head. No readable commit or head: never recorded."""
        sha, head = self._sha(), self._head()
        if not sha or not head:
            return False
        with self._store.reading() as unit:
            return unit.visual_accepted(sha, self._code(), head)


__all__ = [
    "HARNESS_VERSION",
    "REPORT_VERSION",
    "REQUIRED_CHECKS",
    "REQUIRED_TARGETS",
    "REQUIRED_VIEWPORTS",
    "SCENARIO",
    "STATE_SELECTORS",
    "Target",
    "VisualAcceptanceService",
    "evidence_of",
    "report_digest",
    "verify_report",
]
