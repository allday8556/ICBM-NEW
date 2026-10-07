"""B-UX1: the server-owned area classification of a readiness reason (ADR-0014 §3, §22).

A reason is consumed exactly as its owner returned it — code, status, subject — and is never
re-judged here: this module adds only **where** a reason belongs, so a screen can group and count
reasons without recomputing readiness. Area and status are orthogonal: a reason keeps its owner's
status, and one reason may belong to several areas (an authoring revision concerns both the
category and the detail composition).

The classification is total. A code missing from the table — a new owner reason this table has not
caught up with — is ``UNCLASSIFIED`` and keeps its real code; it is never dropped, defaulted to
another area or hidden. A contract test pins the table to every inventoried reason.

Pure: no database, no clock and no I/O. It imports no evaluating owner (preflight, readiness,
pricing), so the screen service may read it.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

REASON_AREA_VERSION: Final = "register-reason-area/v1"

# How an M4 reason is propagated into a registration preflight (preparation.M4_*_PREFIX).
M4_BASE_PREFIX: Final = "M4_BASE."
M4_PRICING_PREFIX: Final = "M4_PRICING."
# A dynamic M4 image-QA finding family (``IMAGE_FINDING_<finding>``).
IMAGE_FINDING_PREFIX: Final = "IMAGE_FINDING_"


class ReasonArea(StrEnum):
    ACCOUNT = "ACCOUNT"
    UNIT = "UNIT"
    SOURCE = "SOURCE"
    PRICE = "PRICE"
    CATEGORY = "CATEGORY"
    LISTING = "LISTING"
    NOTICE = "NOTICE"
    OPTIONS = "OPTIONS"
    POLICY = "POLICY"
    DETAIL = "DETAIL"
    IMAGES = "IMAGES"
    DUPLICATE = "DUPLICATE"
    SAFETY = "SAFETY"
    UNCLASSIFIED = "UNCLASSIFIED"


# The operator-facing label of each area (server-side, like the read-state labels).
AREA_LABELS: Final[Mapping[ReasonArea, str]] = {
    ReasonArea.ACCOUNT: "계정·연결",
    ReasonArea.UNIT: "등록 단위",
    ReasonArea.SOURCE: "원본 상품",
    ReasonArea.PRICE: "가격",
    ReasonArea.CATEGORY: "카테고리",
    ReasonArea.LISTING: "상품명·속성",
    ReasonArea.NOTICE: "상품정보제공고시",
    ReasonArea.OPTIONS: "옵션",
    ReasonArea.POLICY: "배송·반품 정책",
    ReasonArea.DETAIL: "상세 본문",
    ReasonArea.IMAGES: "이미지",
    ReasonArea.DUPLICATE: "중복·충돌",
    ReasonArea.SAFETY: "외부 링크·민감정보",
    ReasonArea.UNCLASSIFIED: "미분류",
}

_A = ReasonArea

# The registration preflight's own reasons (app.stages.register.preparation.REASON_CODES).
REGISTER_AREAS: Final[Mapping[str, tuple[ReasonArea, ...]]] = {
    "ACCOUNT_UNKNOWN": (_A.ACCOUNT,),
    "ACCOUNT_NOT_BOUND": (_A.ACCOUNT,),
    "ACCOUNT_BINDING_MISMATCH": (_A.ACCOUNT,),
    "TARGET_SCOPE_MISMATCH": (_A.ACCOUNT,),
    "CONNECT_CAPABILITY_UNAVAILABLE": (_A.ACCOUNT,),
    "CONNECT_AUTH_NOT_READY": (_A.ACCOUNT,),
    "CONNECT_AUTH_MISMATCH": (_A.ACCOUNT,),
    "CONNECT_NOT_BOUND": (_A.ACCOUNT,),
    "CONNECT_WRITE_SCOPE_MISSING": (_A.ACCOUNT,),
    "CONNECT_WORKFLOW_PAUSED": (_A.ACCOUNT,),
    "CONNECT_WORKFLOW_REVIEW_REQUIRED": (_A.ACCOUNT,),
    "DRAFT_REVISION_STALE": (_A.UNIT,),
    "UNIT_EMPTY": (_A.UNIT,),
    "UNIT_ITEM_NOT_OPEN": (_A.UNIT,),
    "UNIT_ITEM_DUPLICATED": (_A.UNIT,),
    "UNIT_SINGLE_NOT_COVERING": (_A.UNIT,),
    "UNIT_SEPARATE_NOT_ONE_ITEM": (_A.UNIT,),
    "LISTING_IDENTITY_INVALID": (_A.UNIT,),
    "TARGET_PRICING_CONTEXT_INVALID": (_A.PRICE,),
    "DRAFT_PRICE_PIN_MISSING": (_A.PRICE,),
    "DRAFT_PRICE_PIN_CONTEXT_INVALID": (_A.PRICE,),
    "DRAFT_PRICE_PIN_CONTEXT_STALE": (_A.PRICE,),
    "DRAFT_PRICE_PIN_SUPERSEDED": (_A.PRICE,),
    "CATEGORY_NOT_SELECTED": (_A.CATEGORY,),
    "CATEGORY_NOT_CONFIRMED": (_A.CATEGORY,),
    "CATEGORY_TAXONOMY_STALE": (_A.CATEGORY,),
    "CATEGORY_METADATA_MISSING": (_A.CATEGORY,),
    "CATEGORY_METADATA_UNREVIEWED": (_A.CATEGORY,),
    "CATEGORY_NOT_LEAF": (_A.CATEGORY,),
    "CATEGORY_RESTRICTED": (_A.CATEGORY,),
    "LISTING_NAME_MISSING": (_A.LISTING,),
    "LISTING_NAME_TOO_LONG": (_A.LISTING,),
    "ATTRIBUTE_REQUIRED_MISSING": (_A.LISTING,),
    "NOTICE_REQUIRED_MISSING": (_A.NOTICE,),
    "NOTICE_POLICY_MISSING": (_A.NOTICE,),
    "NOTICE_TYPE_UNDOCUMENTED": (_A.NOTICE,),
    # A field rule's reason concerns an attribute or a notice field; its subject names which.
    "FIELD_UNDECLARED": (_A.LISTING, _A.NOTICE),
    "FIELD_VALUE_EMPTY": (_A.LISTING, _A.NOTICE),
    "FIELD_VALUE_TOO_LONG": (_A.LISTING, _A.NOTICE),
    "FIELD_VALUE_TYPE_MISMATCH": (_A.LISTING, _A.NOTICE),
    "FIELD_VALUE_FORM_INVALID": (_A.LISTING, _A.NOTICE),
    "FIELD_AI_SUGGESTION_UNCONFIRMED": (_A.LISTING, _A.NOTICE),
    "FIELD_DETAIL_REFERENCE_NOT_PERMITTED": (_A.LISTING, _A.NOTICE),
    "OPTIONS_NOT_SUPPORTED": (_A.OPTIONS,),
    "OPTION_COUNT_EXCEEDED": (_A.OPTIONS,),
    "OPTION_VALUE_MISSING": (_A.OPTIONS,),
    "OPTION_VALUES_UNEXPECTED": (_A.OPTIONS,),
    "OPTION_DIMENSIONS_INCONSISTENT": (_A.OPTIONS,),
    "OPTION_DIMENSIONS_EXCEEDED": (_A.OPTIONS,),
    "OPTION_VALUES_NOT_DISTINCT": (_A.OPTIONS,),
    "POLICY_TEMPLATE_MISSING": (_A.POLICY,),
    "POLICY_AFTER_SERVICE_PHONE_MISSING": (_A.POLICY,),
    "DETAIL_COMPOSITION_MISSING": (_A.DETAIL,),
    "DETAIL_BODY_EMPTY": (_A.DETAIL,),
    # The category-mapping and detail-composition authoring revisions, together (§27.1).
    "AUTHORING_REVISIONS_UNOWNED": (_A.CATEGORY, _A.DETAIL),
    "PUBLICATION_ASSETS_MISSING": (_A.IMAGES,),
    "PUBLICATION_ASSET_COUNT_EXCEEDED": (_A.IMAGES,),
    "PUBLICATION_REPRESENTATIVE_MISSING": (_A.IMAGES,),
    # A selected detail-body image without a place in the detail composition (Issue #219).
    "PUBLICATION_DETAIL_IMAGES_UNPLACED": (_A.IMAGES, _A.DETAIL),
    "PUBLICATION_ASSET_QA_NOT_PASSED": (_A.IMAGES,),
    "PROVIDER_ASSET_IDENTITY_MISSING": (_A.IMAGES,),
    "PREPARED_ASSET_CANDIDATE_MISMATCH": (_A.IMAGES,),
    "PREPARED_ASSET_PROFILE_MISMATCH": (_A.IMAGES,),
    "PREPARED_ASSET_NOT_SELECTED": (_A.IMAGES,),
    "PREPARED_ASSET_DUPLICATED": (_A.IMAGES,),
    "PREPARED_ASSET_REF_UNSAFE": (_A.IMAGES,),
    "PREPARED_ASSET_UNEXPECTED": (_A.IMAGES,),
    "UNRESOLVED_CREATE_CONFLICT": (_A.DUPLICATE,),
    "LIVE_REGISTRATION_EXISTS": (_A.DUPLICATE,),
    "PROVIDER_DUPLICATE_FOUND": (_A.DUPLICATE,),
    "PROVIDER_DUPLICATE_WEAK_SIGNAL": (_A.DUPLICATE,),
    "DUPLICATE_EVIDENCE_MISSING": (_A.DUPLICATE,),
    "DUPLICATE_EVIDENCE_INCONCLUSIVE": (_A.DUPLICATE,),
    "DUPLICATE_EVIDENCE_INCOMPLETE": (_A.DUPLICATE,),
    "DUPLICATE_EVIDENCE_SCOPE_MISMATCH": (_A.DUPLICATE,),
    "DUPLICATE_EVIDENCE_UNSAFE": (_A.DUPLICATE,),
    "PAYLOAD_SECRET_MATERIAL": (_A.SAFETY,),
    "PAYLOAD_EXTERNAL_URL": (_A.SAFETY,),
}

# The M4 owners' reasons, as each layer returns them (unprefixed).
M4_AREAS: Final[Mapping[str, tuple[ReasonArea, ...]]] = {
    # readiness
    "GROUP_MEMBER_CANDIDATE_PENDING": (_A.SOURCE,),
    "SOURCE_CORE_FIELD_REVIEW_REQUIRED": (_A.SOURCE,),
    "SOURCE_CORE_FIELD_ABSENT": (_A.SOURCE,),
    "SOURCE_STOCK_SOLD_OUT": (_A.SOURCE,),
    "PRICING_SNAPSHOT_MISSING": (_A.PRICE,),
    "PRICING_SNAPSHOT_SUPERSEDED": (_A.PRICE,),
    "PRICE_LOSS": (_A.PRICE,),
    "PRICE_BELOW_MIN_MARGIN": (_A.PRICE,),
    # procurement
    "PRODUCT_GROUP_RETIRED": (_A.SOURCE,),
    "MEMBERSHIP_REVISION_MISSING": (_A.SOURCE,),
    "MEMBERSHIP_REVISION_NOT_CURRENT": (_A.SOURCE,),
    "BINDING_MISSING": (_A.SOURCE,),
    "BINDING_COMPOSITION_INVALID": (_A.SOURCE,),
    "BINDING_OFFER_INVALID": (_A.SOURCE,),
    "BINDING_MEMBER_NOT_CONFIRMED": (_A.SOURCE,),
    "BINDING_PROVENANCE_STALE": (_A.SOURCE,),
    "ATOMIC_SKU_SET_STALE": (_A.SOURCE,),
    "ATOMIC_SKU_BINDING_MISSING": (_A.SOURCE,),
    "ATOMIC_SKU_BINDING_STALE": (_A.SOURCE,),
    # pricing
    "PRICING_PURCHASE_PRICE_UNRESOLVED": (_A.PRICE,),
    "PRICING_PURCHASE_PRICE_AMBIGUOUS": (_A.PRICE,),
    "PRICING_SHIPPING_UNRESOLVED": (_A.PRICE,),
    "PRICING_SHIPPING_CONDITIONAL": (_A.PRICE,),
    "PRICING_SHIPPING_UNKNOWN": (_A.PRICE,),
    "PRICING_MINIMUM_SALE_PRICE_UNRESOLVED": (_A.PRICE,),
    "PRICING_MINIMUM_SALE_PRICE_NOT_POSITIVE": (_A.PRICE,),
    "PRICING_MINIMUM_SALE_PRICE_NOT_OFFER_BOUND": (_A.PRICE,),
    "PRICING_TARGET_MARGIN_UNREACHABLE": (_A.PRICE,),
    "PRICING_QUANTITY_OFFER_UNRESOLVED": (_A.PRICE,),
    # images
    "IMAGE_SELECTION_MISSING": (_A.IMAGES,),
    "IMAGE_SELECTION_EMPTY": (_A.IMAGES,),
    "IMAGE_QA_MISSING": (_A.IMAGES,),
    "IMAGE_QA_REVIEW_REQUIRED": (_A.IMAGES,),
    "IMAGE_SELECTION_RECHECK_REQUIRED": (_A.IMAGES,),
    "IMAGE_SELECTION_STALE": (_A.IMAGES,),
    "IMAGE_DERIVATION_STALE": (_A.IMAGES,),
    "IMAGE_QA_STALE": (_A.IMAGES,),
    "IMAGE_QA_FAILED": (_A.IMAGES,),
    "ATOMIC_SKU_IMAGE_SELECTION_UNAVAILABLE": (_A.IMAGES,),
}

# A layer that is not READY but returned no reason propagates as ``M4_<LAYER>.<STATUS>``.
_LAYER_FALLBACK: Final[Mapping[str, ReasonArea]] = {
    M4_BASE_PREFIX: _A.SOURCE,
    M4_PRICING_PREFIX: _A.PRICE,
}
_STATUSES: Final = frozenset({"READY", "REVIEW_REQUIRED", "BLOCKED", "DUPLICATE", "STALE"})


def m4_areas(code: str, *, layer_prefix: str | None = None) -> tuple[ReasonArea, ...]:
    """The areas of one unprefixed M4 reason (``layer_prefix`` names the layer it came from)."""
    if code in M4_AREAS:
        return M4_AREAS[code]
    if code.startswith(IMAGE_FINDING_PREFIX):
        return (_A.IMAGES,)
    if layer_prefix is not None and layer_prefix in _LAYER_FALLBACK and code in _STATUSES:
        return (_LAYER_FALLBACK[layer_prefix],)
    return (_A.UNCLASSIFIED,)


def areas(code: str) -> tuple[ReasonArea, ...]:
    """The areas of one registration-preflight reason code, exactly as the owner returned it."""
    if code in REGISTER_AREAS:
        return REGISTER_AREAS[code]
    for prefix in (M4_BASE_PREFIX, M4_PRICING_PREFIX):
        if code.startswith(prefix):
            return m4_areas(code[len(prefix) :], layer_prefix=prefix)
    return (_A.UNCLASSIFIED,)
