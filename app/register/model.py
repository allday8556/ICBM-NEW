"""The REGISTER vocabulary of ADR-0014 (Issue #89 M5 PR-B), and its deterministic identities.

Pure: no database, no clock, no I/O, no provider. The persisted tables (``app.register.models``)
repeat these vocabularies as frozen CHECK literals, so no write path can store a value outside
them. Nothing here decides a preflight, builds a payload or calls a marketplace: those are PR-C to
PR-E.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any, Final

from app.connect.marketplace.capability import RemoteOutcome
from app.core.errors import AppError, ErrorClass


class ListingShape(StrEnum):
    """ADR-0014 §2 (Canonical v3.1 §8)."""

    SINGLE_LISTING_WITH_OPTIONS = "SINGLE_LISTING_WITH_OPTIONS"
    SEPARATE_LISTINGS = "SEPARATE_LISTINGS"
    SELECTED_OFFERS = "SELECTED_OFFERS"


class Operation(StrEnum):
    """ADR-0014 §8: UPDATE and DELETE are not authorized in M5's first vertical."""

    CREATE = "CREATE"


class IntentState(StrEnum):
    """ADR-0014 §8, `docs/ARCHITECTURE.md` §7.

    - ``PREPARED``: frozen and not yet sent.
    - ``SENT``: an attempt is in flight, or the CREATE is applied (``APPLIED_PROVEN``) and not yet
      verified, or its verification is a ``MISMATCH`` under review (§11).
    - ``CONFIRMED``: applied and verified against the Snapshot; terminal.
    - ``UNKNOWN``: the outcome is not proven; reconcile first, never resend (§10).
    - ``FAILED``: proven not applied (``NOT_APPLIED_PROVEN``); a retry is a new attempt of the same
      Intent.
    """

    PREPARED = "PREPARED"
    SENT = "SENT"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


# Every state change an Intent may make. SENT -> SENT records an applied outcome or a verification
# under review without a state change. CONFIRMED is terminal. UNKNOWN leaves only with a resolution
# backed by machine or provider evidence (ADR-0014 §10, B3).
INTENT_TRANSITIONS: Final = frozenset(
    {
        (IntentState.PREPARED, IntentState.SENT),
        (IntentState.PREPARED, IntentState.FAILED),
        (IntentState.SENT, IntentState.SENT),
        (IntentState.SENT, IntentState.CONFIRMED),
        (IntentState.SENT, IntentState.UNKNOWN),
        (IntentState.SENT, IntentState.FAILED),
        (IntentState.UNKNOWN, IntentState.SENT),
        (IntentState.UNKNOWN, IntentState.FAILED),
        (IntentState.FAILED, IntentState.SENT),
    }
)

# ADR-0014 §10 (R2), read fail-closed: an Intent in these states may already have created a listing
# whose verification is not settled, so no new CREATE Intent may overlap its conflict scope.
BLOCKING_STATES: Final = frozenset({IntentState.SENT, IntentState.UNKNOWN})


class VerificationState(StrEnum):
    """ADR-0014 §11: the read-back comparison against the immutable Snapshot."""

    NOT_VERIFIED = "NOT_VERIFIED"
    PASS = "PASS"
    MISMATCH = "MISMATCH"


class RegistrationLifecycle(StrEnum):
    """ADR-0014 §14 (R4): a verified registration, or one whose listing is proven absent."""

    ACTIVE = "ACTIVE"
    EXTERNALLY_REMOVED = "EXTERNALLY_REMOVED"


class ResolvedBy(StrEnum):
    """ADR-0014 §9: who recorded a resolution. Never itself the evidence (§10, B3)."""

    READ_BACK = "READ_BACK"
    LOOKUP = "LOOKUP"
    USER = "USER"


class ResolutionEvidence(StrEnum):
    """ADR-0014 §10 (B3): the proof that may settle an UNKNOWN outcome.

    There is deliberately no operator-assertion member: an operator may record or accept one of
    these, but their word alone never establishes an outcome.
    """

    PROVIDER_READ_BACK = "PROVIDER_READ_BACK"
    PROVIDER_LOOKUP = "PROVIDER_LOOKUP"
    TRANSMISSION_PRECLUDED = "TRANSMISSION_PRECLUDED"
    REVIEWED_MACHINE_PROOF = "REVIEWED_MACHINE_PROOF"


class AbsenceEvidence(StrEnum):
    """ADR-0014 §14 (R4): only provider evidence proves a listing absent."""

    PROVIDER_READ_BACK = "PROVIDER_READ_BACK"
    PROVIDER_LOOKUP = "PROVIDER_LOOKUP"


class BatchSummary(StrEnum):
    """ADR-0014 §12: a batch's summary is derived from its child Intents, never stored."""

    EMPTY = "EMPTY"
    IN_PROGRESS = "IN_PROGRESS"
    CONFIRMED = "CONFIRMED"
    PARTIAL = "PARTIAL"
    UNRESOLVED = "UNRESOLVED"
    FAILED = "FAILED"


def summarize(states: Iterable[IntentState]) -> BatchSummary:
    """The derived summary of a batch's Intents (§12). ``PARTIAL`` means at least one listing is
    CONFIRMED and at least one is not; it is never stored and never rewrites a child."""
    present = list(states)
    if not present:
        return BatchSummary.EMPTY
    if all(state is IntentState.CONFIRMED for state in present):
        return BatchSummary.CONFIRMED
    if any(state is IntentState.CONFIRMED for state in present):
        return BatchSummary.PARTIAL
    if any(state is IntentState.UNKNOWN for state in present):
        return BatchSummary.UNRESOLVED
    if all(state is IntentState.FAILED for state in present):
        return BatchSummary.FAILED
    return BatchSummary.IN_PROGRESS


# ---------------------------------------------------------------- identities (ADR-0014 §7, §8)

LISTING_IDENTITY: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
ITEM_KEY_VERSION: Final = "registration-item-key/v1"
ITEM_KEY_PREFIX: Final = "rik1-"
IDEMPOTENCY_VERSION: Final = "registration-idempotency/v1"


def new_listing_identity() -> str:
    """A new stable, seller-side identity for one provider-listing unit (§7). It is created once
    per unit and never derived from a product name; an intentional duplicate or a re-registration
    gets a new one."""
    return f"icbm-{uuid.uuid4().hex}"


def valid_listing_identity(value: str) -> bool:
    return bool(LISTING_IDENTITY.match(value))


def canonical_json(value: Mapping[str, Any] | list[Any]) -> str:
    """The canonical text of a sanitized representation: sorted keys, no insignificant space."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sanitized_digest(value: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical text of an already sanitized representation (ADR-0014 §15, B4).

    It is only ever given the sanitized canonical evidence representation, never wire bytes, so
    no credential, session or tokenized value can enter a durable digest through it.
    """
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def registration_item_key(
    listing_identity: str, product_group_id: str, composition_signature: str
) -> str:
    """The stable ``registration_item_key`` of one Item in one provider-listing unit (§7).

    Deterministic from the unit's listing identity and the Item key (group + composition
    signature); never from a label, a name, a price or an order position.
    """
    text = canonical_json(
        {
            "version": ITEM_KEY_VERSION,
            "listing_identity": listing_identity,
            "product_group_id": product_group_id,
            "composition_signature": composition_signature,
        }
    )
    return ITEM_KEY_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def idempotency_key(
    marketplace_key: str, account_id: str, operation: Operation, registration_snapshot_id: str
) -> str:
    """One durable idempotency identity per marketplace, account, operation and exact Snapshot
    (§8): the same inputs always give the same key, across restarts."""
    text = canonical_json(
        {
            "version": IDEMPOTENCY_VERSION,
            "marketplace_key": marketplace_key,
            "account_id": account_id,
            "operation": operation.value,
            "registration_snapshot_id": registration_snapshot_id,
        }
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def effective_outcome(
    remote_outcome: RemoteOutcome | None, resolved_outcome: RemoteOutcome | None
) -> RemoteOutcome | None:
    """An attempt's outcome now: its evidence-backed resolution, else what it finished with."""
    return resolved_outcome if resolved_outcome is not None else remote_outcome


class RegistrationConflictError(AppError):
    """A registration write the contract forbids in the current state (ADR-0014 §8, §10)."""

    error_class = ErrorClass.CONFLICT
