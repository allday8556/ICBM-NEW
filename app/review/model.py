"""The ReviewItem domain: what a producer derives, its keys, and the lifecycle words (ADR-0016).

A ReviewItem is a durable **index** of human work over a condition an owner already derives (§2).
It holds references only: the kind, the producer, the owner's canonical scope, the subject, the
owner's reason code and the exact source identity. Every one of them is a bounded identifier, so
no page text, URL, credential or payload can ever be stored as one (§9).

The two keys (§3) are computed here, by the server, from those references and nothing else:

    condition key = kind × producer × canonical scope × subject × owner reason code
    review key    = condition key × source identity
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from app.core.errors import AppError, ErrorClass, InputValidationError


class ReviewConflictError(AppError):
    """A review write the current state does not allow; nothing was written (ADR-0016 §5, §8)."""

    error_class = ErrorClass.CONFLICT


class ReviewKind(StrEnum):
    """Closed (ADR-0016 §2, G2-03). A new kind needs an amendment of the ADR."""

    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    STOCK = "STOCK"
    SOURCE_CHANGE = "SOURCE_CHANGE"
    COMPLIANCE = "COMPLIANCE"
    REGISTRATION_ERROR = "REGISTRATION_ERROR"
    FULFILLMENT = "FULFILLMENT"


class ReviewState(StrEnum):
    """§4: decided by reconciliation against current owner truth, and only by it."""

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    SUPERSEDED = "SUPERSEDED"


class ReviewEvent(StrEnum):
    """One row of an item's append-only history (§4, §5, §9)."""

    OPENED = "OPENED"
    REOPENED = "REOPENED"
    SUPERSEDED = "SUPERSEDED"
    RESOLVED = "RESOLVED"
    # A human resolution that did not close the item: the owner still derives the condition, or
    # the same condition moved to a new source identity in that same unit of work (§5).
    RESOLUTION_RECORDED = "RESOLUTION_RECORDED"


class ReviewBasis(StrEnum):
    """Why an event happened. Only the owner's truth moves an item; a human only records."""

    OWNER_CONDITION_DERIVED = "OWNER_CONDITION_DERIVED"
    OWNER_CONDITION_CLEARED = "OWNER_CONDITION_CLEARED"
    OWNER_SOURCE_MOVED = "OWNER_SOURCE_MOVED"
    HUMAN_RESOLUTION = "HUMAN_RESOLUTION"
    CONDITION_PERSISTS = "CONDITION_PERSISTS"


class ReviewDisposition(StrEnum):
    """What the human did (§5). Bounded and server-owned. None of these is a verdict: no value
    here means PASS, READY, CONFIRMED, NOT_APPLIED_PROVEN or remote absence, and none changes an
    owner fact."""

    OWNER_ACTION_TAKEN = "OWNER_ACTION_TAKEN"
    FOLLOW_UP_REQUIRED = "FOLLOW_UP_REQUIRED"
    NO_ACTION_TAKEN = "NO_ACTION_TAKEN"


class ResolutionOutcome(StrEnum):
    """What a human resolution led to, once reconciled in the same unit of work (§5)."""

    RESOLVED = "RESOLVED"
    CONDITION_PERSISTS = "CONDITION_PERSISTS"
    SUPERSEDED = "SUPERSEDED"


# The canonical scope identifiers an owner already uses (§8). No other key is accepted, so no
# cross-domain identifier can be invented for a ReviewItem.
SCOPE_KEYS: Final = frozenset(
    {
        "supplier_key",
        "source_product_id",
        "product_group_id",
        "item_id",
        "marketplace_key",
        "marketplace_account_id",
        "draft_id",
        "listing_identity",
        "preparation_id",
        "intent_id",
    }
)
# A bounded identifier: no whitespace, slash, quote or query material, so no URL, page text or
# credential-shaped value fits. Owner reason codes such as ``M4_BASE.IMAGE_SELECTION_MISSING``
# and subjects such as ``image:2`` do.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")
_PRODUCER = re.compile(r"[a-z][a-z0-9_.]{0,63}")
NOTE_MAX_CHARS: Final = 500

REVIEW_REFERENCE_INVALID: Final = "REVIEW_REFERENCE_INVALID"
REVIEW_NOTE_UNSAFE: Final = "REVIEW_NOTE_UNSAFE"


def _identifier(value: object, what: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise InputValidationError(
            REVIEW_REFERENCE_INVALID,
            f"{what} must be a bounded owner identifier",
            details={"field": what},
        )
    return value


def canonical_scope(scope: Mapping[str, str]) -> dict[str, str]:
    """The scope, validated and in canonical key order. It is never empty."""
    if not scope:
        raise InputValidationError(REVIEW_REFERENCE_INVALID, "a ReviewItem has a canonical scope")
    unknown = sorted(set(scope) - SCOPE_KEYS)
    if unknown:
        raise InputValidationError(
            REVIEW_REFERENCE_INVALID,
            "a scope names only canonical owner identifiers",
            details={"keys": unknown},
        )
    return {key: _identifier(scope[key], f"scope.{key}") for key in sorted(scope)}


# Anything URL-shaped, and any secret-looking assignment or header, fails closed (§9; the same
# stance as persisted source text, ADR-0010 §9). A note is display text; nothing reads it back.
_URL_MATERIAL = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*:)?//|\bwww\.")
_SECRET_MATERIAL = re.compile(
    r"(?i)\b(?:bearer|authorization|password|passwd|secret|token|api[_-]?key|cookie|session)\b"
    r"\s*[:=]"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitized_note(text: str | None) -> str | None:
    """A bounded, sanitized display note, or ``None``. Refused whole rather than redacted."""
    if text is None:
        return None
    note = text.strip()
    if not note:
        return None
    if len(note) > NOTE_MAX_CHARS:
        raise InputValidationError(
            REVIEW_NOTE_UNSAFE, f"a review note is at most {NOTE_MAX_CHARS} characters"
        )
    for pattern, reason in (
        (_URL_MATERIAL, "a review note carries no URL"),
        (_SECRET_MATERIAL, "a review note carries no secret-looking material"),
        (_CONTROL, "a review note carries no control characters"),
    ):
        if pattern.search(note):
            raise InputValidationError(REVIEW_NOTE_UNSAFE, reason)
    return note


def evidence_reference(value: str | None) -> str | None:
    """An optional owner identifier a resolution points at. Never free text (§5)."""
    return None if value is None else _identifier(value, "evidence_reference")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ReviewCondition:
    """One condition an owner derives now, as its producer states it (§2, §6)."""

    kind: ReviewKind
    producer: str
    scope: Mapping[str, str]
    subject: str
    reason_code: str
    source_identity: str
    condition_key: str = field(init=False)
    review_key: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReviewKind):
            raise InputValidationError(REVIEW_REFERENCE_INVALID, "kind is a ReviewKind")
        if not isinstance(self.producer, str) or not _PRODUCER.fullmatch(self.producer):
            raise InputValidationError(REVIEW_REFERENCE_INVALID, "producer is a producer name")
        scope = canonical_scope(self.scope)
        object.__setattr__(self, "scope", scope)
        _identifier(self.subject, "subject")
        _identifier(self.reason_code, "reason_code")
        _identifier(self.source_identity, "source_identity")
        condition = {
            "kind": self.kind.value,
            "producer": self.producer,
            "scope": scope,
            "subject": self.subject,
            "reason_code": self.reason_code,
        }
        object.__setattr__(self, "condition_key", _digest(condition))
        object.__setattr__(
            self,
            "review_key",
            _digest({"condition_key": self.condition_key, "source": self.source_identity}),
        )

    def scope_json(self) -> str:
        return _canonical(dict(self.scope))
