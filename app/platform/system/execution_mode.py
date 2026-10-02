import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from pydantic import BaseModel

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock, SystemClock
from app.platform.core.errors import PolicyBlockedError
from app.platform.core.execution import ExecutionMode

logger = logging.getLogger("icbm.system")

ACTION = "EXECUTION_MODE_CHANGE"
M0_POLICY = "M0_DRY_RUN_ONLY"
# ADR-0018 §2: the policy of an open bounded LIVE window.
LIVE_POLICY: Final = "ADR0018_BOUNDED_LIVE"

# A LIVE window is bounded: it lasts at most this long, never past the approved bounded LIVE
# mutation scope it is opened inside, then lapses back to DRY_RUN by itself.
LIVE_WINDOW_MAX_S: Final = 4 * 60 * 60

# A LIVE request that names no bounded window at all keeps the refusal it always had.
UNBOUNDED_LIVE_FORBIDDEN: Final = "M0_LIVE_FORBIDDEN"
WINDOW_OUT_OF_BOUNDS: Final = "LIVE_WINDOW_OUT_OF_BOUNDS"
WINDOW_ALREADY_OPEN: Final = "LIVE_WINDOW_ALREADY_OPEN"
SCOPE_NOT_APPROVED: Final = "LIVE_SCOPE_NOT_APPROVED"
WINDOW_OPENED: Final = "LIVE_WINDOW_OPENED"
WINDOW_CLOSED: Final = "LIVE_WINDOW_CLOSED"


class ExecutionModeState(BaseModel):
    mode: ExecutionMode
    # The execution-mode layer's own answer (ADR-0018 §4.3 layer 1), never permission for a
    # mutation: the brake, the stage's grant and every other send-time layer still decide.
    live_writes_permitted: bool
    policy: str
    live_until: datetime | None = None


# Until when an approved bounded LIVE mutation scope exists — a live grant (ADR-0018 §3) — or None.
ScopeSource = Callable[[], datetime | None]


@dataclass(frozen=True)
class _LiveWindow:
    until: datetime
    audit_event_id: str


class ExecutionModeService:
    """Owner of the global DRY_RUN | LIVE switch (ADR-0018 §2, ROADMAP §14 item 5).

    Changing it is a protected action (CLAUDE.md §7.1/§7.2): every change request is written
    to the append-only audit log before a decision is returned.

    ``DRY_RUN`` is the configured boot default and the only state a process starts in. ``LIVE``
    exists only as one **bounded window** held in this process's memory, opened inside an approved
    bounded LIVE mutation scope: a request names a duration of at most ``LIVE_WINDOW_MAX_S``, and
    the window opens only while a live grant exists (``scope``, the grant owner's read). Opening
    that scope is the protected action that carries the user's approval (rule §7.2, ADR-0022) —
    the grant, issued by an explicit operator action — so the mode switch asks for no approval of
    its own and no evidence identity. The window never outlasts the scope, and it lapses to
    ``DRY_RUN`` by itself the moment its time is up or no live grant remains — revoked, exhausted,
    expired or unreadable. It can be closed at any time, it is never widened while open, and a
    restart never restores it, with or without a grant.
    **The mode alone is never authority for a mutation**: in ``LIVE`` the protected-write brake,
    the stage's grant and every other layer of the send-time stack still decide (ADR-0018 §4.3).
    """

    def __init__(
        self,
        mode: ExecutionMode,
        audit: AuditLog,
        clock: Clock | None = None,
        *,
        scope: ScopeSource | None = None,
    ) -> None:
        if mode is not ExecutionMode.DRY_RUN:
            # AppConfig already refuses it; the owner never starts in anything else.
            raise ValueError("the execution mode always boots DRY_RUN")
        self._audit = audit
        self._clock = clock or SystemClock()
        # No scope source means no approved scope ever exists: LIVE never opens.
        self._scope = scope
        self._lock = threading.Lock()
        self._window: _LiveWindow | None = None

    def state(self) -> ExecutionModeState:
        with self._lock:
            window = self._current()
        if window is None:
            return ExecutionModeState(
                mode=ExecutionMode.DRY_RUN, live_writes_permitted=False, policy=M0_POLICY
            )
        return ExecutionModeState(
            mode=ExecutionMode.LIVE,
            live_writes_permitted=True,
            policy=LIVE_POLICY,
            live_until=window.until,
        )

    def request_change(
        self,
        target: ExecutionMode,
        *,
        actor: str,
        reason: str | None,
        window_s: int | None = None,
    ) -> ExecutionModeState:
        if target is ExecutionMode.DRY_RUN:
            self._close(actor=actor, reason=reason)
        else:
            self._open(actor=actor, reason=reason, window_s=window_s)
        return self.state()

    # ------------------------------------------------------------------ transitions

    def _current(self) -> _LiveWindow | None:
        """The open window, or None once it has lapsed: its time is up, or no approved scope
        remains. The caller holds the lock."""
        window = self._window
        if window is None:
            return None
        now = self._clock.now()
        scope = self._scope_until()
        if now >= window.until or scope is None or now >= scope:
            self._window = None
            return None
        return window

    def _scope_until(self) -> datetime | None:
        if self._scope is None:
            return None
        try:
            return self._scope()
        except Exception:  # an unreadable scope is no scope, never an error
            logger.warning("execution_mode.scope_unreadable", extra={"action": ACTION})
            return None

    def _close(self, *, actor: str, reason: str | None) -> None:
        with self._lock:
            window = self._current()
            if window is None:
                return  # already DRY_RUN: nothing changes and nothing is recorded, as before
            event_id = self._record(
                AuditOutcome.ALLOWED,
                WINDOW_CLOSED,
                actor=actor,
                before=ExecutionMode.LIVE,
                after=ExecutionMode.DRY_RUN,
                details={
                    "requested_mode": ExecutionMode.DRY_RUN,
                    "reason": reason,
                    "opened_by_audit_event_id": window.audit_event_id,
                    "live_until": window.until.isoformat(),
                },
            )
            self._window = None
        logger.warning(
            "protected_action.allowed",
            extra={"action": ACTION, "reason_code": WINDOW_CLOSED, "audit_event_id": event_id},
        )

    def _open(self, *, actor: str, reason: str | None, window_s: int | None) -> None:
        with self._lock:
            current = self._current()
            mode = ExecutionMode.DRY_RUN if current is None else ExecutionMode.LIVE
            details: dict[str, Any] = {
                "requested_mode": ExecutionMode.LIVE,
                "reason": reason,
                "window_s": window_s,
                "policy": M0_POLICY if current is None else LIVE_POLICY,
            }
            now = self._clock.now()
            refusal = _refusal(current, window_s)
            scope = self._scope_until() if refusal is None else None
            if refusal is None and (scope is None or now >= scope):
                refusal = SCOPE_NOT_APPROVED
            if refusal is None and scope is not None and isinstance(window_s, int):
                # Never past the approved scope the window is opened inside.
                until = min(now + timedelta(seconds=window_s), scope)
                details["live_until"] = until.isoformat()
                details["scope_until"] = scope.isoformat()
                # Recorded before the decision takes effect: a failed append opens nothing.
                event_id = self._record(
                    AuditOutcome.ALLOWED,
                    WINDOW_OPENED,
                    actor=actor,
                    before=mode,
                    after=ExecutionMode.LIVE,
                    details=details,
                )
                self._window = _LiveWindow(until, event_id)
                denied = ""
            else:
                denied = refusal or WINDOW_OUT_OF_BOUNDS
                event_id = self._record(
                    AuditOutcome.DENIED,
                    denied,
                    actor=actor,
                    before=mode,
                    after=mode,
                    details=details,
                )
        if not denied:
            logger.warning(
                "protected_action.allowed",
                extra={"action": ACTION, "reason_code": WINDOW_OPENED, "audit_event_id": event_id},
            )
            return
        logger.warning(
            "protected_action.denied",
            extra={
                "action": ACTION,
                "requested_mode": ExecutionMode.LIVE,
                "audit_event_id": event_id,
            },
        )
        raise PolicyBlockedError(
            denied,
            _MESSAGES[denied],
            details={"audit_event_id": event_id, "requested_mode": ExecutionMode.LIVE},
        )

    def _record(
        self,
        outcome: AuditOutcome,
        reason_code: str,
        *,
        actor: str,
        before: ExecutionMode,
        after: ExecutionMode,
        details: dict[str, Any],
    ) -> str:
        record = self._audit.append(
            AuditEntry(
                event_type=AuditEventType.PROTECTED_ACTION,
                action=ACTION,
                actor=actor,
                outcome=outcome,
                target_ref="system:execution_mode",
                reason_code=reason_code,
                before={"mode": before},
                after={"mode": after},
                details=details,
            )
        )
        return record.event_id


def _refusal(current: _LiveWindow | None, window_s: int | None) -> str | None:
    """Why a LIVE request opens nothing before the scope is read, or None."""
    if window_s is None:
        return UNBOUNDED_LIVE_FORBIDDEN
    if current is not None:
        # Never widened while open: closing it first is its own audited change.
        return WINDOW_ALREADY_OPEN
    if (
        not isinstance(window_s, int)
        or isinstance(window_s, bool)
        or not 0 < window_s <= LIVE_WINDOW_MAX_S
    ):
        return WINDOW_OUT_OF_BOUNDS
    return None


_MESSAGES: Final = {
    UNBOUNDED_LIVE_FORBIDDEN: (
        "DRY_RUN is the default. LIVE opens only as a bounded window of at most "
        f"{LIVE_WINDOW_MAX_S} seconds inside an approved bounded LIVE mutation scope "
        "(ADR-0018 §2, CLAUDE.md §7)."
    ),
    WINDOW_ALREADY_OPEN: "a LIVE window is already open; close it before opening another",
    WINDOW_OUT_OF_BOUNDS: f"a LIVE window lasts 1 to {LIVE_WINDOW_MAX_S} seconds",
    SCOPE_NOT_APPROVED: (
        "no approved bounded LIVE mutation scope exists: a LIVE window opens only while a live "
        "grant does (ADR-0018 §2, §3; CLAUDE.md §7.2)"
    ),
}
