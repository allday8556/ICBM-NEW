import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent, AuditEventType, AuditOutcome
from app.core.clock import Clock
from app.core.correlation import get_correlation_id, new_correlation_id
from app.db.database import Database

logger = logging.getLogger("icbm.audit")


@dataclass(frozen=True)
class AuditEntry:
    event_type: AuditEventType
    action: str
    actor: str
    outcome: AuditOutcome
    target_ref: str | None = None
    reason_code: str | None = None
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    details: Mapping[str, Any] | None = None
    correlation_id: str | None = None


class AuditEventRecord(BaseModel):
    seq: int
    event_id: str
    occurred_at: datetime
    correlation_id: str
    actor: str
    event_type: str
    action: str
    target_ref: str | None
    outcome: str
    reason_code: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    details: dict[str, Any]


def _dump(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)


def _to_record(event: AuditEvent) -> AuditEventRecord:
    return AuditEventRecord(
        seq=event.seq,
        event_id=event.event_id,
        occurred_at=event.occurred_at,
        correlation_id=event.correlation_id,
        actor=event.actor,
        event_type=event.event_type,
        action=event.action,
        target_ref=event.target_ref,
        outcome=event.outcome,
        reason_code=event.reason_code,
        before=json.loads(event.before_json) if event.before_json else None,
        after=json.loads(event.after_json) if event.after_json else None,
        details=json.loads(event.details_json),
    )


class AuditLog:
    """Append-only: this class exposes no update or delete, and the database enforces it too."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def append(self, entry: AuditEntry, *, session: Session | None = None) -> AuditEventRecord:
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            occurred_at=self._clock.now(),
            correlation_id=entry.correlation_id or get_correlation_id() or new_correlation_id(),
            actor=entry.actor,
            event_type=entry.event_type,
            action=entry.action,
            target_ref=entry.target_ref,
            outcome=entry.outcome,
            reason_code=entry.reason_code,
            before_json=_dump(entry.before),
            after_json=_dump(entry.after),
            details_json=_dump(entry.details) or "{}",
        )
        if session is None:
            with self._db.write() as own:
                own.add(event)
                own.flush()
                record = _to_record(event)
        else:
            session.add(event)
            session.flush()
            record = _to_record(event)
        logger.info(
            "audit.appended",
            extra={
                "event_id": record.event_id,
                "event_type": record.event_type,
                "action": record.action,
                "outcome": record.outcome,
                "reason_code": record.reason_code,
                "audit_correlation_id": record.correlation_id,
            },
        )
        return record

    def list_events(
        self, *, correlation_id: str | None = None, limit: int = 100
    ) -> list[AuditEventRecord]:
        query = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit)
        if correlation_id is not None:
            query = query.where(AuditEvent.correlation_id == correlation_id)
        with self._db.read() as session:
            return [_to_record(event) for event in session.scalars(query).all()]
