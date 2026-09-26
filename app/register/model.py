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
    """ADR-0014 §14 (R4): only provider evidence proves a listing absent.

    ``PROVIDER_LOOKUP`` stays a value of the stored vocabulary only (the check constraint of
    migration 0016). It is never admissible: a provider lookup never proves remote absence
    (§17.2, §28), so :meth:`RegistrationUnit.record_external_absence` refuses it.
    """

    PROVIDER_READ_BACK = "PROVIDER_READ_BACK"
    PROVIDER_LOOKUP = "PROVIDER_LOOKUP"


class ExecutionScopeState(StrEnum):
    """ADR-0014 §26: whether one execution scope's automatic send brake is engaged.

    This is REGISTER's own operational control, never capability truth: CONNECT still owns the
    binding, the auth, the permission scope, its workflow overlays and contract freshness.
    """

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class ScopePauseReason(StrEnum):
    """ADR-0014 §26: why the send brake of one execution scope is engaged.

    Each cause has its own accepted release (§26): only ``AUTH`` is ever released automatically,
    and then only by a CONNECT authentication proof newer than the pause itself.
    """

    AUTH = "AUTH"
    POLICY = "POLICY"
    FAILURE_BUDGET = "FAILURE_BUDGET"


# M5-26: the causes an operator may release. `AUTH` is deliberately absent — an authentication
# pause ends when the account authenticates again, and an operator action is not that proof.
OPERATOR_RESUMABLE: Final = frozenset({ScopePauseReason.POLICY, ScopePauseReason.FAILURE_BUDGET})
# M5-25: the measured cause each brake reason is recorded with, so a durable row can never pair a
# reason with a class that did not cause it. `FAILURE_BUDGET` is a policy threshold, not one
# provider verdict: it carries the class of the failure that spent the last of the budget, or
# none at all when a policy revision alone exhausted it, and never a cause that pauses by itself.
PAUSE_CAUSE_CLASSES: Final[Mapping[ScopePauseReason, frozenset[ErrorClass] | None]] = {
    ScopePauseReason.AUTH: frozenset({ErrorClass.AUTH}),
    ScopePauseReason.POLICY: frozenset({ErrorClass.POLICY_BLOCKED}),
    ScopePauseReason.FAILURE_BUDGET: None,
}


def pause_class_allowed(reason: ScopePauseReason, error_class: ErrorClass | None) -> bool:
    """Whether this measured class may be recorded with this brake reason (§26)."""
    allowed = PAUSE_CAUSE_CLASSES[reason]
    if allowed is None:
        return error_class is None or error_class not in {
            ErrorClass.AUTH,
            ErrorClass.POLICY_BLOCKED,
        }
    return error_class in allowed


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
    marketplace_key: str,
    marketplace_account_id: str,
    operation: Operation,
    registration_snapshot_id: str,
) -> str:
    """One durable idempotency identity per marketplace, canonical account, operation and exact
    Snapshot (§8): the same inputs always give the same key, across restarts. The account is the
    ICBM ``marketplace_account_id``, never the provider's wire ``account_id``."""
    text = canonical_json(
        {
            "version": IDEMPOTENCY_VERSION,
            "marketplace_key": marketplace_key,
            "marketplace_account_id": marketplace_account_id,
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


def uncovered_single_listing(open_items: Mapping[str, str], sent_items: Mapping[str, str]) -> bool:
    """Whether a ``SINGLE_LISTING_WITH_OPTIONS`` unit fails to send exactly its Draft (§2, R3).

    Both mappings are Item id → pinned ``pricing_snapshot_id``. The one provider-listing unit must
    hold every open Item of the Draft revision with its pinned price, and nothing else; a subset
    is never a single listing. Correspondence is by Item identity, never by position or label.
    """
    return dict(open_items) != dict(sent_items)


class RegistrationConflictError(AppError):
    """A registration write the contract forbids in the current state (ADR-0014 §8, §10)."""

    error_class = ErrorClass.CONFLICT
