"""The REGISTER review producer (Gate 2 G2-C, ADR-0016 §6).

It indexes, by reference, the durable REGISTER states that only a human can move on. It reads the
registration store, re-implements no execution rule, and writes nothing. It never reconciles an
``UNKNOWN``, never reads the provider and never frees a conflict scope: ADR-0014 §9, §10 and M5-23
are unchanged, and a resolution of one of these items changes no REGISTER fact (§5).

**The reviewed mapping**, all REGISTRATION_ERROR:

- an Intent whose outcome is ``UNKNOWN`` (ADR-0014 §10, its ``REVIEW_REQUIRED`` overlay) →
  ``intent``, ``REGISTER_INTENT_UNKNOWN``; source identity: its latest attempt;
- an Intent whose read-back comparison is a ``MISMATCH`` (§11) → ``readback``,
  ``REGISTER_READBACK_MISMATCH``; source identity: the sanitized comparison evidence digest;
- a PAUSED execution scope (§26) → ``scope:<endpoint_group>``,
  ``REGISTER_SCOPE_PAUSED_<AUTH|POLICY|FAILURE_BUDGET>``; source identity: its resume generation.

An Intent's scope is ``marketplace_key``, ``marketplace_account_id``, the ``draft_id`` its
Snapshot froze, and ``intent_id``; an execution scope's is its account (§8).

Not indexed, by design: preflight and preparation reasons. They are derived verdicts of the
preparation screen, re-derived on every read, and not errors of a registration (ADR-0014 M5-03);
an ``APPLIED_PROVEN`` Intent awaiting its read-back is pending work, not a recorded failure.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Final

from app.register.model import IntentState, VerificationState
from app.register.store import IntentReviewRecord, RegistrationStore, ScopeRecord
from app.review.model import ReviewCondition, ReviewKind

REGISTER_PRODUCER: Final = "register.execution"

REGISTER_INTENT_UNKNOWN: Final = "REGISTER_INTENT_UNKNOWN"
REGISTER_READBACK_MISMATCH: Final = "REGISTER_READBACK_MISMATCH"
REGISTER_SCOPE_PAUSED_PREFIX: Final = "REGISTER_SCOPE_PAUSED_"


class RegisterReviewProducer:
    """Reads REGISTER's durable intents and execution scopes; writes nothing."""

    def __init__(self, registrations: RegistrationStore) -> None:
        self._registrations = registrations

    @property
    def name(self) -> str:
        return REGISTER_PRODUCER

    def scopes(self) -> Sequence[Mapping[str, str]]:
        """One scope per canonical account: it includes every intent and execution scope of it."""
        return tuple(
            {"marketplace_key": key, "marketplace_account_id": account}
            for key, account in self._registrations.review_accounts()
        )

    def truth_token(self) -> str:
        return hashlib.sha256(
            json.dumps(self._registrations.review_truth(), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def derive(self, scope: Mapping[str, str]) -> Sequence[ReviewCondition]:
        key, account = scope.get("marketplace_key"), scope.get("marketplace_account_id")
        narrowed = None if key is None or account is None else (key, account)
        conditions = [
            *(
                c
                for intent in self._registrations.review_intents(narrowed)
                for c in _intent(intent)
            ),
            *(_paused(paused) for paused in self._registrations.review_paused_scopes(narrowed)),
        ]
        return tuple(c for c in conditions if _within(c.scope, scope))


def _intent(intent: IntentReviewRecord) -> list[ReviewCondition]:
    scope = {
        "marketplace_key": intent.marketplace_key,
        "marketplace_account_id": intent.marketplace_account_id,
        "draft_id": intent.draft_id,
        "intent_id": intent.intent_id,
    }
    found = []
    if intent.state is IntentState.UNKNOWN:
        found.append(
            ReviewCondition(
                kind=ReviewKind.REGISTRATION_ERROR,
                producer=REGISTER_PRODUCER,
                scope=scope,
                subject="intent",
                reason_code=REGISTER_INTENT_UNKNOWN,
                source_identity=intent.latest_attempt_id or intent.intent_id,
            )
        )
    if intent.verification_state is VerificationState.MISMATCH:
        found.append(
            ReviewCondition(
                kind=ReviewKind.REGISTRATION_ERROR,
                producer=REGISTER_PRODUCER,
                scope=scope,
                subject="readback",
                reason_code=REGISTER_READBACK_MISMATCH,
                source_identity=intent.verification_evidence_digest or intent.intent_id,
            )
        )
    return found


def _paused(paused: ScopeRecord) -> ReviewCondition:
    assert paused.pause_reason is not None  # the schema: a PAUSED row names its cause
    return ReviewCondition(
        kind=ReviewKind.REGISTRATION_ERROR,
        producer=REGISTER_PRODUCER,
        scope={
            "marketplace_key": paused.marketplace_key,
            "marketplace_account_id": paused.marketplace_account_id,
        },
        subject=f"scope:{paused.endpoint_group}",
        reason_code=f"{REGISTER_SCOPE_PAUSED_PREFIX}{paused.pause_reason.value}",
        source_identity=f"generation-{paused.resume_generation}",
    )


def _within(scope: Mapping[str, str], wanted: Mapping[str, str]) -> bool:
    return all(scope.get(key) == value for key, value in wanted.items())
