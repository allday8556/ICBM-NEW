"""The provider seams REGISTER owns and an adapter implements (ADR-0014 §25; M5 PR-D).

REGISTER never imports a marketplace adapter, an HTTP client or a CONNECT service — the contract
test ``test_no_register_module_reaches_a_provider`` enforces it — so every provider capability
reaches the domain as one of the Protocols below. PR-D implements them for SmartStore under
``integrations/marketplaces/smartstore/``; PR-E wires them into execution.

Each seam is written so that *not having* the capability is expressible without lying:

* a duplicate source with no admissible evidence answers with none and says why — a stale or a
  missing store-catalog snapshot (ADR-0042 §6) — never a fabricated ``NO_MATCH``, and PR-C then
  never reaches READY where proof is required;
* an upload whose outcome is ambiguous returns no ``PreparedAsset``, so no provider asset identity
  is invented and the final preflight stays short of READY;
* a read-back that cannot be normalized is ``UNREADABLE``, which is never a confirmation.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.platform.core.errors import ErrorClass
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.register.policy import CATALOG_MAX_AGE, DuplicateKeyKind
from app.stages.register.preparation import DuplicateEvidence, PreflightResult, PreparedAsset


@dataclass(frozen=True)
class DuplicateUnit:
    """The one provider-listing unit a duplicate check is asked about (ADR-0014 §13; ADR-0042 §6).

    ``item_ids`` name the unit's Items (their sources carry the owner-declared seller-code
    conventions, ADR-0024 §2), ``listing_name`` is the authored listing name (a weak signal only)
    and ``max_age`` is the target policy's store-catalog bound (ADR-0042 §5.2). ``group_ids`` are
    the unit's common products: a store listing linked to one of them is a duplicate (ADR-0042 §6,
    S2a)."""

    marketplace_key: str
    marketplace_account_id: str
    listing_identity: str
    item_ids: tuple[str, ...] = ()
    listing_name: str | None = None
    max_age: timedelta = CATALOG_MAX_AGE
    group_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchedListing:
    """One store listing a duplicate check matched, for the seller's screen only. Never a
    fingerprint input: the evidence carries the verdict-relevant part."""

    key_kind: DuplicateKeyKind
    marketplace_product_id: str
    name: str | None = None
    sale_status: str | None = None


@dataclass(frozen=True)
class DuplicateCheck:
    """What the duplicate-evidence owner can say now about one unit (ADR-0042 §6).

    ``evidence`` exists only for a ``COMPLETE``, fresh snapshot; otherwise ``problem`` names why
    there is none (``DUPLICATE_EVIDENCE_STALE`` / ``DUPLICATE_EVIDENCE_UNAVAILABLE``) and
    ``resyncing`` whether the automatic re-sync is running. ``as_of`` is the start of the last
    COMPLETE sync, shown wherever a duplicate result is (SCP-07)."""

    evidence: DuplicateEvidence | None
    problem: str | None = None
    as_of: datetime | None = None
    resyncing: bool = False
    matched: tuple[MatchedListing, ...] = ()
    # Set only when the snapshot is stale **because** an ICBM mutation of the store started at or
    # after its start (it is otherwise still within its age bound): that mutation's start. The
    # CREATE send path waits for the re-sync then, instead of refusing (ADR-0042 §5.2).
    stale_since_mutation: datetime | None = None
    # ADR-0042 §5.2 tightening: no NO_MATCH because the seller has not confirmed (or a later sync
    # invalidated) the store's catalog coverage in 「스토어 상품」.
    coverage_unconfirmed: bool = False


def duplicate_unit(candidate: PreflightResult) -> DuplicateUnit:
    """The duplicate question one evaluated unit asks: its scope, Items, name and the target
    policy's store-snapshot bound."""
    unit = candidate.resolved
    name = candidate.request.listing.name
    return DuplicateUnit(
        marketplace_key=unit.marketplace_key,
        marketplace_account_id=unit.marketplace_account_id,
        listing_identity=unit.listing_identity,
        item_ids=tuple(unit.unit_item_ids),
        listing_name=None if name is None else str(name.value),
        max_age=unit.target.catalog_max_age,
        group_ids=tuple(sorted({item.product_group_id for item in unit.items})),
    )


@runtime_checkable
class DuplicateLookupSource(Protocol):
    """The duplicate-evidence source of one marketplace (ADR-0014 §13, amended by ADR-0042 §6)."""

    def available(self) -> bool:
        """Whether evidence can exist now: for SmartStore, while a COMPLETE, fresh store-catalog
        snapshot exists for the bound account. ``False`` keeps the caller fail-closed."""
        ...

    def check(
        self, unit: DuplicateUnit, *, spot_check: bool = False, resync: bool = True
    ) -> DuplicateCheck:
        """The current evidence of one unit, or none with the reason. Never a fabricated
        ``NO_MATCH``. ``spot_check`` adds the positive-only seller-code search of the first
        CREATE copy (ADR-0042 §6): a hit is a match, zero hits change nothing. ``resync=False``
        reads without asking for the automatic re-sync (a background reader such as the review
        producer)."""
        ...

    def truth_token(self, marketplace_account_id: str, max_age: timedelta) -> str:
        """Everything this owner's answers for the account depend on now, besides the unit's own
        inputs: it moves whenever an answer could (a new COMPLETE sync, a freshness change, an
        ICBM mutation of the store)."""
        ...


@runtime_checkable
class PreparedAssetSource(Protocol):
    """Known provider assets for the selected exact bytes in one canonical account."""

    def prepared_assets(
        self,
        *,
        marketplace_key: str,
        marketplace_account_id: str,
        preparation_revision_id: str,
        candidate_fingerprint: str,
        selected_artifacts: tuple[tuple[str, str, str], ...],
        asset_profile: str,
    ) -> tuple[PreparedAsset, ...]: ...


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
        """Whether this comparison can prove a published state at all.

        ADR-0014 §11 confirms a registration only on an exact published-state comparison against
        the explicitly expected state, and the execution owner refuses with
        ``REGISTER_PUBLISHED_STATE_UNPROVEN`` rather than invent one. ``False`` is the honest
        answer while either half is missing — the read-back cannot carry the state, or nothing
        states the expected one — so a readiness that names the proofs a real CREATE needs can
        report it as missing instead of implying that endpoint adoption alone covers it.
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
    "DuplicateCheck",
    "DuplicateEvidence",
    "DuplicateLookupSource",
    "DuplicateUnit",
    "LookupStatus",
    "MatchedListing",
    "PreparedAsset",
    "PreparedAssetSource",
    "ProviderAssetSource",
    "ReadbackComparator",
    "ReadbackSource",
    "ReconcileLookup",
    "WireProjector",
    "duplicate_unit",
]
