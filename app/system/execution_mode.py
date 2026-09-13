import logging

from pydantic import BaseModel

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.core.errors import PolicyBlockedError
from app.core.execution import ExecutionMode

logger = logging.getLogger("icbm.system")

ACTION = "EXECUTION_MODE_CHANGE"
M0_POLICY = "M0_DRY_RUN_ONLY"


class ExecutionModeState(BaseModel):
    mode: ExecutionMode
    live_writes_permitted: bool
    policy: str


class ExecutionModeService:
    """Owner of the global DRY_RUN | LIVE switch.

    Changing it is a protected action (CLAUDE.md §7.1/§7.2): every change request is written
    to the append-only audit log before a decision is returned. During M0 every change is
    denied — M0 performs zero external writes (Issue #1).
    """

    def __init__(self, mode: ExecutionMode, audit: AuditLog) -> None:
        self._mode = mode
        self._audit = audit

    def state(self) -> ExecutionModeState:
        return ExecutionModeState(mode=self._mode, live_writes_permitted=False, policy=M0_POLICY)

    def request_change(
        self, target: ExecutionMode, *, actor: str, reason: str | None
    ) -> ExecutionModeState:
        if target is self._mode:
            return self.state()
        record = self._audit.append(
            AuditEntry(
                event_type=AuditEventType.PROTECTED_ACTION,
                action=ACTION,
                actor=actor,
                outcome=AuditOutcome.DENIED,
                target_ref="system:execution_mode",
                reason_code="M0_LIVE_FORBIDDEN",
                before={"mode": self._mode},
                after={"mode": self._mode},
                details={"requested_mode": target, "reason": reason, "policy": M0_POLICY},
            )
        )
        logger.warning(
            "protected_action.denied",
            extra={"action": ACTION, "requested_mode": target, "audit_event_id": record.event_id},
        )
        raise PolicyBlockedError(
            "M0_LIVE_FORBIDDEN",
            "M0 permits DRY_RUN only; LIVE requires an explicit GitHub verification scope and "
            "user approval.",
            details={"audit_event_id": record.event_id, "requested_mode": target},
        )
