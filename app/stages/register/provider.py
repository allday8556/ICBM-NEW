"""The provider seams REGISTER owns and an adapter implements (ADR-0014 §25; M5 PR-D).

REGISTER never imports a marketplace adapter, an HTTP client or a CONNECT service — the contract
test ``test_no_register_module_reaches_a_provider`` enforces it — so every provider capability
reaches the domain as one of the Protocols below. PR-D implements them for SmartStore under
``integrations/marketplaces/smartstore/``; PR-E wires them into execution.

Each seam is written so that *not having* the capability is expressible without lying:

* a duplicate lookup that is not adopted raises instead of returning ``NO_MATCH``, and PR-C then
  sees absent evidence — ``DUPLICATE_EVIDENCE_MISSING``, never READY where proof is required;
* an upload whose outcome is ambiguous returns no ``PreparedAsset``, so no provider asset identity
  is invented and the final preflight stays short of READY;
* a read-back that cannot be normalized is ``UNREADABLE``, which is never a confirmation.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.platform.core.errors import ErrorClass
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.register.preparation import DuplicateEvidence, PreparedAsset


@runtime_checkable
class DuplicateLookupSource(Protocol):
    """A provider duplicate lookup, scoped to one canonical account and listing identity."""

    def available(self) -> bool:
        """Whether a lookup contract is adopted at all. ``False`` keeps the caller fail-closed."""
        ...

    def evidence(
        self, *, marketplace_account_id: str, listing_identity: str
    ) -> DuplicateEvidence | None:
        """The evidence of one lookup, or a raised error. Never a fabricated ``NO_MATCH``."""
        ...


@runtime_checkable
class ProviderAssetSource(Protocol):
    """Promotion of one upload outcome to the provider asset identity of one exact artifact."""

    def promote(
        self,
        retained: Mapping[str, Any],
        *,
        asset_kind: Any,
        sha256: str,
        derivation_id: str | None,
        asset_profile: str,
        candidate_fingerprint: str,
    ) -> Any:
        """An outcome whose ``asset`` is a :class:`PreparedAsset` only when unambiguous."""
        ...


@runtime_checkable
class ReadbackComparator(Protocol):
    """Normalization of a provider read-back and its comparison with a frozen Snapshot."""

    def compare(self, snapshot_payload: Mapping[str, Any], retained: Mapping[str, Any]) -> Any:
        """A verdict plus its sanitized evidence, compared against the Snapshot only."""
        ...

    def proves_published_state(self) -> bool:
        """Whether this comparison's canonical evidence can carry a published state at all.

        ADR-0014 §11 confirms a registration only on an exact published-state comparison, and the
        execution owner refuses with ``REGISTER_PUBLISHED_STATE_UNPROVEN`` rather than invent one.
        ``False`` is the honest answer while the adopted read-back contract proves no published
        state, so a readiness that names the proofs a real CREATE needs can report it as missing
        instead of implying that endpoint adoption alone covers it.
        """
        ...


@dataclass(frozen=True)
class CreateHandoff:
    """What one CREATE handoff proved (ADR-0014 §9; PR-E kickoff §4).

    ``error_class`` (the cause) and ``remote_outcome`` (whether the mutation happened) are
    independent axes. A sender that cannot prove the mutation did not happen reports
    ``UNKNOWN``; it must never infer ``NOT_APPLIED_PROVEN`` from a timeout, a 5xx or an
    exception type. ``marketplace_product_id`` exists exactly when the outcome is applied.

    Both mappings are the **sanitized** canonical representations the durable digests are taken
    over (§15): never wire bytes, never a header, never a token.
    """

    remote_outcome: RemoteOutcome
    sanitized_request: Mapping[str, Any]
    marketplace_product_id: str | None = None
    # The provider's channel identity of the same listing, when the response names it
    # (for SmartStore the STOREFARM ``smartstoreChannelProductNo``; Issue #89 5904349289 §B).
    # Only an applied outcome carries it, and it is never guessed.
    marketplace_channel_product_id: str | None = None
    response_status: int | None = None
    sanitized_response: Mapping[str, Any] | None = None
    error_class: ErrorClass | None = None
    error_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class CreateSender(Protocol):
    """The provider CREATE handoff. Unavailable until the provider contract is adopted."""

    def available(self) -> bool:
        """Whether a CREATE may be handed off at all. ``False`` keeps the caller fail-closed."""
        ...

    def send(
        self, *, payload: Mapping[str, Any], idempotency_key: str, listing_identity: str
    ) -> CreateHandoff:
        """Hand one frozen Snapshot payload to the provider, or raise before any transport."""
        ...


@runtime_checkable
class ReadbackSource(Protocol):
    """The adopted read-back of a known provider product identity (PR-D)."""

    def available(self) -> bool: ...

    def read(self, *, marketplace_product_id: str) -> Mapping[str, Any]:
        """The retained, sanitized response of one read-back."""
        ...


@runtime_checkable
class WireProjector(Protocol):
    """The provider wire projection of one frozen Snapshot payload (PR-D)."""

    def __call__(self, payload: Mapping[str, Any]) -> Any:
        """A projection whose ``sendable`` is false while any documented gap remains."""
        ...


class LookupStatus(StrEnum):
    """What one reconcile lookup managed to observe (ADR-0014 §28.2, §28.4).

    ``COMPLETE`` means every page was enumerated consistently, so its exact-candidate count is
    trustworthy — and even then zero is never absence. ``UNAVAILABLE`` (no session, a rate or quota
    refusal, a result too large for the bounded read budget) and ``ERROR`` (anything else) prove
    nothing at all.
    """

    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


@runtime_checkable
class ReconcileLookup(Protocol):
    """The positive-only reconcile lookup by the stable listing identity (§10, §28.2).

    ``find`` returns sanitized evidence with, at least:

    * ``status`` — a :class:`LookupStatus` value;
    * ``code`` — the reason when the status is not ``COMPLETE``;
    * ``candidates`` — only when ``COMPLETE``: every provider listing whose seller code is
      **exactly** this listing identity's provider projection, each as ``origin_product_no`` and
      ``channel_product_no`` (strings); never the provider's similar or partial matches.

    It never raises for a provider or session failure: that is ``UNAVAILABLE`` or ``ERROR``, which
    prove nothing. A lookup never proves remote absence (§17.2) and never authorizes a CREATE.
    """

    def available(self) -> bool:
        """Whether the lookup contract is adopted at all. ``False`` keeps the path fail-closed."""
        ...

    def find(self, *, marketplace_account_id: str, listing_identity: str) -> Mapping[str, Any]:
        """The sanitized evidence of one lookup. Never a fabricated absence."""
        ...

    def confirms_candidate(
        self, *, listing_identity: str, retained_readback: Mapping[str, Any]
    ) -> bool:
        """Whether a candidate's read-back carries exactly this listing identity's provider code
        — the second half of §28.2's presence proof. ``False`` for anything unreadable."""
        ...


__all__ = [
    "CreateHandoff",
    "CreateSender",
    "DuplicateEvidence",
    "DuplicateLookupSource",
    "LookupStatus",
    "PreparedAsset",
    "ProviderAssetSource",
    "ReadbackComparator",
    "ReadbackSource",
    "ReconcileLookup",
    "WireProjector",
]
