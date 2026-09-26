"""The data-root record of Phase C harness commands (Issue #110; review ``5313663701`` follow-up).

Every harness command that changes the live data root is reserved here under its stable
correlation *before* its change, and settled after it with what owner truth proves it did:
``APPLIED``, ``RECOVERED`` (applied, proven on reconciliation) or ``NOT_APPLIED``. A reservation
without a result is an unresolved command. The harness reconciles its own campaign's unresolved
commands against owner truth; while any stays unresolved — a crashed command not yet reconciled, or
an ambiguous one its campaign holds — no campaign on this data root runs an evidence command. A new
campaign therefore never walks around another campaign's unresolved change.

Only the Phase C harness reaches this owner (a repository rule pins the caller).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.collect.adaptive_capture.models import COMMAND_OUTCOMES, PhaseCCommand, PhaseCCommandResult
from app.core.clock import Clock
from app.core.errors import InputValidationError
from app.db.database import Database

PHASE_C_COMMAND_REFUSED = "PHASE_C_COMMAND_REFUSED"


def _command_refused(message: str) -> InputValidationError:
    return InputValidationError(PHASE_C_COMMAND_REFUSED, message)


@dataclass(frozen=True)
class CommandRecord:
    correlation_id: str
    campaign_id: str
    command: str
    reserved_at: datetime
    outcome: str | None


class PhaseCCommandStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def reserve(self, *, correlation_id: str, campaign_id: str, command: str) -> None:
        """Reserve one command before its change: a correlation is reserved once, ever."""
        with self._db.write() as session:
            # One writer holds the data root (ADR-0006), so this check and the insert are one unit.
            if session.get(PhaseCCommand, correlation_id) is not None:
                raise _command_refused("a Phase C command correlation is reserved once")
            session.add(
                PhaseCCommand(
                    correlation_id=correlation_id,
                    campaign_id=campaign_id,
                    command=command,
                    reserved_at=self._clock.now(),
                )
            )

    def settle(self, correlation_id: str, outcome: str) -> None:
        """Record what one reserved command was proven to do; a command is settled once."""
        if outcome not in COMMAND_OUTCOMES:
            raise _command_refused(f"a command outcome is one of {COMMAND_OUTCOMES}")
        with self._db.write() as session:
            if session.get(PhaseCCommand, correlation_id) is None:
                raise _command_refused("only a reserved command is settled")
            if session.get(PhaseCCommandResult, correlation_id) is not None:
                raise _command_refused("a Phase C command is settled once")
            session.add(
                PhaseCCommandResult(
                    correlation_id=correlation_id, outcome=outcome, settled_at=self._clock.now()
                )
            )

    def command(self, correlation_id: str) -> CommandRecord | None:
        with self._db.read() as session:
            row = session.get(PhaseCCommand, correlation_id)
            if row is None:
                return None
            result = session.get(PhaseCCommandResult, correlation_id)
            return CommandRecord(
                row.correlation_id,
                row.campaign_id,
                row.command,
                row.reserved_at,
                None if result is None else result.outcome,
            )

    def of_campaign(self, campaign_id: str) -> tuple[CommandRecord, ...]:
        """Every command one campaign reserved here, settled or not, oldest first."""
        with self._db.read() as session:
            rows = session.execute(
                select(PhaseCCommand, PhaseCCommandResult.outcome)
                .outerjoin(
                    PhaseCCommandResult,
                    PhaseCCommandResult.correlation_id == PhaseCCommand.correlation_id,
                )
                .where(PhaseCCommand.campaign_id == campaign_id)
                .order_by(PhaseCCommand.reserved_at, PhaseCCommand.correlation_id)
            ).all()
            return tuple(
                CommandRecord(r.correlation_id, r.campaign_id, r.command, r.reserved_at, outcome)
                for r, outcome in rows
            )

    def unresolved(self) -> tuple[CommandRecord, ...]:
        """Every reserved command of any campaign with no proven result yet."""
        with self._db.read() as session:
            rows = session.execute(
                select(PhaseCCommand)
                .outerjoin(
                    PhaseCCommandResult,
                    PhaseCCommandResult.correlation_id == PhaseCCommand.correlation_id,
                )
                .where(PhaseCCommandResult.correlation_id.is_(None))
                .order_by(PhaseCCommand.reserved_at, PhaseCCommand.correlation_id)
            ).scalars()
            return tuple(
                CommandRecord(r.correlation_id, r.campaign_id, r.command, r.reserved_at, None)
                for r in rows
            )
