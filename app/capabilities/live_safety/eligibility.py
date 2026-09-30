"""The durable canary-eligibility owner (ADR-0018 §5.1; Issue #89 resolution ``5910018106``).

The first bounded canary may use only a product proven outside every regulated category in scope
(ADR-0018 §5). That is a **canary-local restriction**, read by no other owner: it is never a
``COMPLIANCE PASS``, it adds no compliance meaning to ``CategoryMetadata``, and no ComplianceGate
is implemented here.

**What counts as proof.** Never an operator assertion. The server builds a deterministic
**eligibility review packet** for one exact canary lineage from the canonical owners — the account,
the exact preparation revision and its candidate fingerprint, the taxonomy, the category, the
current reviewed category-metadata revision with the facts that make it usable, and the sanitized
publication-facing text of the preparation — and digests it. A record stores that digest and the
closed v1 checklist reviewed against it. It proves ``CANARY_NON_REGULATED`` only when every scope
key is ``OUTSIDE_SCOPE`` with an admissible evidence kind and a reference.

**Exact lineage, automatic staleness.** A record is read back only through a binding the server
re-derives from current owner truth: the same account, preparation revision, candidate
fingerprint, taxonomy, category, metadata revision, scope version and packet digest. Any drift
changes the binding, so an earlier record simply no longer matches; nothing revives it, and a
missing, unreadable or ambiguous owner state is unproven. ASSET and CREATE use the same binding:
the ASSET stage from the preparation's current candidate, the CREATE stage from the final preflight
that reproduces the Intent's Snapshot, whose candidate fingerprint is the one its assets were
prepared under.

A binding is derived **before** the unit that admits a mutation — a preflight never runs inside a
write unit — and the owner-write fence of that unit proves no owner wrote in between (§4.3). Inside
the unit only the record and the immutable lineage rows are read.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol

from sqlalchemy.exc import SQLAlchemyError

from app.capabilities.live_safety.model import MutationStage
from app.capabilities.live_safety.store import LiveAuthorityStore
from app.platform.core.errors import InputValidationError, NotFoundError
from app.stages.register.model import RegistrationConflictError, sanitized_digest
from app.stages.register.preparation import PreflightResult
from app.stages.register.sanitize import PayloadSanitationError, require_clean
from app.stages.register.store import RegistrationStore

PACKET_VERSION: Final = "canary-eligibility-review-packet/v1"
# The regulated scope a canary of each marketplace is reviewed against. A marketplace without one
# has no eligibility proof at all.
SCOPE_VERSIONS: Final[Mapping[str, str]] = {"smartstore": "smartstore-canary-nonregulated/v1"}
MAX_REFERENCE_LENGTH: Final = 200

PACKET_UNAVAILABLE: Final = "CANARY_ELIGIBILITY_PACKET_UNAVAILABLE"
PACKET_MOVED: Final = "CANARY_ELIGIBILITY_PACKET_MOVED"
TRUTH_MOVED: Final = "CANARY_ELIGIBILITY_TRUTH_MOVED"
CHECKLIST_INVALID: Final = "CANARY_ELIGIBILITY_CHECKLIST_INVALID"
EVIDENCE_REFUSED: Final = "CANARY_ELIGIBILITY_EVIDENCE_REFUSED"


class ScopeKey(StrEnum):
    """The closed v1 checklist. The last key keeps the four named ones from becoming an
    accidental exhaustive legal taxonomy."""

    HEALTH_FUNCTIONAL_FOOD = "HEALTH_FUNCTIONAL_FOOD"
    KC_CERTIFICATION_REQUIRED = "KC_CERTIFICATION_REQUIRED"
    MFDS_NOTICE_OR_APPROVAL = "MFDS_NOTICE_OR_APPROVAL"
    PROHIBITED_OR_RESTRICTED_WORDING = "PROHIBITED_OR_RESTRICTED_WORDING"
    OTHER_REGULATED_OR_RESTRICTED_CATEGORY = "OTHER_REGULATED_OR_RESTRICTED_CATEGORY"


class Finding(StrEnum):
    OUTSIDE_SCOPE = "OUTSIDE_SCOPE"
    IN_SCOPE = "IN_SCOPE"
    UNKNOWN = "UNKNOWN"


class EvidenceKind(StrEnum):
    """What may ground an exclusion. An operator assertion is not among them."""

    # Exactly the reviewed metadata revision the packet names.
    CATEGORY_METADATA = "CATEGORY_METADATA"
    # A stable reviewed reference to the rule or evidence used for the exclusion.
    OFFICIAL_RULE = "OFFICIAL_RULE"
    # Exactly the server-built packet, by its digest, for the wording and content review.
    LISTING_REVIEW_PACKET = "LISTING_REVIEW_PACKET"


class EligibilityVerdict(StrEnum):
    PROVEN_OUTSIDE = "PROVEN_OUTSIDE"
    UNPROVEN = "UNPROVEN"


# ------------------------------------------------------------------ the lineage binding


@dataclass(frozen=True)
class EligibilityBinding:
    """One exact canary lineage, as the server derived it from current owner truth."""

    unit_ref: str
    marketplace_key: str
    marketplace_account_id: str
    preparation_revision_id: str
    candidate_fingerprint: str
    taxonomy_revision: str
    category_id: str
    category_metadata_revision: str
    scope_version: str
    review_packet_digest: str


@dataclass(frozen=True)
class ReviewPacket:
    """The packet an operator reviews, its digest and the lineage it binds."""

    packet: Mapping[str, Any]
    digest: str
    binding: EligibilityBinding


@dataclass(frozen=True)
class EligibilityRecord:
    eligibility_id: str
    marketplace_key: str
    marketplace_account_id: str
    preparation_revision_id: str
    candidate_fingerprint: str
    taxonomy_revision: str
    category_id: str
    category_metadata_revision: str
    scope_version: str
    seq: int
    review_packet_digest: str
    checks: Mapping[str, Any]
    verdict: EligibilityVerdict
    recorded_by: str
    recorded_at: datetime


def asset_unit_ref(preparation_revision_id: str) -> str:
    return f"preparation_revision:{preparation_revision_id}"


def create_unit_ref(intent_id: str) -> str:
    return f"registration_intent:{intent_id}"


def review_packet(
    result: PreflightResult, preparation_revision_id: str, *, unit_ref: str
) -> ReviewPacket | None:
    """The deterministic review packet of the unit this evaluation resolved, or ``None``.

    ``None`` means no packet can be built, so nothing can be proven: no scope is defined for the
    marketplace, no category is chosen, the category has no current metadata revision that is
    operator-reviewed, leaf and registrable for exactly this taxonomy and category, or the
    publication text is not sanitized. It is pure: it reads nothing but the evaluation.
    """
    unit, request = result.resolved, result.request
    scope_version = SCOPE_VERSIONS.get(unit.marketplace_key)
    category, metadata, target = request.category, unit.metadata, unit.target
    if scope_version is None or category is None or metadata is None:
        return None
    if not (metadata.reviewed and metadata.leaf and metadata.registrable):
        return None
    if (
        metadata.category_id != category.category_id
        or metadata.taxonomy_revision != target.taxonomy_revision
        or category.taxonomy_revision != target.taxonomy_revision
        or not preparation_revision_id
    ):
        return None
    listing, detail = request.listing, request.detail
    packet: dict[str, Any] = {
        "packet_version": PACKET_VERSION,
        "scope_version": scope_version,
        "marketplace_key": unit.marketplace_key,
        "marketplace_account_id": unit.marketplace_account_id,
        "preparation_revision_id": preparation_revision_id,
        "candidate_fingerprint": result.candidate_fingerprint,
        "taxonomy_revision": target.taxonomy_revision,
        "category_id": category.category_id,
        "category_metadata_revision": metadata.metadata_revision,
        # The preflight reads only the current revision of a category (ADR-0015 §3), so the
        # revision an evaluation resolved is the current one.
        "category_metadata": {"current": True, "reviewed": True, "leaf": True, "registrable": True},
        "publication_text": {
            "name": None if listing.name is None else listing.name.canonical(),
            "tags": sorted(listing.tags),
            "attributes": {k: v.canonical() for k, v in sorted(listing.attributes.items())},
            "notices": {k: v.canonical() for k, v in sorted(listing.notices.items())},
            "options": {
                item: dict(sorted(values.items()))
                for item, values in sorted(listing.options.items())
            },
            "detail": None
            if detail is None
            else {"sections": list(detail.sections), "body": detail.body},
        },
        "checklist": [key.value for key in ScopeKey],
    }
    try:
        require_clean(packet, "canary_eligibility_packet")
    except PayloadSanitationError:
        return None
    digest = sanitized_digest(packet)
    return ReviewPacket(
        packet=packet,
        digest=digest,
        binding=EligibilityBinding(
            unit_ref=unit_ref,
            marketplace_key=unit.marketplace_key,
            marketplace_account_id=unit.marketplace_account_id,
            preparation_revision_id=preparation_revision_id,
            candidate_fingerprint=result.candidate_fingerprint,
            taxonomy_revision=target.taxonomy_revision,
            category_id=category.category_id,
            category_metadata_revision=metadata.metadata_revision,
            scope_version=scope_version,
            review_packet_digest=digest,
        ),
    )


def binding_of(
    result: PreflightResult | None, preparation_revision_id: str | None, *, unit_ref: str
) -> EligibilityBinding | None:
    """The lineage binding of one evaluation, or ``None`` when no packet can be built."""
    if result is None or not preparation_revision_id:
        return None
    built = review_packet(result, preparation_revision_id, unit_ref=unit_ref)
    return None if built is None else built.binding


# ------------------------------------------------------------------ the closed checklist


@dataclass(frozen=True)
class _Check:
    finding: Finding
    evidence_kind: EvidenceKind | None
    evidence_ref: str | None


def _refusal(code: str, message: str, **details: object) -> InputValidationError:
    return InputValidationError(code, message, details=dict(details))


def validate_checks(
    checks: Mapping[str, Any], packet: ReviewPacket
) -> tuple[dict[str, dict[str, str | None]], EligibilityVerdict]:
    """The canonical closed checklist and the verdict it supports, or a refusal of the whole
    record. Exactly the v1 scope keys, nothing else; an evidence kind outside the admitted three
    — an operator assertion included — is refused, never stored."""
    if not isinstance(checks, Mapping) or set(checks) != {key.value for key in ScopeKey}:
        raise _refusal(
            CHECKLIST_INVALID,
            "the checklist is exactly the v1 scope keys: none missing, none added",
            expected=[key.value for key in ScopeKey],
        )
    canonical: dict[str, dict[str, str | None]] = {}
    proven = True
    for key in ScopeKey:
        raw = checks[key.value]
        kind = raw.get("evidence_kind") if isinstance(raw, Mapping) else None
        if isinstance(kind, str) and kind not in {member.value for member in EvidenceKind}:
            raise _refusal(
                EVIDENCE_REFUSED,
                "the evidence kind is not admissible; an operator assertion is never evidence",
                key=key.value,
            )
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"finding", "evidence_kind", "evidence_ref"}
            or raw["finding"] not in {member.value for member in Finding}
            or not isinstance(raw["evidence_kind"], str | None)
            or not isinstance(raw["evidence_ref"], str | None)
        ):
            raise _refusal(
                CHECKLIST_INVALID,
                "a check names exactly a finding, an evidence kind and an evidence reference",
                key=key.value,
            )
        check = _Check(
            finding=Finding(raw["finding"]),
            evidence_kind=None if kind is None else EvidenceKind(kind),
            evidence_ref=raw["evidence_ref"],
        )
        reference = None if check.evidence_ref is None else check.evidence_ref.strip()
        if (check.evidence_kind is None) != (reference is None):
            raise _refusal(
                CHECKLIST_INVALID, "an evidence kind and its reference go together", key=key.value
            )
        if check.evidence_kind is not None:
            assert reference is not None
            _require_reference(key, check.evidence_kind, reference, packet)
        if check.finding is Finding.OUTSIDE_SCOPE and check.evidence_kind is None:
            raise _refusal(
                EVIDENCE_REFUSED,
                "an exclusion is recorded only with admissible evidence, never asserted",
                key=key.value,
            )
        proven = proven and check.finding is Finding.OUTSIDE_SCOPE
        canonical[key.value] = {
            "finding": check.finding.value,
            "evidence_kind": None if check.evidence_kind is None else check.evidence_kind.value,
            "evidence_ref": reference,
        }
    return canonical, (EligibilityVerdict.PROVEN_OUTSIDE if proven else EligibilityVerdict.UNPROVEN)


def _require_reference(
    key: ScopeKey, kind: EvidenceKind, reference: str, packet: ReviewPacket
) -> None:
    if not reference or len(reference) > MAX_REFERENCE_LENGTH:
        raise _refusal(
            EVIDENCE_REFUSED, "an evidence reference is present and short", key=key.value
        )
    try:
        require_clean({"evidence_ref": reference}, "canary_eligibility")
    except PayloadSanitationError as unsafe:
        raise _refusal(
            EVIDENCE_REFUSED,
            "an evidence reference holds no URL, credential or session material",
            key=key.value,
        ) from unsafe
    exact = {
        EvidenceKind.CATEGORY_METADATA: packet.binding.category_metadata_revision,
        EvidenceKind.LISTING_REVIEW_PACKET: packet.digest,
    }.get(kind)
    if exact is not None and reference != exact:
        raise _refusal(
            EVIDENCE_REFUSED,
            "this evidence kind names exactly the packet's own metadata revision or digest",
            key=key.value,
            kind=kind.value,
        )


# ------------------------------------------------------------------ the owner


class CandidateEvaluator(Protocol):
    """The preparation owner's mutation-stage candidate (``RegistrationPreparationService``)."""

    def stage_candidate(self, preparation_id: str) -> PreflightResult: ...


class CanaryEligibilityService:
    def __init__(
        self,
        *,
        store: LiveAuthorityStore,
        registrations: RegistrationStore,
        preparations: CandidateEvaluator,
    ) -> None:
        self._store = store
        self._registrations = registrations
        self._preparations = preparations

    # -------------------------------------------------------------- the review packet

    def review_packet(self, preparation_id: str) -> ReviewPacket:
        """The packet of this preparation's current revision, built now from owner truth."""
        preparation = self._registrations.preparation(preparation_id)
        if preparation is None:
            raise NotFoundError("CANARY_ELIGIBILITY_PREPARATION_NOT_FOUND", "no such preparation")
        revision_id = preparation.current.preparation_revision_id
        built = review_packet(
            # The candidate both stages bind, duplicate evidence included (5915900049 D4).
            self._preparations.stage_candidate(preparation_id),
            revision_id,
            unit_ref=asset_unit_ref(revision_id),
        )
        if built is None:
            raise _refusal(
                PACKET_UNAVAILABLE,
                "no eligibility packet can be built: the unit needs a chosen category whose"
                " current metadata revision is reviewed, leaf and registrable, sanitized"
                " publication text and a marketplace with a defined regulated scope",
            )
        return built

    # -------------------------------------------------------------- the protected action

    def record(
        self,
        *,
        preparation_id: str,
        expected_packet_digest: str,
        checks: Mapping[str, Any],
        actor: str,
        correlation_id: str,
    ) -> EligibilityRecord:
        """Append one eligibility record over the exact server-built packet.

        The packet is rebuilt here, immediately before the write. ``expected_packet_digest`` is
        the digest of the packet the operator reviewed: it is only an expectation, and a packet
        that moved since refuses. Every identity the record stores is the server's own.
        """
        fence = self._fence()
        packet = self.review_packet(preparation_id)
        if expected_packet_digest != packet.digest:
            raise RegistrationConflictError(
                PACKET_MOVED,
                "the reviewed packet is not the current one; review the current packet",
                details={"current_packet_digest": packet.digest},
            )
        canonical, verdict = validate_checks(checks, packet)
        with self._store.transaction() as unit:
            if unit.owner_writes() != fence:
                raise RegistrationConflictError(
                    TRUTH_MOVED, "an owner wrote while the packet was rebuilt; review it again"
                )
            row = unit.record_eligibility(
                binding=_scope(packet.binding),
                checks=canonical,
                verdict=verdict.value,
                actor=actor,
                correlation_id=correlation_id,
            )
        return _record(row)

    # -------------------------------------------------------------- reads

    def current(self, binding: EligibilityBinding) -> EligibilityRecord | None:
        """The highest-``seq`` record of exactly this binding's scope."""
        with self._store.reading() as unit:
            row = unit.current_eligibility(_scope(binding))
        return None if row is None else _record(row)

    def history(self, binding: EligibilityBinding) -> tuple[EligibilityRecord, ...]:
        with self._store.reading() as unit:
            return tuple(_record(row) for row in unit.eligibility_records(_scope(binding)))

    def proven(
        self, stage: MutationStage, unit_ref: str, binding: EligibilityBinding | None
    ) -> bool:
        """Whether ``CANARY_NON_REGULATED`` is proven for exactly this stage unit and lineage.

        Safe inside a mutation-start unit: it reads the record and the immutable lineage rows and
        evaluates nothing. Anything missing, unreadable or unequal is unproven.
        """
        if binding is None or binding.unit_ref != unit_ref:
            return False
        try:
            if not self._lineage_holds(stage, binding):
                return False
            record = self.current(binding)
        except SQLAlchemyError:
            return False
        return (
            record is not None
            and record.verdict is EligibilityVerdict.PROVEN_OUTSIDE
            and (
                record.taxonomy_revision,
                record.category_id,
                record.category_metadata_revision,
                record.review_packet_digest,
            )
            == (
                binding.taxonomy_revision,
                binding.category_id,
                binding.category_metadata_revision,
                binding.review_packet_digest,
            )
        )

    # -------------------------------------------------------------- helpers

    def _lineage_holds(self, stage: MutationStage, binding: EligibilityBinding) -> bool:
        preparation = self._registrations.preparation_of_revision(binding.preparation_revision_id)
        if preparation is None or (
            preparation.marketplace_key,
            preparation.marketplace_account_id,
        ) != (binding.marketplace_key, binding.marketplace_account_id):
            return False
        if stage is MutationStage.ASSET:
            return (
                binding.unit_ref == asset_unit_ref(binding.preparation_revision_id)
                and preparation.current.preparation_revision_id == binding.preparation_revision_id
            )
        prefix = create_unit_ref("")
        if not binding.unit_ref.startswith(prefix):
            return False
        intent = self._registrations.intent(binding.unit_ref[len(prefix) :])
        if intent is None:
            return False
        provenance = self._registrations.snapshot_preparation(intent.registration_snapshot_id)
        return (
            provenance is not None
            and provenance.preparation_revision_id == binding.preparation_revision_id
            and (intent.marketplace_key, intent.marketplace_account_id)
            == (binding.marketplace_key, binding.marketplace_account_id)
        )

    def _fence(self) -> int:
        with self._store.reading() as unit:
            return unit.owner_writes()


def _scope(binding: EligibilityBinding) -> dict[str, str]:
    return {
        "marketplace_key": binding.marketplace_key,
        "marketplace_account_id": binding.marketplace_account_id,
        "preparation_revision_id": binding.preparation_revision_id,
        "candidate_fingerprint": binding.candidate_fingerprint,
        "taxonomy_revision": binding.taxonomy_revision,
        "category_id": binding.category_id,
        "category_metadata_revision": binding.category_metadata_revision,
        "scope_version": binding.scope_version,
        "review_packet_digest": binding.review_packet_digest,
    }


def _record(row: Any) -> EligibilityRecord:
    return EligibilityRecord(
        eligibility_id=row.eligibility_id,
        marketplace_key=row.marketplace_key,
        marketplace_account_id=row.marketplace_account_id,
        preparation_revision_id=row.preparation_revision_id,
        candidate_fingerprint=row.candidate_fingerprint,
        taxonomy_revision=row.taxonomy_revision,
        category_id=row.category_id,
        category_metadata_revision=row.category_metadata_revision,
        scope_version=row.scope_version,
        seq=row.seq,
        review_packet_digest=row.review_packet_digest,
        checks=json.loads(row.checks_json),
        verdict=EligibilityVerdict(row.verdict),
        recorded_by=row.recorded_by,
        recorded_at=row.recorded_at,
    )
