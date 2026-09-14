"""SMARTSTORE-A0-PERMISSION: operator-attested permission evidence (M2 PR-C).

Contract: PERMISSIONS_SCOPES.md §5, §7, §8, §14 and §18; CAPABILITY_MAPPING.md §5 (S1) and §17
#18; M2 instructions §6. An attestation is OPERATOR_ATTESTED_EVIDENCE (SOURCES.md A0): it proves
only that ICBM received and handled what the operator observed in Commerce API Center. It is
never provider runtime truth, never R0 and never MACHINE_VERIFIED, and it cannot make product
write READY.

This module is pure: no I/O and no clock. Current use is always re-derived from the stored
evidence against what is current *now*; any mismatch fails closed to write_scope UNKNOWN and never
fabricates MISSING (§6.5).
"""

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.connect.marketplace.capability import (
    PERMISSION_UNKNOWN,
    CapabilityInvariantError,
    ContractDecision,
    ContractFreshness,
    EvidenceStrength,
    WriteScope,
    WriteScopeStatus,
    freshness_allows,
)


class ApiGroup(StrEnum):
    """The M2 SmartStore API groups (PERMISSIONS_SCOPES §4), with ASCII keys for durable state."""

    SELLER_INFO = "SELLER_INFO"
    PRODUCT = "PRODUCT"
    ORDER_SELLER = "ORDER_SELLER"
    INQUIRY = "INQUIRY"


# The names the operator sees in Commerce API Center (PERMISSIONS_SCOPES §4).
API_GROUP_LABELS: dict[ApiGroup, str] = {
    ApiGroup.SELLER_INFO: "판매자정보",
    ApiGroup.PRODUCT: "상품",
    ApiGroup.ORDER_SELLER: "주문 판매자",
    ApiGroup.INQUIRY: "문의",
}

# PERMISSIONS_SCOPES §5.1 and Q4: the M2 product-registration baseline. ENDPOINT_MATRIX §5 keeps it
# out of the endpoint registry, which adopts no product endpoint in M2; the final union is frozen
# only when M5 adopts the product endpoints.
PRODUCT_REGISTRATION_REQUIRED_GROUPS: frozenset[ApiGroup] = frozenset({ApiGroup.PRODUCT})

SMARTSTORE_PROVIDER = "SMARTSTORE"
SELF_AUTH_MODE = "SELF"
_MIN_FINGERPRINT_KEY_BYTES = 32


class EvidenceSource(StrEnum):
    OPERATOR_ATTESTED_PROVIDER_ADMIN = "OPERATOR_ATTESTED_PROVIDER_ADMIN"


class EvidenceFreshness(StrEnum):
    """§8: whether the observation is still inside the configured evidence-age policy."""

    FRESH = "FRESH"
    EXPIRED = "EXPIRED"
    # No bounded age policy is configured, so the evidence can never be current (§8, Q5).
    NO_AGE_POLICY = "NO_AGE_POLICY"


class Invalidation(StrEnum):
    """Why evidence cannot authorize current use (§6.5; PERMISSIONS_SCOPES §7.1, §8, §14)."""

    APPLICATION_NOT_CONFIGURED = "APPLICATION_NOT_CONFIGURED"
    APPLICATION_FINGERPRINT_MISMATCH = "APPLICATION_FINGERPRINT_MISMATCH"
    REQUIRED_GROUPS_CHANGED = "REQUIRED_GROUPS_CHANGED"
    MAPPING_REVISION_UNAVAILABLE = "MAPPING_REVISION_UNAVAILABLE"
    MAPPING_REVISION_CHANGED = "MAPPING_REVISION_CHANGED"
    EXPIRED = "EXPIRED"
    NO_AGE_POLICY = "NO_AGE_POLICY"
    MALFORMED = "MALFORMED"


class AttestationRefusedError(CapabilityInvariantError):
    """An attestation that cannot be recorded at all: nothing is stored (§6.7)."""

    def __init__(self, reason: Invalidation) -> None:
        super().__init__(f"attestation refused: {reason}")
        self.reason = reason


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CapabilityInvariantError(message)


def _groups(values: Iterable[object], name: str) -> frozenset[ApiGroup]:
    groups = frozenset(values)
    _require(all(isinstance(g, ApiGroup) for g in groups), f"{name} must be ApiGroup values")
    return frozenset(g for g in groups if isinstance(g, ApiGroup))


@dataclass(frozen=True)
class ApplicationIdentity:
    """The configured provider application (PERMISSIONS_SCOPES §7.1). PR-A supplies it."""

    provider: str
    auth_mode: str
    client_id: str

    def __post_init__(self) -> None:
        _require(
            all(
                isinstance(v, str) and v.strip()
                for v in (self.provider, self.auth_mode, self.client_id)
            ),
            "an application identity has a provider, an auth mode and a client_id",
        )


def application_fingerprint(identity: ApplicationIdentity, key: bytes) -> str:
    """Non-reversible keyed fingerprint of ``provider | auth_mode | client_id`` (§7.1).

    It binds evidence to the configured application without storing the client_id. The key
    lives in the OS secret store; this is not an authentication secret or an account identity.
    """
    _require(len(key) >= _MIN_FINGERPRINT_KEY_BYTES, "the fingerprint key must be 256-bit")
    material = "|".join((identity.provider, identity.auth_mode, identity.client_id.strip()))
    return hmac.new(key, material.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Attestation:
    """One recorded operator attestation: the evidence envelope (§5, §7)."""

    observed_at: datetime
    application_fingerprint: str
    required_groups: frozenset[ApiGroup]
    observed_groups: frozenset[ApiGroup]
    endpoint_mapping_revision: str
    evidence_source: EvidenceSource = EvidenceSource.OPERATOR_ATTESTED_PROVIDER_ADMIN
    # §17 #18: the only strength an attestation can ever carry; A0 is never promoted.
    evidence_strength: EvidenceStrength = EvidenceStrength.OPERATOR_ATTESTED

    def __post_init__(self) -> None:
        _require(isinstance(self.evidence_source, EvidenceSource), "unknown evidence source")
        _require(
            self.evidence_strength is EvidenceStrength.OPERATOR_ATTESTED,
            "an attestation is OPERATOR_ATTESTED evidence, never MACHINE_VERIFIED (A0)",
        )
        _require(self.observed_at.tzinfo is not None, "observed_at must be timezone-aware")
        _require(bool(self.application_fingerprint), "an attestation is bound to an application")
        _require(
            bool(self.endpoint_mapping_revision), "an attestation is bound to a mapping revision"
        )
        object.__setattr__(
            self, "required_groups", _groups(self.required_groups, "required_groups")
        )
        object.__setattr__(
            self, "observed_groups", _groups(self.observed_groups, "observed_groups")
        )
        _require(bool(self.required_groups), "the required group set is never empty")

    @property
    def attested_status(self) -> WriteScopeStatus:
        """Positive evidence either way (§5.1, §5.2): every required group observed, or at least
        one visibly absent. An attestation never says UNKNOWN."""
        if self.required_groups <= self.observed_groups:
            return WriteScopeStatus.READY
        return WriteScopeStatus.MISSING


@dataclass(frozen=True)
class AttestationContext:
    """What is current now. Stored evidence applies only while it still matches it."""

    now: datetime
    application_fingerprint: str | None
    required_groups: frozenset[ApiGroup]
    endpoint_mapping_revision: str | None
    max_age: timedelta | None


def refusal(context: AttestationContext) -> Invalidation | None:
    """Why no attestation can be recorded right now (§6.7): evidence needs an application
    fingerprint and a mapping revision to bind to, and neither may be typed in by hand."""
    if context.application_fingerprint is None:
        return Invalidation.APPLICATION_NOT_CONFIGURED
    if context.endpoint_mapping_revision is None:
        return Invalidation.MAPPING_REVISION_UNAVAILABLE
    return None


def attest(observed_groups: Iterable[object], context: AttestationContext) -> Attestation:
    """Build a new attestation. ``observed_at``, the fingerprint, the required groups and the
    mapping revision all come from the context; only the observed groups come from the operator.
    """
    reason = refusal(context)
    if reason is not None or context.application_fingerprint is None:
        raise AttestationRefusedError(reason or Invalidation.APPLICATION_NOT_CONFIGURED)
    if context.endpoint_mapping_revision is None:
        raise AttestationRefusedError(Invalidation.MAPPING_REVISION_UNAVAILABLE)
    return Attestation(
        observed_at=context.now,
        application_fingerprint=context.application_fingerprint,
        required_groups=context.required_groups,
        observed_groups=_groups(observed_groups, "observed_groups"),
        endpoint_mapping_revision=context.endpoint_mapping_revision,
    )


def freshness(observed_at: datetime, now: datetime, max_age: timedelta | None) -> EvidenceFreshness:
    """§8: fresh only inside a bounded age policy; with no policy, never fresh."""
    if max_age is None:
        return EvidenceFreshness.NO_AGE_POLICY
    return EvidenceFreshness.FRESH if now - observed_at <= max_age else EvidenceFreshness.EXPIRED


@dataclass(frozen=True)
class AttestationEvaluation:
    """The evidence judged against the current context."""

    write_scope: WriteScope
    # None only when the stored evidence itself is malformed.
    freshness: EvidenceFreshness | None
    invalidations: frozenset[Invalidation]

    @property
    def current(self) -> bool:
        return not self.invalidations


class Promotion(StrEnum):
    """What the current evaluation did to capability truth — the A0 projection's answer to
    "stored, but did it count?" (§6.3, CAPABILITY_MAPPING F5)."""

    NO_EVIDENCE = "NO_EVIDENCE"
    APPLIED = "APPLIED"
    # Evidence is on record but invalidated, so write_scope is UNKNOWN.
    NOT_CURRENT = "NOT_CURRENT"
    # Evidence is stored and current, but promoting write_scope to READY is new trust and the
    # contract freshness is not CURRENT (F5): the evidence waits, write_scope stays as it was.
    BLOCKED_BY_CONTRACT_FRESHNESS = "BLOCKED_BY_CONTRACT_FRESHNESS"
    # Evaluated but not yet converged (transient: the next read converges it).
    PENDING = "PENDING"


MALFORMED_EVALUATION = AttestationEvaluation(
    PERMISSION_UNKNOWN, None, frozenset({Invalidation.MALFORMED})
)


def promotion(
    evaluation: AttestationEvaluation | None,
    capability_scope: WriteScope,
    contract_freshness: ContractFreshness,
) -> Promotion:
    """What the evidence did to capability truth. A stored, current READY that capability does
    not yet hold because F5 blocks new trust is reported as such — never as "unknown"."""
    if evaluation is None:
        return Promotion.NO_EVIDENCE
    if not evaluation.current:
        return Promotion.NOT_CURRENT
    if capability_scope == evaluation.write_scope:
        return Promotion.APPLIED
    if evaluation.write_scope.status is WriteScopeStatus.READY and not freshness_allows(
        contract_freshness, ContractDecision.PROMOTE_UNVERIFIED_CAPABILITY
    ):
        return Promotion.BLOCKED_BY_CONTRACT_FRESHNESS
    return Promotion.PENDING


def evaluate(attestation: Attestation, context: AttestationContext) -> AttestationEvaluation:
    """Current use of stored evidence (§6.5). Any invalidation fails closed to UNKNOWN; the
    evidence itself stays on record."""
    invalid: set[Invalidation] = set()
    if context.application_fingerprint is None:
        invalid.add(Invalidation.APPLICATION_NOT_CONFIGURED)
    elif not hmac.compare_digest(
        attestation.application_fingerprint, context.application_fingerprint
    ):
        invalid.add(Invalidation.APPLICATION_FINGERPRINT_MISMATCH)
    if attestation.required_groups != context.required_groups:
        invalid.add(Invalidation.REQUIRED_GROUPS_CHANGED)
    if context.endpoint_mapping_revision is None:
        invalid.add(Invalidation.MAPPING_REVISION_UNAVAILABLE)
    elif attestation.endpoint_mapping_revision != context.endpoint_mapping_revision:
        invalid.add(Invalidation.MAPPING_REVISION_CHANGED)
    if attestation.observed_at > context.now:
        invalid.add(Invalidation.MALFORMED)  # recorded "later" than now: validity unknowable
    state = freshness(attestation.observed_at, context.now, context.max_age)
    if state is EvidenceFreshness.EXPIRED:
        invalid.add(Invalidation.EXPIRED)
    elif state is EvidenceFreshness.NO_AGE_POLICY:
        invalid.add(Invalidation.NO_AGE_POLICY)
    scope = (
        PERMISSION_UNKNOWN
        if invalid
        else WriteScope(attestation.attested_status, EvidenceStrength.OPERATOR_ATTESTED)
    )
    return AttestationEvaluation(scope, state, frozenset(invalid))
