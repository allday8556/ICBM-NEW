"""M5 PR-C: the derived registration preflight of one provider-listing unit (ADR-0014 §2–§5, §7,
§10, §13, §18–§21; Issue #89 kickoff 5742880432).

Pure: no database, no clock, no provider and no I/O. :mod:`app.register.preflight` gathers the
current truth of every owner into a :class:`ResolvedUnit`; :func:`evaluate` derives the result.

**Two stages** (ADR-0014 §3, B2)::

    CANDIDATE   every dependency except the provider asset identity, mutation-free
                → READY is the only status that permits a marketplace asset upload, and the
                  upload binds to ``candidate_fingerprint``
    FINAL       every dependency, the prepared provider assets included
                → READY is required before a RegistrationSnapshot is frozen

**Status.** Within one target the status is the highest of
``BLOCKED > DUPLICATE > STALE > REVIEW_REQUIRED > READY`` and every reason is returned. Nothing is
stored: no ``REGISTERABLE`` exists, and a result is only an evaluation.

**Fingerprint** (``registration-preflight-fingerprint/v1``): SHA-256 over the UTF-8 canonical
JSON (sorted keys, no insignificant whitespace) of the exact dependencies used. Maps are keyed,
sets are sorted, and Items follow their Draft ordinal, so a request's own ordering never changes
it. The candidate fingerprint names every non-asset dependency; the final fingerprint names the
candidate fingerprint and the prepared provider assets.

**M4 truth is consumed, never redone.** Base and per-target pricing readiness propagate with
their own status under ``M4_BASE.`` / ``M4_PRICING.``; the Draft's exact ``pricing_snapshot_id``
pin is compared with the price the M4 owner holds current for the target context, and a
difference is ``STALE``, never a silent re-read or a re-price.
"""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from app.collect.facts import ImageRole
from app.connect.accounts import AccountBinding
from app.connect.marketplace.capability import (
    AuthStatus,
    WorkflowScope,
    WorkflowState,
    WriteScopeStatus,
)
from app.products.image_model import ImageAssetKind, QaVerdict
from app.products.model import READINESS_PRECEDENCE, ReadinessStatus, Reason, precedence_status
from app.products.pricing import PriceBasis, PriceGuard
from app.register import sanitize
from app.register.model import ListingShape, canonical_json, valid_listing_identity
from app.register.policy import (
    SATISFYING,
    STRONG_KEYS,
    CategoryMetadata,
    DuplicateKeyKind,
    FieldRule,
    Provenance,
    TargetPolicy,
)

PREFLIGHT_RULE_VERSION: Final = "registration-preflight/v1"
FINGERPRINT_VERSION: Final = "registration-preflight-fingerprint/v1"
LISTING_IDENTITY_VERSION: Final = "listing-identity/v1"
RESALE_ADVISORY_LABEL: Final = "공급처 판매가 정책 참고"

_B, _D, _S, _R = (
    ReadinessStatus.BLOCKED,
    ReadinessStatus.DUPLICATE,
    ReadinessStatus.STALE,
    ReadinessStatus.REVIEW_REQUIRED,
)

# ---------------------------------------------------------------- reason inventory
# Our own codes, never page or provider content. ``M4_BASE.`` / ``M4_PRICING.`` prefix an M4 reason
# propagated with its own status.

ACCOUNT_UNKNOWN: Final = "ACCOUNT_UNKNOWN"
ACCOUNT_NOT_BOUND: Final = "ACCOUNT_NOT_BOUND"
ACCOUNT_BINDING_MISMATCH: Final = "ACCOUNT_BINDING_MISMATCH"
TARGET_SCOPE_MISMATCH: Final = "TARGET_SCOPE_MISMATCH"
CONNECT_CAPABILITY_UNAVAILABLE: Final = "CONNECT_CAPABILITY_UNAVAILABLE"
CONNECT_AUTH_NOT_READY: Final = "CONNECT_AUTH_NOT_READY"
CONNECT_AUTH_MISMATCH: Final = "CONNECT_AUTH_MISMATCH"
CONNECT_NOT_BOUND: Final = "CONNECT_NOT_BOUND"
CONNECT_WRITE_SCOPE_MISSING: Final = "CONNECT_WRITE_SCOPE_MISSING"
CONNECT_WORKFLOW_PAUSED: Final = "CONNECT_WORKFLOW_PAUSED"
CONNECT_WORKFLOW_REVIEW: Final = "CONNECT_WORKFLOW_REVIEW_REQUIRED"
DRAFT_REVISION_STALE: Final = "DRAFT_REVISION_STALE"
UNIT_EMPTY: Final = "UNIT_EMPTY"
UNIT_ITEM_NOT_OPEN: Final = "UNIT_ITEM_NOT_OPEN"
UNIT_ITEM_DUPLICATED: Final = "UNIT_ITEM_DUPLICATED"
UNIT_SINGLE_NOT_COVERING: Final = "UNIT_SINGLE_NOT_COVERING"
UNIT_SEPARATE_NOT_ONE_ITEM: Final = "UNIT_SEPARATE_NOT_ONE_ITEM"
LISTING_IDENTITY_INVALID: Final = "LISTING_IDENTITY_INVALID"
TARGET_PRICING_CONTEXT_INVALID: Final = "TARGET_PRICING_CONTEXT_INVALID"
PRICE_PIN_MISSING: Final = "DRAFT_PRICE_PIN_MISSING"
PRICE_PIN_CONTEXT_INVALID: Final = "DRAFT_PRICE_PIN_CONTEXT_INVALID"
PRICE_PIN_CONTEXT_STALE: Final = "DRAFT_PRICE_PIN_CONTEXT_STALE"
PRICE_PIN_SUPERSEDED: Final = "DRAFT_PRICE_PIN_SUPERSEDED"
CATEGORY_NOT_SELECTED: Final = "CATEGORY_NOT_SELECTED"
CATEGORY_NOT_CONFIRMED: Final = "CATEGORY_NOT_CONFIRMED"
CATEGORY_TAXONOMY_STALE: Final = "CATEGORY_TAXONOMY_STALE"
CATEGORY_METADATA_MISSING: Final = "CATEGORY_METADATA_MISSING"
CATEGORY_METADATA_UNREVIEWED: Final = "CATEGORY_METADATA_UNREVIEWED"
CATEGORY_NOT_LEAF: Final = "CATEGORY_NOT_LEAF"
CATEGORY_RESTRICTED: Final = "CATEGORY_RESTRICTED"
LISTING_NAME_MISSING: Final = "LISTING_NAME_MISSING"
LISTING_NAME_TOO_LONG: Final = "LISTING_NAME_TOO_LONG"
ATTRIBUTE_REQUIRED_MISSING: Final = "ATTRIBUTE_REQUIRED_MISSING"
NOTICE_REQUIRED_MISSING: Final = "NOTICE_REQUIRED_MISSING"
NOTICE_POLICY_MISSING: Final = "NOTICE_POLICY_MISSING"
FIELD_UNDECLARED: Final = "FIELD_UNDECLARED"
FIELD_VALUE_EMPTY: Final = "FIELD_VALUE_EMPTY"
FIELD_VALUE_TOO_LONG: Final = "FIELD_VALUE_TOO_LONG"
FIELD_AI_SUGGESTION_UNCONFIRMED: Final = "FIELD_AI_SUGGESTION_UNCONFIRMED"
FIELD_DETAIL_REFERENCE_NOT_PERMITTED: Final = "FIELD_DETAIL_REFERENCE_NOT_PERMITTED"
OPTIONS_NOT_SUPPORTED: Final = "OPTIONS_NOT_SUPPORTED"
OPTION_COUNT_EXCEEDED: Final = "OPTION_COUNT_EXCEEDED"
OPTION_VALUE_MISSING: Final = "OPTION_VALUE_MISSING"
OPTION_VALUES_UNEXPECTED: Final = "OPTION_VALUES_UNEXPECTED"
OPTION_DIMENSIONS_INCONSISTENT: Final = "OPTION_DIMENSIONS_INCONSISTENT"
OPTION_DIMENSIONS_EXCEEDED: Final = "OPTION_DIMENSIONS_EXCEEDED"
OPTION_VALUES_NOT_DISTINCT: Final = "OPTION_VALUES_NOT_DISTINCT"
POLICY_TEMPLATE_MISSING: Final = "POLICY_TEMPLATE_MISSING"
DETAIL_COMPOSITION_MISSING: Final = "DETAIL_COMPOSITION_MISSING"
DETAIL_BODY_EMPTY: Final = "DETAIL_BODY_EMPTY"
# No owner exists yet for the category-mapping and detail-composition authoring revisions (G1-A
# holds both as null; architect decision 5800619183). Authoring and the candidate still run; the
# unit is never READY, so nothing can be frozen until real owners supply both revisions.
AUTHORING_REVISIONS_UNOWNED: Final = "AUTHORING_REVISIONS_UNOWNED"
PUBLICATION_ASSETS_MISSING: Final = "PUBLICATION_ASSETS_MISSING"
PUBLICATION_ASSET_COUNT_EXCEEDED: Final = "PUBLICATION_ASSET_COUNT_EXCEEDED"
PUBLICATION_REPRESENTATIVE_MISSING: Final = "PUBLICATION_REPRESENTATIVE_MISSING"
PUBLICATION_ASSET_QA_NOT_PASSED: Final = "PUBLICATION_ASSET_QA_NOT_PASSED"
PROVIDER_ASSET_IDENTITY_MISSING: Final = "PROVIDER_ASSET_IDENTITY_MISSING"
PREPARED_ASSET_CANDIDATE_MISMATCH: Final = "PREPARED_ASSET_CANDIDATE_MISMATCH"
PREPARED_ASSET_PROFILE_MISMATCH: Final = "PREPARED_ASSET_PROFILE_MISMATCH"
PREPARED_ASSET_NOT_SELECTED: Final = "PREPARED_ASSET_NOT_SELECTED"
PREPARED_ASSET_DUPLICATED: Final = "PREPARED_ASSET_DUPLICATED"
PREPARED_ASSET_REF_UNSAFE: Final = "PREPARED_ASSET_REF_UNSAFE"
PREPARED_ASSET_UNEXPECTED: Final = "PREPARED_ASSET_UNEXPECTED"
UNRESOLVED_CREATE_CONFLICT: Final = "UNRESOLVED_CREATE_CONFLICT"
LIVE_REGISTRATION_EXISTS: Final = "LIVE_REGISTRATION_EXISTS"
PROVIDER_DUPLICATE_FOUND: Final = "PROVIDER_DUPLICATE_FOUND"
PROVIDER_DUPLICATE_WEAK_SIGNAL: Final = "PROVIDER_DUPLICATE_WEAK_SIGNAL"
DUPLICATE_EVIDENCE_MISSING: Final = "DUPLICATE_EVIDENCE_MISSING"
DUPLICATE_EVIDENCE_INCONCLUSIVE: Final = "DUPLICATE_EVIDENCE_INCONCLUSIVE"
DUPLICATE_EVIDENCE_INCOMPLETE: Final = "DUPLICATE_EVIDENCE_INCOMPLETE"
DUPLICATE_EVIDENCE_SCOPE_MISMATCH: Final = "DUPLICATE_EVIDENCE_SCOPE_MISMATCH"
DUPLICATE_EVIDENCE_UNSAFE: Final = "DUPLICATE_EVIDENCE_UNSAFE"
M4_BASE_PREFIX: Final = "M4_BASE."
M4_PRICING_PREFIX: Final = "M4_PRICING."

REASON_CODES: Final = frozenset(
    {
        ACCOUNT_UNKNOWN,
        ACCOUNT_NOT_BOUND,
        ACCOUNT_BINDING_MISMATCH,
        TARGET_SCOPE_MISMATCH,
        CONNECT_CAPABILITY_UNAVAILABLE,
        CONNECT_AUTH_NOT_READY,
        CONNECT_AUTH_MISMATCH,
        CONNECT_NOT_BOUND,
        CONNECT_WRITE_SCOPE_MISSING,
        CONNECT_WORKFLOW_PAUSED,
        CONNECT_WORKFLOW_REVIEW,
        DRAFT_REVISION_STALE,
        UNIT_EMPTY,
        UNIT_ITEM_NOT_OPEN,
        UNIT_ITEM_DUPLICATED,
        UNIT_SINGLE_NOT_COVERING,
        UNIT_SEPARATE_NOT_ONE_ITEM,
        LISTING_IDENTITY_INVALID,
        TARGET_PRICING_CONTEXT_INVALID,
        PRICE_PIN_MISSING,
        PRICE_PIN_CONTEXT_INVALID,
        PRICE_PIN_CONTEXT_STALE,
        PRICE_PIN_SUPERSEDED,
        CATEGORY_NOT_SELECTED,
        CATEGORY_NOT_CONFIRMED,
        CATEGORY_TAXONOMY_STALE,
        CATEGORY_METADATA_MISSING,
        CATEGORY_METADATA_UNREVIEWED,
        CATEGORY_NOT_LEAF,
        CATEGORY_RESTRICTED,
        LISTING_NAME_MISSING,
        LISTING_NAME_TOO_LONG,
        ATTRIBUTE_REQUIRED_MISSING,
        NOTICE_REQUIRED_MISSING,
        NOTICE_POLICY_MISSING,
        FIELD_UNDECLARED,
        FIELD_VALUE_EMPTY,
        FIELD_VALUE_TOO_LONG,
        FIELD_AI_SUGGESTION_UNCONFIRMED,
        FIELD_DETAIL_REFERENCE_NOT_PERMITTED,
        OPTIONS_NOT_SUPPORTED,
        OPTION_COUNT_EXCEEDED,
        OPTION_VALUE_MISSING,
        OPTION_VALUES_UNEXPECTED,
        OPTION_DIMENSIONS_INCONSISTENT,
        OPTION_DIMENSIONS_EXCEEDED,
        OPTION_VALUES_NOT_DISTINCT,
        POLICY_TEMPLATE_MISSING,
        DETAIL_COMPOSITION_MISSING,
        DETAIL_BODY_EMPTY,
        AUTHORING_REVISIONS_UNOWNED,
        PUBLICATION_ASSETS_MISSING,
        PUBLICATION_ASSET_COUNT_EXCEEDED,
        PUBLICATION_REPRESENTATIVE_MISSING,
        PUBLICATION_ASSET_QA_NOT_PASSED,
        PROVIDER_ASSET_IDENTITY_MISSING,
        PREPARED_ASSET_CANDIDATE_MISMATCH,
        PREPARED_ASSET_PROFILE_MISMATCH,
        PREPARED_ASSET_NOT_SELECTED,
        PREPARED_ASSET_DUPLICATED,
        PREPARED_ASSET_REF_UNSAFE,
        PREPARED_ASSET_UNEXPECTED,
        UNRESOLVED_CREATE_CONFLICT,
        LIVE_REGISTRATION_EXISTS,
        PROVIDER_DUPLICATE_FOUND,
        PROVIDER_DUPLICATE_WEAK_SIGNAL,
        DUPLICATE_EVIDENCE_MISSING,
        DUPLICATE_EVIDENCE_INCONCLUSIVE,
        DUPLICATE_EVIDENCE_INCOMPLETE,
        DUPLICATE_EVIDENCE_SCOPE_MISMATCH,
        DUPLICATE_EVIDENCE_UNSAFE,
        sanitize.SECRET_MATERIAL,
        sanitize.EXTERNAL_URL,
    }
)


class PreflightStage(StrEnum):
    CANDIDATE = "CANDIDATE"
    FINAL = "FINAL"


# ---------------------------------------------------------------- the request


@dataclass(frozen=True)
class FieldValue:
    """One outbound value and its provenance. ``detail_page_reference`` states the value is the
    "상세페이지 참조" representation, allowed only where the field's rule permits it."""

    value: str = ""
    provenance: Provenance = Provenance.OPERATOR_CONFIRMED
    detail_page_reference: bool = False

    def canonical(self) -> dict[str, object]:
        if self.detail_page_reference:
            return {"detail_page_reference": True, "provenance": self.provenance.value}
        return {"value": self.value, "provenance": self.provenance.value}


@dataclass(frozen=True)
class ListingValues:
    """The listing's outbound business values, as the operator or a source fact supplied them.
    Nothing here is fabricated: an absent optional value stays absent."""

    name: FieldValue | None = None
    tags: frozenset[str] = frozenset()
    attributes: Mapping[str, FieldValue] = field(default_factory=dict)
    notices: Mapping[str, FieldValue] = field(default_factory=dict)
    # Item id → option dimension → option value. Display values only: never an identity.
    options: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


class CategoryConfirmation(StrEnum):
    OPERATOR_CONFIRMED = "OPERATOR_CONFIRMED"
    AI_SUGGESTION = "AI_SUGGESTION"


@dataclass(frozen=True)
class CategorySelection:
    """``mapping_revision`` is the target's category-mapping revision, exactly as its owner holds
    it: ``None`` while no owner exists (decision 5800619183). It is never defaulted or invented."""

    category_id: str
    mapping_revision: str | None
    taxonomy_revision: str
    confirmation: CategoryConfirmation


@dataclass(frozen=True)
class DetailComposition:
    """``product body → detail composition → marketplace payload`` (Issue #61, ADR-0014 §19).
    The first vertical composes the body only; later guidance adds sections here, never in the
    payload builder. ``composition_revision`` is ``None`` while its owner does not exist: a body
    may still be authored, and the unit reports ``AUTHORING_REVISIONS_UNOWNED``."""

    composition_revision: str | None
    body: str
    sections: tuple[str, ...] = ("BODY",)


def compose_body_only(composition_revision: str | None, body: str) -> DetailComposition:
    return DetailComposition(composition_revision=composition_revision, body=body)


class DuplicateVerdict(StrEnum):
    NO_MATCH = "NO_MATCH"
    MATCH = "MATCH"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class DuplicateMatch:
    key_kind: DuplicateKeyKind
    provider_listing_ref: str


@dataclass(frozen=True)
class DuplicateEvidence:
    """Provider duplicate-lookup evidence for one unit (ADR-0014 §13). PR-C performs no lookup:
    this is the input contract a PR-D adapter fills, with the SHA-256 of its sanitized evidence.

    Sanitation is enforced here, not assumed (§15, B4; PR #92 review 5256446628): evidence with any
    field that could carry secret, signed or tokenized material is never READY, whatever override
    covers the unit, and a raw provider listing reference never enters a fingerprint.
    """

    marketplace_key: str
    marketplace_account_id: str
    listing_identity: str
    lookup_contract_version: str
    evidence_digest: str
    verdict: DuplicateVerdict
    keys_checked: frozenset[DuplicateKeyKind]
    matches: tuple[DuplicateMatch, ...] = ()

    def unsafe_fields(self) -> tuple[str, ...]:
        """Every field that may not reach a durable digest: an evidence digest that is not a
        lower-case SHA-256, a contract version that is not a plain label, or a provider listing
        reference that is not an opaque or plain https reference (no query, fragment, userinfo,
        bearer or token material). The scope fields are only ever compared, never fingerprinted."""
        unsafe = []
        if not sanitize.hex_digest(self.evidence_digest):
            unsafe.append("evidence_digest")
        if not sanitize.safe_label(self.lookup_contract_version):
            unsafe.append("lookup_contract_version")
        if any(not sanitize.safe_provider_reference(m.provider_listing_ref) for m in self.matches):
            unsafe.append("matches")
        return tuple(unsafe)

    def canonical(self, unit_scope: tuple[str, str, str]) -> dict[str, object]:
        """The typed, sanitized identity of the evidence for the fingerprint: its digest and
        contract version once they pass sanitation, the verdict, the keys checked, the kinds of the
        matches and whether its scope is the unit's. Never a raw provider reference, and never a
        field that failed sanitation."""
        unsafe = bool(self.unsafe_fields())
        return {
            "unsafe": unsafe,
            "lookup_contract_version": None if unsafe else self.lookup_contract_version,
            "evidence_digest": None if unsafe else self.evidence_digest,
            "scope_matches": (
                self.marketplace_key,
                self.marketplace_account_id,
                self.listing_identity,
            )
            == unit_scope,
            "verdict": self.verdict.value,
            "keys_checked": sorted(k.value for k in self.keys_checked),
            "match_key_kinds": sorted(m.key_kind.value for m in self.matches),
        }


@dataclass(frozen=True)
class PreparedAsset:
    """A provider asset PR-D prepared for one exact local artifact, bound to the candidate
    fingerprint it was uploaded under. An ambiguous upload is never represented as one."""

    asset_kind: ImageAssetKind
    sha256: str
    derivation_id: str | None
    asset_profile: str
    candidate_fingerprint: str
    provider_asset_ref: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.asset_kind.value, self.sha256, self.derivation_id or "")

    def unsafe(self) -> bool:
        """Whether the provider reference may not enter a durable fingerprint or payload: it is
        ``PREPARED_ASSET_REF_UNSAFE`` and only its absence is recorded (§15, B4)."""
        return not sanitize.safe_provider_reference(self.provider_asset_ref)

    def canonical(self) -> dict[str, object]:
        unsafe = self.unsafe()
        return {
            "asset_kind": self.asset_kind.value,
            "sha256": self.sha256,
            "derivation_id": self.derivation_id,
            "asset_profile": self.asset_profile,
            "candidate_fingerprint": self.candidate_fingerprint,
            "unsafe": unsafe,
            "provider_asset_ref": None if unsafe else self.provider_asset_ref,
        }


@dataclass(frozen=True)
class ResaleAdvisory:
    """``공급처 판매가 정책 참고`` (Issue #80 ruling 5738886070, ADR-0014 §20): display data only.
    It is never a price, never a readiness input and never part of the fingerprint."""

    text: str
    provenance_ref: str


@dataclass(frozen=True)
class UnitRequest:
    """Which provider-listing unit of a Draft to prepare. ``item_ids`` is ``None`` for a
    ``SINGLE_LISTING_WITH_OPTIONS`` unit (every open Item); the one Item of a separate listing, or
    the chosen subset of ``SELECTED_OFFERS``, is named. Its order never matters."""

    draft_id: str
    expected_draft_revision: int
    item_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PreflightRequest:
    unit: UnitRequest
    category: CategorySelection | None
    listing: ListingValues
    detail: DetailComposition | None
    duplicate_evidence: DuplicateEvidence | None = None
    advisories: tuple[ResaleAdvisory, ...] = ()


# ---------------------------------------------------------------- resolved current truth


@dataclass(frozen=True)
class AccountState:
    """The canonical account's binding (PR-B) and the CONNECT capability it is read with."""

    binding: AccountBinding
    capability_available: bool
    auth: AuthStatus | None = None
    write_scope: WriteScopeStatus | None = None
    overlays: tuple[tuple[WorkflowScope, WorkflowState], ...] = ()

    def canonical(self) -> dict[str, object]:
        return {
            "binding": self.binding.value,
            "capability_available": self.capability_available,
            "auth": None if self.auth is None else self.auth.value,
            "write_scope": None if self.write_scope is None else self.write_scope.value,
            "overlays": sorted([s.value, w.value] for s, w in self.overlays),
        }


@dataclass(frozen=True)
class ReadinessInput:
    """One M4 readiness evaluation, as the M4 owner returned it."""

    status: ReadinessStatus
    reasons: tuple[Reason, ...]
    rule_version: str
    dependency_fingerprint: str

    def canonical(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "rule_version": self.rule_version,
            "dependency_fingerprint": self.dependency_fingerprint,
        }


@dataclass(frozen=True)
class PinnedPrice:
    """The Draft Item's pinned M4 PricingSnapshot."""

    pricing_snapshot_id: str
    item_id: str
    marketplace_key: str
    account_id: str | None
    pricing_context_fingerprint: str
    dependency_fingerprint: str
    membership_revision_id: str
    source_binding_id: str
    source_product_facts_revision_id: str
    final_sale_price_krw: int
    price_basis: PriceBasis
    price_guard: PriceGuard

    def canonical(self) -> dict[str, object]:
        return {
            "pricing_snapshot_id": self.pricing_snapshot_id,
            "pricing_context_fingerprint": self.pricing_context_fingerprint,
            "dependency_fingerprint": self.dependency_fingerprint,
            "membership_revision_id": self.membership_revision_id,
            "source_binding_id": self.source_binding_id,
            "source_product_facts_revision_id": self.source_product_facts_revision_id,
            "final_sale_price_krw": self.final_sale_price_krw,
            "price_basis": self.price_basis.value,
            "price_guard": self.price_guard.value,
        }


@dataclass(frozen=True)
class BindingCopy:
    """The current procurement of the pinned price, copied for the Snapshot's
    ``source_snapshot`` (ADR-0014 §6): identifiers only, never a URL."""

    binding_id: str
    binding_kind: str
    group_member_id: str
    source_product_uid: str | None
    provenance_revision_id: str
    fulfillment_quantity: int
    quantity_offer_id: str | None

    def canonical(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "binding_kind": self.binding_kind,
            "group_member_id": self.group_member_id,
            "source_product_uid": self.source_product_uid,
            "provenance_revision_id": self.provenance_revision_id,
            "fulfillment_quantity": self.fulfillment_quantity,
            "quantity_offer_id": self.quantity_offer_id,
        }


@dataclass(frozen=True)
class PublicationImage:
    """One currently selected M4 image of an Item, with its exact-binary QA."""

    role: ImageRole
    position: int
    asset_kind: ImageAssetKind
    sha256: str
    derivation_id: str | None
    qa_result_id: str | None
    qa_verdict: QaVerdict | None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.asset_kind.value, self.sha256, self.derivation_id or "")

    def canonical(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "position": self.position,
            "asset_kind": self.asset_kind.value,
            "sha256": self.sha256,
            "derivation_id": self.derivation_id,
            "qa_result_id": self.qa_result_id,
            "qa_verdict": None if self.qa_verdict is None else self.qa_verdict.value,
        }


@dataclass(frozen=True)
class OverrideCoverage:
    """An active DuplicateOverride of this account for the Item's group (PR-B owner)."""

    override_id: str
    product_group_id: str
    listing_composition_id: str | None


@dataclass(frozen=True)
class ResolvedItem:
    item_id: str
    ordinal: int
    product_group_id: str
    composition_id: str
    composition_signature: str
    pin: PinnedPrice | None
    current_price_id: str | None
    base: ReadinessInput
    pricing: ReadinessInput
    binding: BindingCopy | None
    selection_revision_id: str | None
    images: tuple[PublicationImage, ...]
    overrides: tuple[OverrideCoverage, ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        return (self.product_group_id, self.composition_signature)

    def covered(self) -> bool:
        """Whether an active override of this account permits a duplicate of this Item."""
        return any(o.listing_composition_id in (None, self.composition_id) for o in self.overrides)

    def canonical(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "ordinal": self.ordinal,
            "product_group_id": self.product_group_id,
            "composition_id": self.composition_id,
            "composition_signature": self.composition_signature,
            "pin": None if self.pin is None else self.pin.canonical(),
            "current_price_id": self.current_price_id,
            "base_readiness": self.base.canonical(),
            "pricing_readiness": self.pricing.canonical(),
            "binding": None if self.binding is None else self.binding.canonical(),
            "selection_revision_id": self.selection_revision_id,
            "images": [image.canonical() for image in self.images],
            "overrides": sorted(o.override_id for o in self.overrides),
        }


@dataclass(frozen=True)
class ConflictState:
    """A CREATE Intent that is SENT or UNKNOWN in the unit's R2 conflict scope."""

    intent_id: str
    state: str


@dataclass(frozen=True)
class LiveRegistration:
    """A verified, ACTIVE registration of this account whose Items overlap the unit's groups."""

    registration_id: str


@dataclass(frozen=True)
class DraftItemState:
    item_id: str
    ordinal: int
    pricing_snapshot_id: str


@dataclass(frozen=True)
class ResolvedUnit:
    """Everything current the evaluation reads, gathered by the preflight service."""

    marketplace_key: str
    marketplace_account_id: str
    draft_id: str
    draft_revision: int
    listing_shape: ListingShape
    open_items: tuple[DraftItemState, ...]
    unit_item_ids: tuple[str, ...]
    items: tuple[ResolvedItem, ...]
    account: AccountState
    listing_identity: str
    identity_generation: int
    conflicts: tuple[ConflictState, ...]
    live_registrations: tuple[LiveRegistration, ...]
    metadata: CategoryMetadata | None
    # The account's current Settings/platform registration policy (a server-side source).
    target: TargetPolicy


# ---------------------------------------------------------------- the result


@dataclass(frozen=True)
class PreflightResult:
    """One derived evaluation. It is never stored; it is recomputed from current truth."""

    stage: PreflightStage
    status: ReadinessStatus
    reasons: tuple[Reason, ...]
    rule_version: str
    fingerprint_version: str
    candidate_fingerprint: str
    dependency_fingerprint: str
    dependencies: Mapping[str, Any]
    upload_permitted: bool
    request: PreflightRequest
    resolved: ResolvedUnit
    prepared_assets: tuple[PreparedAsset, ...] = ()
    advisories: tuple[ResaleAdvisory, ...] = ()

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(reason.code for reason in self.reasons)


# ---------------------------------------------------------------- pure helpers


def fingerprint(dependencies: Mapping[str, Any]) -> str:
    """``registration-preflight-fingerprint/v1``: SHA-256 of the UTF-8 canonical JSON."""
    return hashlib.sha256(canonical_json(dict(dependencies)).encode("utf-8")).hexdigest()


def resolve_unit(
    shape: ListingShape, open_ids: Sequence[str], requested: Sequence[str] | None
) -> tuple[tuple[str, ...], tuple[Reason, ...]]:
    """The unit's Items in Draft order, and every reason the request does not name a valid unit.

    ``SINGLE_LISTING_WITH_OPTIONS`` is every open Item; ``SEPARATE_LISTINGS`` exactly one;
    ``SELECTED_OFFERS`` a non-empty chosen subset. Request order never matters.
    """
    reasons: list[Reason] = []
    wanted = list(open_ids) if requested is None else list(requested)
    if len(set(wanted)) != len(wanted):
        reasons.append(Reason(UNIT_ITEM_DUPLICATED, _B, "unit"))
    missing = sorted(set(wanted) - set(open_ids))
    reasons.extend(Reason(UNIT_ITEM_NOT_OPEN, _B, f"item:{item}") for item in missing)
    chosen = tuple(item for item in open_ids if item in set(wanted))
    if not chosen:
        reasons.append(Reason(UNIT_EMPTY, _B, "unit"))
    if shape is ListingShape.SINGLE_LISTING_WITH_OPTIONS and set(wanted) != set(open_ids):
        reasons.append(Reason(UNIT_SINGLE_NOT_COVERING, _B, "unit"))
    if shape is ListingShape.SEPARATE_LISTINGS and len(set(wanted)) != 1:
        reasons.append(Reason(UNIT_SEPARATE_NOT_ONE_ITEM, _B, "unit"))
    return chosen, tuple(reasons)


def listing_identity(
    marketplace_key: str,
    marketplace_account_id: str,
    draft_id: str,
    unit_key: Iterable[tuple[str, str]],
    generation: int,
) -> str:
    """``listing-identity/v1``: the deterministic seller-side identity of one provider-listing unit
    (ADR-0014 §7), from stable local identity only: never a name, a price or an order position.

    ``generation`` counts the earlier Snapshots of this Draft and unit that an Intent already
    names, so a unit that may already have reached the marketplace never shares an identity with
    a later one; a unit re-prepared before any Intent keeps its identity.
    """
    text = canonical_json(
        {
            "version": LISTING_IDENTITY_VERSION,
            "marketplace_key": marketplace_key,
            "marketplace_account_id": marketplace_account_id,
            "draft_id": draft_id,
            "unit": sorted([group, signature] for group, signature in unit_key),
            "generation": generation,
        }
    )
    return "icbm-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _ordered(reasons: Iterable[Reason]) -> tuple[Reason, ...]:
    rank = {status: index for index, status in enumerate(READINESS_PRECEDENCE)}
    unique = {(reason.code, reason.subject): reason for reason in reasons}
    return tuple(sorted(unique.values(), key=lambda r: (rank[r.status], r.code, r.subject or "")))


def _subject(item_id: str, detail: str | None) -> str:
    return f"item:{item_id}" if detail is None else f"item:{item_id}:{detail}"


# ---------------------------------------------------------------- the rules


def _account_reasons(unit: ResolvedUnit, target: TargetPolicy) -> list[Reason]:
    reasons: list[Reason] = []
    if (target.marketplace_key, target.marketplace_account_id) != (
        unit.marketplace_key,
        unit.marketplace_account_id,
    ):
        reasons.append(Reason(TARGET_SCOPE_MISMATCH, _B, "target"))
    binding = unit.account.binding
    if binding is AccountBinding.UNKNOWN_ACCOUNT:
        reasons.append(Reason(ACCOUNT_UNKNOWN, _B, "account"))
    elif binding is AccountBinding.NOT_BOUND:
        reasons.append(Reason(ACCOUNT_NOT_BOUND, _B, "account"))
    elif binding is AccountBinding.MISMATCHED:
        # PR-C never classifies same or different account itself: review (kickoff §Account).
        reasons.append(Reason(ACCOUNT_BINDING_MISMATCH, _R, "account"))
    account = unit.account
    if not account.capability_available:
        reasons.append(Reason(CONNECT_CAPABILITY_UNAVAILABLE, _B, "capability"))
        return reasons
    if account.auth is AuthStatus.NOT_BOUND:
        reasons.append(Reason(CONNECT_NOT_BOUND, _B, "auth"))
    elif account.auth is AuthStatus.AUTH_MISMATCH:
        reasons.append(Reason(CONNECT_AUTH_MISMATCH, _R, "auth"))
    elif account.auth is not AuthStatus.READY:
        reasons.append(Reason(CONNECT_AUTH_NOT_READY, _R, "auth"))
    if account.write_scope is WriteScopeStatus.MISSING:
        reasons.append(Reason(CONNECT_WRITE_SCOPE_MISSING, _B, "write_scope"))
    for scope, state in account.overlays:
        if scope not in (WorkflowScope.AUTHENTICATION, WorkflowScope.PRODUCT_REGISTRATION):
            continue
        if state is WorkflowState.PAUSED:
            reasons.append(Reason(CONNECT_WORKFLOW_PAUSED, _B, scope.value))
        else:
            reasons.append(Reason(CONNECT_WORKFLOW_REVIEW, _R, scope.value))
    return reasons


def _unit_reasons(request: PreflightRequest, unit: ResolvedUnit) -> list[Reason]:
    reasons: list[Reason] = []
    if request.unit.expected_draft_revision != unit.draft_revision:
        reasons.append(Reason(DRAFT_REVISION_STALE, _S, "draft"))
    _chosen, problems = resolve_unit(
        unit.listing_shape, [i.item_id for i in unit.open_items], request.unit.item_ids
    )
    reasons.extend(problems)
    if not valid_listing_identity(unit.listing_identity):
        reasons.append(Reason(LISTING_IDENTITY_INVALID, _B, "listing_identity"))
    return reasons


def _pricing_reasons(target: TargetPolicy, unit: ResolvedUnit) -> list[Reason]:
    reasons: list[Reason] = []
    context = target.pricing_context
    if context.marketplace_key != unit.marketplace_key or context.account_id not in (
        None,
        unit.marketplace_account_id,
    ):
        reasons.append(Reason(TARGET_PRICING_CONTEXT_INVALID, _B, "pricing_context"))
    pins = {state.item_id: state.pricing_snapshot_id for state in unit.open_items}
    for item in unit.items:
        pin = item.pin
        if pin is None or pins.get(item.item_id) != pin.pricing_snapshot_id:
            reasons.append(Reason(PRICE_PIN_MISSING, _B, _subject(item.item_id, "price")))
            continue
        if (
            pin.item_id != item.item_id
            or pin.marketplace_key != unit.marketplace_key
            or pin.account_id not in (None, unit.marketplace_account_id)
        ):
            reasons.append(Reason(PRICE_PIN_CONTEXT_INVALID, _B, _subject(item.item_id, "price")))
        elif pin.pricing_context_fingerprint != context.fingerprint:
            reasons.append(Reason(PRICE_PIN_CONTEXT_STALE, _S, _subject(item.item_id, "price")))
        elif item.current_price_id != pin.pricing_snapshot_id:
            # The M4 owner holds another price current: the Draft is repriced, never silently.
            reasons.append(Reason(PRICE_PIN_SUPERSEDED, _S, _subject(item.item_id, "price")))
    return reasons


def _m4_reasons(unit: ResolvedUnit) -> list[Reason]:
    """M4 base and target pricing readiness, each reason with its own status (kickoff §6)."""
    reasons: list[Reason] = []
    for item in unit.items:
        for prefix, readiness in ((M4_BASE_PREFIX, item.base), (M4_PRICING_PREFIX, item.pricing)):
            reasons.extend(
                Reason(f"{prefix}{r.code}", r.status, _subject(item.item_id, r.subject))
                for r in readiness.reasons
            )
            if readiness.status is not ReadinessStatus.READY and not readiness.reasons:
                reasons.append(
                    Reason(f"{prefix}{readiness.status.value}", readiness.status, item.item_id)
                )
    return reasons


def _category_reasons(request: PreflightRequest, unit: ResolvedUnit) -> list[Reason]:
    selection = request.category
    if selection is None:
        return [Reason(CATEGORY_NOT_SELECTED, _R, "category")]
    reasons: list[Reason] = []
    if selection.confirmation is not CategoryConfirmation.OPERATOR_CONFIRMED:
        # An AI ranking alone is never a category confirmation (ADR-0014 §4, §18).
        reasons.append(Reason(CATEGORY_NOT_CONFIRMED, _R, "category"))
    if selection.taxonomy_revision != unit.target.taxonomy_revision:
        reasons.append(Reason(CATEGORY_TAXONOMY_STALE, _S, "category"))
    metadata = unit.metadata
    if metadata is None:
        reasons.append(Reason(CATEGORY_METADATA_MISSING, _R, "category"))
        return reasons
    if not metadata.reviewed:
        reasons.append(Reason(CATEGORY_METADATA_UNREVIEWED, _R, "category"))
    if not metadata.leaf:
        reasons.append(Reason(CATEGORY_NOT_LEAF, _B, "category"))
    if not metadata.registrable:
        reasons.append(Reason(CATEGORY_RESTRICTED, _B, "category"))
    return reasons


def _value_reasons(subject: str, rule: FieldRule | None, value: FieldValue) -> list[Reason]:
    reasons: list[Reason] = []
    if value.provenance not in SATISFYING:
        reasons.append(Reason(FIELD_AI_SUGGESTION_UNCONFIRMED, _R, subject))
    if value.detail_page_reference:
        if rule is None or not rule.detail_page_reference_allowed:
            status = rule.missing_status if rule is not None and rule.required else _R
            reasons.append(Reason(FIELD_DETAIL_REFERENCE_NOT_PERMITTED, status, subject))
        return reasons
    if not value.value.strip():
        reasons.append(Reason(FIELD_VALUE_EMPTY, _R, subject))
    elif rule is not None and rule.max_length is not None and len(value.value) > rule.max_length:
        reasons.append(Reason(FIELD_VALUE_TOO_LONG, _R, subject))
    return reasons


def _declared_reasons(
    kind: str, missing_code: str, rules: Sequence[FieldRule], values: Mapping[str, FieldValue]
) -> list[Reason]:
    """Required fields come from the versioned metadata only; an absent optional field stays
    absent, and a value for a field the metadata does not declare is never sent silently."""
    declared = {rule.key: rule for rule in rules}
    reasons = [
        Reason(FIELD_UNDECLARED, _R, f"{kind}:{key}")
        for key in sorted(values)
        if key not in declared
    ]
    for key in sorted(declared):
        rule = declared[key]
        value = values.get(key)
        if value is None:
            if rule.required:
                reasons.append(Reason(missing_code, rule.missing_status, f"{kind}:{key}"))
            continue
        reasons.extend(_value_reasons(f"{kind}:{key}", rule, value))
    return reasons


def _listing_reasons(request: PreflightRequest, metadata: CategoryMetadata | None) -> list[Reason]:
    listing = request.listing
    reasons: list[Reason] = []
    name = listing.name
    if name is None or (not name.detail_page_reference and not name.value.strip()):
        reasons.append(Reason(LISTING_NAME_MISSING, _R, "name"))
    else:
        name_rule = FieldRule("name", required=True, max_length=None)
        reasons.extend(_value_reasons("name", name_rule, name))
        if metadata is not None and len(name.value) > metadata.name_max_length:
            reasons.append(Reason(LISTING_NAME_TOO_LONG, _R, "name"))
    if metadata is None:
        return reasons
    reasons.extend(
        _declared_reasons(
            "attribute", ATTRIBUTE_REQUIRED_MISSING, metadata.attributes, listing.attributes
        )
    )
    if metadata.notice is None:
        if listing.notices:
            reasons.append(Reason(NOTICE_POLICY_MISSING, _R, "notice"))
    else:
        reasons.extend(
            _declared_reasons(
                "notice", NOTICE_REQUIRED_MISSING, metadata.notice.fields, listing.notices
            )
        )
    return reasons


def _option_reasons(
    request: PreflightRequest, unit: ResolvedUnit, metadata: CategoryMetadata | None
) -> list[Reason]:
    """Deterministic option compatibility (ADR-0014 §2): a unit of several Items is one listing
    with options, and only the category metadata says whether and how it may be."""
    reasons: list[Reason] = []
    options = request.listing.options
    unit_ids = {item.item_id for item in unit.items}
    reasons.extend(
        Reason(FIELD_UNDECLARED, _R, f"option:{item}") for item in sorted(set(options) - unit_ids)
    )
    if len(unit.items) <= 1:
        reasons.extend(
            Reason(OPTION_VALUES_UNEXPECTED, _R, _subject(item.item_id, "options"))
            for item in unit.items
            if options.get(item.item_id)
        )
        return reasons
    if metadata is not None:
        policy = metadata.options
        if not policy.options_supported:
            reasons.append(Reason(OPTIONS_NOT_SUPPORTED, _B, "options"))
        elif len(unit.items) > policy.max_options:
            reasons.append(Reason(OPTION_COUNT_EXCEEDED, _B, "options"))
    values = []
    for item in unit.items:
        chosen = options.get(item.item_id) or {}
        if not chosen or not all(str(v).strip() for v in chosen.values()):
            reasons.append(Reason(OPTION_VALUE_MISSING, _R, _subject(item.item_id, "options")))
            continue
        values.append(tuple(sorted((str(k), str(v)) for k, v in chosen.items())))
    dimensions = {tuple(k for k, _v in combination) for combination in values}
    if len(dimensions) > 1:
        reasons.append(Reason(OPTION_DIMENSIONS_INCONSISTENT, _R, "options"))
    elif metadata is not None and dimensions:
        (dims,) = dimensions
        if len(dims) > metadata.options.max_dimensions:
            reasons.append(Reason(OPTION_DIMENSIONS_EXCEEDED, _B, "options"))
    if len(set(values)) != len(values):
        reasons.append(Reason(OPTION_VALUES_NOT_DISTINCT, _R, "options"))
    return reasons


def _template_reasons(target: TargetPolicy, metadata: CategoryMetadata | None) -> list[Reason]:
    if metadata is None:
        return []
    return [
        Reason(POLICY_TEMPLATE_MISSING, _R, f"template:{kind}")
        for kind in sorted(metadata.required_templates)
        if not str(target.templates.get(kind, "")).strip()
    ]


def _detail_reasons(request: PreflightRequest) -> list[Reason]:
    detail = request.detail
    if detail is None:
        return [Reason(DETAIL_COMPOSITION_MISSING, _R, "detail")]
    if not detail.body.strip():
        return [Reason(DETAIL_BODY_EMPTY, _R, "detail")]
    return []


def _authoring_reasons(request: PreflightRequest, unit: ResolvedUnit) -> list[Reason]:
    """Whether both authoring revisions have an owner-held value (decision 5800619183).

    The target policy holds ``None`` for a revision whose owner does not exist, and an authored
    selection or composition carries that ``None`` exactly. A value the preparation carries while
    the target policy holds ``None`` came from no owner, so it is unowned too: a client-supplied
    revision never makes a unit READY. Either absence is one statement — no owner stands behind
    the revision — and it is neither missing category metadata nor a missing policy. Every other
    rule is still evaluated as it always is."""
    target, category, detail = unit.target, request.category, request.detail
    unowned = (
        target.category_mapping_revision is None
        or target.detail_composition_revision is None
        or (category is not None and category.mapping_revision is None)
        or (detail is not None and detail.composition_revision is None)
    )
    return [Reason(AUTHORING_REVISIONS_UNOWNED, _R, "authoring")] if unowned else []


def _publication_reasons(target: TargetPolicy, unit: ResolvedUnit) -> list[Reason]:
    policy = target.asset_policy
    reasons: list[Reason] = []
    for item in unit.items:
        images = item.images
        if len(images) < max(policy.min_images, 1):
            reasons.append(Reason(PUBLICATION_ASSETS_MISSING, _R, _subject(item.item_id, "images")))
        if len(images) > policy.max_images:
            reasons.append(
                Reason(PUBLICATION_ASSET_COUNT_EXCEEDED, _R, _subject(item.item_id, "images"))
            )
        if policy.requires_representative and not any(
            image.role is ImageRole.REPRESENTATIVE for image in images
        ):
            reasons.append(
                Reason(PUBLICATION_REPRESENTATIVE_MISSING, _R, _subject(item.item_id, "images"))
            )
        for image in images:
            if image.qa_verdict is QaVerdict.PASS:
                continue
            status = _B if image.qa_verdict is QaVerdict.FAIL else _R
            where = f"{image.role.value}:{image.position}"
            reasons.append(
                Reason(PUBLICATION_ASSET_QA_NOT_PASSED, status, _subject(item.item_id, where))
            )
    return reasons


def _conflict_reasons(unit: ResolvedUnit) -> list[Reason]:
    """R2: an in-flight or unresolved CREATE in the scope. No override ever releases it."""
    return [
        Reason(UNRESOLVED_CREATE_CONFLICT, _B, f"intent:{conflict.intent_id}")
        for conflict in unit.conflicts
    ]


def _duplicate_reasons(request: PreflightRequest, unit: ResolvedUnit) -> list[Reason]:
    """ADR-0014 §13. A live listing ICBM knows, or a strong provider match, is DUPLICATE unless an
    active override of this account covers every Item of the unit; missing or inconclusive
    provider evidence is never READY where the target requires provider proof."""
    target = unit.target
    covered = bool(unit.items) and all(item.covered() for item in unit.items)
    reasons: list[Reason] = []
    if not covered:
        reasons.extend(
            Reason(LIVE_REGISTRATION_EXISTS, _D, f"registration:{live.registration_id}")
            for live in unit.live_registrations
        )
    evidence = request.duplicate_evidence
    if evidence is None:
        if target.duplicate_proof_required:
            reasons.append(Reason(DUPLICATE_EVIDENCE_MISSING, _R, "duplicate"))
        return reasons
    if evidence.unsafe_fields():
        # Before any verdict or override: unsanitized evidence is never READY (§15, B4).
        reasons.append(Reason(DUPLICATE_EVIDENCE_UNSAFE, _B, "duplicate"))
        return reasons
    if (evidence.marketplace_key, evidence.marketplace_account_id, evidence.listing_identity) != (
        unit.marketplace_key,
        unit.marketplace_account_id,
        unit.listing_identity,
    ):
        reasons.append(Reason(DUPLICATE_EVIDENCE_SCOPE_MISMATCH, _R, "duplicate"))
        return reasons
    if evidence.verdict is DuplicateVerdict.INCONCLUSIVE or (
        evidence.verdict is DuplicateVerdict.NO_MATCH and evidence.matches
    ):
        reasons.append(Reason(DUPLICATE_EVIDENCE_INCONCLUSIVE, _R, "duplicate"))
    elif evidence.verdict is DuplicateVerdict.MATCH:
        strong = any(match.key_kind in STRONG_KEYS for match in evidence.matches)
        if not evidence.matches:
            reasons.append(Reason(DUPLICATE_EVIDENCE_INCONCLUSIVE, _R, "duplicate"))
        elif not covered:
            code, status = (
                (PROVIDER_DUPLICATE_FOUND, _D) if strong else (PROVIDER_DUPLICATE_WEAK_SIGNAL, _R)
            )
            reasons.append(Reason(code, status, "duplicate"))
    elif target.duplicate_proof_required and not (
        target.duplicate_lookup_keys <= evidence.keys_checked
    ):
        reasons.append(Reason(DUPLICATE_EVIDENCE_INCOMPLETE, _R, "duplicate"))
    return reasons


def _sanitation_reasons(request: PreflightRequest, unit: ResolvedUnit) -> list[Reason]:
    """Every caller-supplied value that reaches the fingerprint or the payload is scanned: the
    listing values, the templates, the category selection and the whole detail composition."""
    listing, category, detail = request.listing, request.category, request.detail
    values: dict[str, Any] = {
        "name": None if listing.name is None else listing.name.value,
        "tags": sorted(listing.tags),
        "attributes": {k: v.value for k, v in listing.attributes.items()},
        "notices": {k: v.value for k, v in listing.notices.items()},
        "options": {k: dict(v) for k, v in listing.options.items()},
        "templates": dict(unit.target.templates),
        "category": None
        if category is None
        else [category.category_id, category.mapping_revision, category.taxonomy_revision],
        "detail": None
        if detail is None
        else {
            "composition_revision": detail.composition_revision,
            "sections": list(detail.sections),
            "body": detail.body,
        },
    }
    return [Reason(code, _B, where) for code, where in sanitize.problems(values, path="outbound")]


def _prepared_reasons(
    target: TargetPolicy,
    unit: ResolvedUnit,
    prepared: Sequence[PreparedAsset],
    candidate_fingerprint: str,
) -> list[Reason]:
    """The final stage only: each selected artifact has a provider asset prepared for exactly it,
    under this very candidate fingerprint and profile (ADR-0014 §3 B2, §5)."""
    policy = target.asset_policy
    if not policy.provider_asset_identity_required:
        return [Reason(PREPARED_ASSET_UNEXPECTED, _R, f"asset:{p.sha256}") for p in prepared]
    reasons: list[Reason] = []
    by_key: dict[tuple[str, str, str], PreparedAsset] = {}
    for asset in prepared:
        if asset.key in by_key:
            reasons.append(Reason(PREPARED_ASSET_DUPLICATED, _R, f"asset:{asset.sha256}"))
        by_key[asset.key] = asset
    selected = {image.key for item in unit.items for image in item.images}
    for key in sorted(selected):
        found = by_key.get(key)
        subject = f"asset:{key[1]}"
        if found is None:
            reasons.append(Reason(PROVIDER_ASSET_IDENTITY_MISSING, _R, subject))
            continue
        if found.candidate_fingerprint != candidate_fingerprint:
            reasons.append(Reason(PREPARED_ASSET_CANDIDATE_MISMATCH, _S, subject))
        if found.asset_profile != policy.profile:
            reasons.append(Reason(PREPARED_ASSET_PROFILE_MISMATCH, _S, subject))
        if found.unsafe():
            reasons.append(Reason(PREPARED_ASSET_REF_UNSAFE, _B, subject))
    reasons.extend(
        Reason(PREPARED_ASSET_NOT_SELECTED, _S, f"asset:{key[1]}")
        for key in sorted(set(by_key) - selected)
    )
    return reasons


# ---------------------------------------------------------------- dependencies and evaluation


def _listing_canonical(request: PreflightRequest, unit: ResolvedUnit) -> dict[str, object]:
    listing = request.listing
    unit_ids = [item.item_id for item in unit.items]
    return {
        "name": None if listing.name is None else listing.name.canonical(),
        "tags": sorted(set(listing.tags)),
        "attributes": {k: v.canonical() for k, v in listing.attributes.items()},
        "notices": {k: v.canonical() for k, v in listing.notices.items()},
        "options": {
            item: dict(listing.options[item]) for item in unit_ids if item in listing.options
        },
        "undeclared_options": sorted(set(listing.options) - set(unit_ids)),
    }


def _metadata_canonical(metadata: CategoryMetadata | None) -> dict[str, object] | None:
    if metadata is None:
        return None
    return {
        "taxonomy_revision": metadata.taxonomy_revision,
        "category_id": metadata.category_id,
        "metadata_revision": metadata.metadata_revision,
        "reviewed": metadata.reviewed,
        "leaf": metadata.leaf,
        "registrable": metadata.registrable,
    }


def candidate_dependencies(request: PreflightRequest, unit: ResolvedUnit) -> dict[str, Any]:
    """Every non-asset dependency of the unit, canonical. The resale advisory is not one."""
    target = unit.target
    policy = target.asset_policy
    category = request.category
    evidence = request.duplicate_evidence
    detail = request.detail
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "rule_version": PREFLIGHT_RULE_VERSION,
        "sanitizer_rules_version": sanitize.SANITIZER_RULES_VERSION,
        "target": {
            "marketplace_key": target.marketplace_key,
            "marketplace_account_id": target.marketplace_account_id,
            "policy_revision": target.policy_revision,
            "taxonomy_revision": target.taxonomy_revision,
            "pricing_context_fingerprint": target.pricing_context.fingerprint,
            "sanitizer_profile_version": target.sanitizer_profile_version,
            "templates": dict(target.templates),
            "duplicate_proof_required": target.duplicate_proof_required,
            "duplicate_lookup_keys": sorted(k.value for k in target.duplicate_lookup_keys),
            "asset_policy": {
                "profile": policy.profile,
                "min_images": policy.min_images,
                "max_images": policy.max_images,
                "requires_representative": policy.requires_representative,
                "provider_asset_identity_required": policy.provider_asset_identity_required,
            },
        },
        "account": {
            "marketplace_key": unit.marketplace_key,
            "marketplace_account_id": unit.marketplace_account_id,
            **unit.account.canonical(),
        },
        "draft": {
            "draft_id": unit.draft_id,
            "draft_revision": unit.draft_revision,
            "expected_draft_revision": request.unit.expected_draft_revision,
            "listing_shape": unit.listing_shape.value,
            "open_items": [[s.item_id, s.ordinal, s.pricing_snapshot_id] for s in unit.open_items],
        },
        "unit": {
            "item_ids": list(unit.unit_item_ids),
            "requested": None
            if request.unit.item_ids is None
            else sorted(set(request.unit.item_ids)),
            "listing_identity": unit.listing_identity,
            "identity_generation": unit.identity_generation,
        },
        "items": [item.canonical() for item in unit.items],
        "category": None
        if category is None
        else {
            "category_id": category.category_id,
            "mapping_revision": category.mapping_revision,
            "taxonomy_revision": category.taxonomy_revision,
            "confirmation": category.confirmation.value,
            "metadata": _metadata_canonical(unit.metadata),
        },
        "listing": _listing_canonical(request, unit),
        "detail": None
        if detail is None
        else {
            "composition_revision": detail.composition_revision,
            "sections": list(detail.sections),
            "body_digest": hashlib.sha256(detail.body.encode("utf-8")).hexdigest(),
        },
        "duplicate": {
            "evidence": None
            if evidence is None
            else evidence.canonical(
                (unit.marketplace_key, unit.marketplace_account_id, unit.listing_identity)
            ),
            "live_registrations": sorted(r.registration_id for r in unit.live_registrations),
        },
        "conflicts": sorted([c.intent_id, c.state] for c in unit.conflicts),
    }


def evaluate(
    request: PreflightRequest,
    unit: ResolvedUnit,
    stage: PreflightStage,
    prepared: Sequence[PreparedAsset] = (),
) -> PreflightResult:
    """The derived preflight of one provider-listing unit at one stage."""
    if stage is PreflightStage.CANDIDATE and prepared:
        raise ValueError("a candidate is evaluated without any provider asset identity")
    metadata = unit.metadata
    reasons: list[Reason] = [
        *_account_reasons(unit, unit.target),
        *_unit_reasons(request, unit),
        *_pricing_reasons(unit.target, unit),
        *_m4_reasons(unit),
        *_category_reasons(request, unit),
        *_listing_reasons(request, metadata),
        *_option_reasons(request, unit, metadata),
        *_template_reasons(unit.target, metadata),
        *_detail_reasons(request),
        *_authoring_reasons(request, unit),
        *_publication_reasons(unit.target, unit),
        *_conflict_reasons(unit),
        *_duplicate_reasons(request, unit),
        *_sanitation_reasons(request, unit),
    ]
    dependencies = candidate_dependencies(request, unit)
    candidate = fingerprint(dependencies)
    final = candidate
    prepared_assets = tuple(sorted(prepared, key=lambda a: (a.key, a.provider_asset_ref)))
    if stage is PreflightStage.FINAL:
        reasons.extend(_prepared_reasons(unit.target, unit, prepared_assets, candidate))
        dependencies = {
            "fingerprint_version": FINGERPRINT_VERSION,
            "stage": PreflightStage.FINAL.value,
            "candidate_fingerprint": candidate,
            "prepared_assets": [asset.canonical() for asset in prepared_assets],
            "candidate": dependencies,
        }
        final = fingerprint(dependencies)
    ordered = _ordered(reasons)
    status = precedence_status(ordered)
    return PreflightResult(
        stage=stage,
        status=status,
        reasons=ordered,
        rule_version=PREFLIGHT_RULE_VERSION,
        fingerprint_version=FINGERPRINT_VERSION,
        candidate_fingerprint=candidate,
        dependency_fingerprint=final,
        dependencies=dependencies,
        upload_permitted=(
            stage is PreflightStage.CANDIDATE
            and status is ReadinessStatus.READY
            and unit.target.asset_policy.provider_asset_identity_required
        ),
        request=request,
        resolved=unit,
        prepared_assets=prepared_assets,
        advisories=request.advisories,
    )
