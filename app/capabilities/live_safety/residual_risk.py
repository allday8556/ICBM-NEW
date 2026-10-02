"""The residual-risk acceptance proof (ADR-0018 §6.1, G3-30; migration 0036).

Opening any real canary under the §6.1 strategy needs an explicit user and architect acceptance of
its residual risk, **recorded in GitHub**. GitHub holds that decision. This owner holds only the
durable proof the existing send-time layer ``RESIDUAL_RISK_ACCEPTED`` reads: for one canonical
account and one exact risk contract, the GitHub comment recording the user's acceptance and the one
recording the architect's, each by its id and the SHA-256 of its body. It never decides, interprets
or replaces the decision. The user only decides: writing and digesting those comments is evidence
bookkeeping the agent and the Host do (ADR-0022 §3).

- **Evidence is a content-bound GitHub identity**, ``github_issue_comment:<id>@<sha256 of the
  body>``, the form the Agent Host uses for every cited source. Any other form is refused; the two
  acceptances are never the same comment.
- **The risk contract is exact.** A proof binds :data:`RESIDUAL_RISK_CONTRACT_VERSION` and the
  SHA-256 of :data:`RESIDUAL_RISK_STATEMENT`, the §6.1 statement verbatim. A proof of another
  version or another statement is stale and proves nothing; a changed risk needs a new acceptance.
- **Append-only, one writer.** Only the protected operator command ``icbm live
  record-residual-risk-acceptance`` records a proof; no API or screen can.
- **It authorizes nothing.** It is one layer of both mutation stages (ADR-0018 §10). Execution
  mode, the brake, endpoint adoption, eligibility, the restore drill, retention, the visual
  acceptance and the stage's own grant and gate all still decide, and each still refuses on its
  own. Missing, unreadable, wrong-scope or stale proof is not accepted.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from app.capabilities.live_safety.models import ResidualRiskAcceptance
from app.capabilities.live_safety.store import LiveAuthorityStore
from app.platform.core.errors import InputValidationError

# The exact risk this proof accepts, as ADR-0018 §6.1 states it (architect decision 5845062336).
RESIDUAL_RISK_CONTRACT_VERSION: Final = "adr-0018-6.1-residual-risk/v1"
RESIDUAL_RISK_STATEMENT: Final = (
    "a product may be live in SmartStore after an ambiguous CREATE while ICBM has not yet"
    " recovered the provider identity, leaving that listing temporarily outside normal confirmed"
    " price/stock monitoring."
)
RESIDUAL_RISK_STATEMENT_DIGEST: Final = hashlib.sha256(
    RESIDUAL_RISK_STATEMENT.encode("utf-8")
).hexdigest()

# The one admitted evidence form: a GitHub comment, by id and the SHA-256 of its body.
EVIDENCE_FORM: Final = "github_issue_comment:<id>@<sha256>"
_EVIDENCE = re.compile(r"^github_issue_comment:([0-9]{6,20})@([0-9a-f]{64})$")

CONTRACT_NOT_CURRENT: Final = "LIVE_RESIDUAL_RISK_CONTRACT_NOT_CURRENT"
EVIDENCE_UNSUPPORTED: Final = "LIVE_RESIDUAL_RISK_EVIDENCE_UNSUPPORTED"


@dataclass(frozen=True)
class AcceptanceEvidence:
    comment_id: str
    body_digest: str

    @property
    def identity(self) -> str:
        return f"github_issue_comment:{self.comment_id}@{self.body_digest}"


@dataclass(frozen=True)
class AcceptanceRecord:
    acceptance_id: str
    marketplace_key: str
    marketplace_account_id: str
    risk_contract_version: str
    risk_statement_digest: str
    seq: int
    user_acceptance: AcceptanceEvidence
    architect_acceptance: AcceptanceEvidence
    recorded_by: str
    recorded_at: datetime


def parse_evidence(value: str) -> AcceptanceEvidence:
    """One acceptance's GitHub identity, or a refusal: no other evidence form is admitted."""
    found = _EVIDENCE.fullmatch(value.strip()) if isinstance(value, str) else None
    if found is None:
        raise InputValidationError(
            EVIDENCE_UNSUPPORTED,
            f"an acceptance is a GitHub comment identity of the form {EVIDENCE_FORM}",
        )
    return AcceptanceEvidence(found.group(1), found.group(2))


class ResidualRiskAcceptanceService:
    """The durable proof of the §6.1 residual-risk acceptance. It records and reads; it never
    decides, and nothing it holds permits a mutation by itself."""

    def __init__(self, store: LiveAuthorityStore) -> None:
        self._store = store

    def record(
        self,
        *,
        marketplace_key: str,
        marketplace_account_id: str,
        risk_contract: str,
        user_acceptance: str,
        architect_acceptance: str,
        actor: str,
        correlation_id: str,
    ) -> AcceptanceRecord:
        """Record the proof of one acceptance already given in GitHub. Called only by the
        protected operator command."""
        if risk_contract != RESIDUAL_RISK_CONTRACT_VERSION:
            raise InputValidationError(
                CONTRACT_NOT_CURRENT,
                f"the current residual-risk contract is {RESIDUAL_RISK_CONTRACT_VERSION}",
            )
        user = parse_evidence(user_acceptance)
        architect = parse_evidence(architect_acceptance)
        with self._store.transaction() as unit:
            row = unit.record_residual_risk_acceptance(
                marketplace_key=marketplace_key,
                marketplace_account_id=marketplace_account_id,
                risk_contract_version=RESIDUAL_RISK_CONTRACT_VERSION,
                risk_statement_digest=RESIDUAL_RISK_STATEMENT_DIGEST,
                user_comment_id=user.comment_id,
                user_comment_digest=user.body_digest,
                architect_comment_id=architect.comment_id,
                architect_comment_digest=architect.body_digest,
                actor=actor,
                correlation_id=correlation_id,
            )
            return _record(row)

    def history(
        self, marketplace_key: str, marketplace_account_id: str
    ) -> tuple[AcceptanceRecord, ...]:
        """Every proof of this account under the current risk contract, oldest first."""
        with self._store.reading() as unit:
            rows = unit.residual_risk_acceptances(
                marketplace_key, marketplace_account_id, RESIDUAL_RISK_CONTRACT_VERSION
            )
            return tuple(_record(row) for row in rows)

    def accepted(self, marketplace_key: str, marketplace_account_id: str) -> bool:
        """Whether a proof of exactly this account, the current risk contract and the current
        statement exists. Anything else — no proof, another account, a stale contract or an
        unreadable owner — is not accepted."""
        if not marketplace_key or not marketplace_account_id:
            return False
        try:
            records = self.history(marketplace_key, marketplace_account_id)
        except SQLAlchemyError:
            return False
        return any(
            record.marketplace_key == marketplace_key
            and record.marketplace_account_id == marketplace_account_id
            and record.risk_contract_version == RESIDUAL_RISK_CONTRACT_VERSION
            and record.risk_statement_digest == RESIDUAL_RISK_STATEMENT_DIGEST
            and record.user_acceptance.comment_id != record.architect_acceptance.comment_id
            for record in records
        )


def _record(row: ResidualRiskAcceptance) -> AcceptanceRecord:
    return AcceptanceRecord(
        acceptance_id=row.acceptance_id,
        marketplace_key=row.marketplace_key,
        marketplace_account_id=row.marketplace_account_id,
        risk_contract_version=row.risk_contract_version,
        risk_statement_digest=row.risk_statement_digest,
        seq=row.seq,
        user_acceptance=AcceptanceEvidence(row.user_comment_id, row.user_comment_digest),
        architect_acceptance=AcceptanceEvidence(
            row.architect_comment_id, row.architect_comment_digest
        ),
        recorded_by=row.recorded_by,
        recorded_at=row.recorded_at,
    )


__all__ = [
    "CONTRACT_NOT_CURRENT",
    "EVIDENCE_FORM",
    "EVIDENCE_UNSUPPORTED",
    "RESIDUAL_RISK_CONTRACT_VERSION",
    "RESIDUAL_RISK_STATEMENT",
    "RESIDUAL_RISK_STATEMENT_DIGEST",
    "AcceptanceEvidence",
    "AcceptanceRecord",
    "ResidualRiskAcceptanceService",
    "parse_evidence",
]
