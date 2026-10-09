"""B-UX2: what an operator can do about one reason, server-owned (ADR-0014 §22 amendment note).

Every readiness reason (the registration preflight's, the M4 owners' propagated ones) and every
REGISTRATION_ERROR review condition gets one **actionability** and, only when the operator can act,
the **surface** where they act:

- ``FIX_AVAILABLE`` — an existing screen changes the owner truth behind the reason; its surface is
  named. The screen never fixes anything itself here: it only says where.
- ``RECHECK`` — a read-only re-check the server already offers (re-evaluate, verify, reconcile).
- ``WAITING`` — the owner moves on by itself; nothing to do now.
- ``NO_OPERATOR_ACTION`` — a source fact the operator cannot change in ICBM (a sold-out source).
- ``NOT_IMPLEMENTED`` — no surface exists yet. A reason is never given a fix it does not have:
  a code missing from this table is ``NOT_IMPLEMENTED`` with its real code, never a fake button.

The status of a reason stays its owner's; this table only adds what can be done. It reads no
owner and decides no readiness.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

ACTIONABILITY_VERSION: Final = "register-actionability/v1"

M4_BASE_PREFIX: Final = "M4_BASE."
M4_PRICING_PREFIX: Final = "M4_PRICING."
IMAGE_FINDING_PREFIX: Final = "IMAGE_FINDING_"


class Actionability(StrEnum):
    FIX_AVAILABLE = "FIX_AVAILABLE"
    RECHECK = "RECHECK"
    WAITING = "WAITING"
    NO_OPERATOR_ACTION = "NO_OPERATOR_ACTION"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class FixSurface(StrEnum):
    """An existing screen where the operator acts. The UI only navigates there."""

    REGISTER_PREPARATION = "REGISTER_PREPARATION"
    REGISTER_ACTION = "REGISTER_ACTION"
    SETTINGS_TARGET_POLICY = "SETTINGS_TARGET_POLICY"
    SETTINGS_CATEGORY_METADATA = "SETTINGS_CATEGORY_METADATA"
    SETTINGS_CONNECT = "SETTINGS_CONNECT"
    # B-EDITOR: the unit workspace's image section (the image owner's selection routes).
    REGISTER_IMAGES = "REGISTER_IMAGES"


ACTIONABILITY_LABELS: Final[Mapping[Actionability, str]] = {
    Actionability.FIX_AVAILABLE: "수정 가능",
    Actionability.RECHECK: "재확인",
    Actionability.WAITING: "대기",
    Actionability.NO_OPERATOR_ACTION: "조치 없음",
    Actionability.NOT_IMPLEMENTED: "수정 화면 없음",
}

SURFACE_LABELS: Final[Mapping[FixSurface, str]] = {
    FixSurface.REGISTER_PREPARATION: "등록관리 › 등록 준비 입력",
    FixSurface.REGISTER_ACTION: "등록관리 › 단위 동작",
    FixSurface.SETTINGS_TARGET_POLICY: "설정 › 등록 정책",
    FixSurface.SETTINGS_CATEGORY_METADATA: "설정 › 카테고리 메타데이터",
    FixSurface.SETTINGS_CONNECT: "설정 › 마켓 연결",
    FixSurface.REGISTER_IMAGES: "등록관리 › 이미지",
}

_F, _R, _W, _N, _X = (
    Actionability.FIX_AVAILABLE,
    Actionability.RECHECK,
    Actionability.WAITING,
    Actionability.NO_OPERATOR_ACTION,
    Actionability.NOT_IMPLEMENTED,
)
_PREP, _ACT, _POLICY, _META, _CONNECT = (
    FixSurface.REGISTER_PREPARATION,
    FixSurface.REGISTER_ACTION,
    FixSurface.SETTINGS_TARGET_POLICY,
    FixSurface.SETTINGS_CATEGORY_METADATA,
    FixSurface.SETTINGS_CONNECT,
)
_IMAGES = FixSurface.REGISTER_IMAGES

Entry = tuple[Actionability, FixSurface | None]

# The registration preflight's own reasons (``preparation.REASON_CODES``).
REGISTER_ACTIONS: Final[Mapping[str, Entry]] = {
    # Account and CONNECT: the marketplace connection in Settings.
    "ACCOUNT_UNKNOWN": (_F, _CONNECT),
    "ACCOUNT_NOT_BOUND": (_F, _CONNECT),
    "ACCOUNT_BINDING_MISMATCH": (_F, _CONNECT),
    "TARGET_SCOPE_MISMATCH": (_F, _POLICY),
    "CONNECT_CAPABILITY_UNAVAILABLE": (_X, None),
    "CONNECT_AUTH_NOT_READY": (_F, _CONNECT),
    "CONNECT_AUTH_MISMATCH": (_F, _CONNECT),
    "CONNECT_NOT_BOUND": (_F, _CONNECT),
    "CONNECT_WRITE_SCOPE_MISSING": (_X, None),
    "CONNECT_WORKFLOW_PAUSED": (_X, None),
    "CONNECT_WORKFLOW_REVIEW_REQUIRED": (_X, None),
    # The unit: a moved Draft is re-authored; no screen reshapes a unit yet.
    "DRAFT_REVISION_STALE": (_F, _PREP),
    "UNIT_EMPTY": (_X, None),
    "UNIT_ITEM_NOT_OPEN": (_X, None),
    "UNIT_ITEM_DUPLICATED": (_X, None),
    "UNIT_SINGLE_NOT_COVERING": (_X, None),
    "UNIT_SEPARATE_NOT_ONE_ITEM": (_X, None),
    "LISTING_IDENTITY_INVALID": (_X, None),
    # Price: the pricing context is the target policy's; a pin is moved only by the Draft command
    # owner's re-pin (B-PRICE1), which has M4 price again — the unit's "가격 다시 고정" action.
    # An open Item always holds a pin, so a missing one has no operator fix.
    "TARGET_PRICING_CONTEXT_INVALID": (_F, _POLICY),
    "DRAFT_PRICE_PIN_MISSING": (_X, None),
    "DRAFT_PRICE_PIN_CONTEXT_INVALID": (_F, _ACT),
    "DRAFT_PRICE_PIN_CONTEXT_STALE": (_F, _ACT),
    "DRAFT_PRICE_PIN_SUPERSEDED": (_F, _ACT),
    # Category: chosen in the preparation form; its reviewed metadata lives in Settings.
    "CATEGORY_NOT_SELECTED": (_F, _PREP),
    "CATEGORY_NOT_CONFIRMED": (_F, _PREP),
    "CATEGORY_TAXONOMY_STALE": (_F, _PREP),
    "CATEGORY_METADATA_MISSING": (_F, _META),
    "CATEGORY_METADATA_UNREVIEWED": (_F, _META),
    "CATEGORY_NOT_LEAF": (_F, _PREP),
    "CATEGORY_RESTRICTED": (_F, _PREP),
    # Listing values, notices, options and the detail body: the preparation form.
    "LISTING_NAME_MISSING": (_F, _PREP),
    "LISTING_NAME_TOO_LONG": (_F, _PREP),
    "ATTRIBUTE_REQUIRED_MISSING": (_F, _PREP),
    "NOTICE_REQUIRED_MISSING": (_F, _PREP),
    "NOTICE_POLICY_MISSING": (_F, _META),
    "NOTICE_TYPE_UNDOCUMENTED": (_F, _META),
    "FIELD_UNDECLARED": (_F, _PREP),
    "FIELD_VALUE_EMPTY": (_F, _PREP),
    "FIELD_VALUE_TOO_LONG": (_F, _PREP),
    "FIELD_VALUE_TYPE_MISMATCH": (_F, _PREP),
    "FIELD_VALUE_FORM_INVALID": (_F, _PREP),
    "FIELD_AI_SUGGESTION_UNCONFIRMED": (_F, _PREP),
    "FIELD_DETAIL_REFERENCE_NOT_PERMITTED": (_F, _PREP),
    "OPTIONS_NOT_SUPPORTED": (_X, None),
    "OPTION_COUNT_EXCEEDED": (_X, None),
    "OPTION_VALUE_MISSING": (_F, _PREP),
    "OPTION_VALUES_UNEXPECTED": (_F, _PREP),
    "OPTION_DIMENSIONS_INCONSISTENT": (_F, _PREP),
    "OPTION_DIMENSIONS_EXCEEDED": (_X, None),
    "OPTION_VALUES_NOT_DISTINCT": (_F, _PREP),
    "POLICY_TEMPLATE_MISSING": (_F, _POLICY),
    "POLICY_AFTER_SERVICE_PHONE_MISSING": (_F, _POLICY),
    "DETAIL_COMPOSITION_MISSING": (_F, _PREP),
    "DETAIL_BODY_EMPTY": (_F, _PREP),
    # The authoring revisions are stamped by a target-policy save, then authored again.
    "AUTHORING_REVISIONS_UNOWNED": (_F, _POLICY),
    # Images: the selection is made in the unit workspace's image section (B-EDITOR). QA is the
    # automatic rule's, and uploads happen on the LIVE path.
    "PUBLICATION_ASSETS_MISSING": (_F, _IMAGES),
    "PUBLICATION_ASSET_COUNT_EXCEEDED": (_F, _IMAGES),
    "PUBLICATION_REPRESENTATIVE_MISSING": (_F, _IMAGES),
    # B-DETAIL's reason, consumed as it is: no screen places a detail image here.
    "PUBLICATION_DETAIL_IMAGES_UNPLACED": (_X, None),
    "PUBLICATION_ASSET_QA_NOT_PASSED": (_X, None),
    "PROVIDER_ASSET_IDENTITY_MISSING": (_X, None),
    "PREPARED_ASSET_CANDIDATE_MISMATCH": (_X, None),
    "PREPARED_ASSET_PROFILE_MISMATCH": (_X, None),
    "PREPARED_ASSET_NOT_SELECTED": (_X, None),
    "PREPARED_ASSET_DUPLICATED": (_X, None),
    "PREPARED_ASSET_REF_UNSAFE": (_X, None),
    "PREPARED_ASSET_UNEXPECTED": (_X, None),
    # A CREATE whose outcome is unknown is re-checked by a read-only reconcile.
    "UNRESOLVED_CREATE_CONFLICT": (_R, _ACT),
    "LIVE_REGISTRATION_EXISTS": (_X, None),
    "ADOPTED_LISTING_EXISTS": (_X, None),
    # ADR-0031 §4.1: nothing the operator does here clears a supplier's own restriction.
    "SOURCE_CHANNEL_FORBIDDEN": (_X, None),
    "SOURCE_CHANNEL_UNRESOLVED": (_X, None),
    "SUPPLIER_NOT_ACTIVE": (_X, None),
    "PROVIDER_DUPLICATE_FOUND": (_X, None),
    "PROVIDER_DUPLICATE_WEAK_SIGNAL": (_X, None),
    # No provider duplicate lookup is adopted yet.
    "DUPLICATE_EVIDENCE_MISSING": (_X, None),
    "DUPLICATE_EVIDENCE_INCONCLUSIVE": (_X, None),
    "DUPLICATE_EVIDENCE_INCOMPLETE": (_X, None),
    "DUPLICATE_EVIDENCE_SCOPE_MISMATCH": (_X, None),
    "DUPLICATE_EVIDENCE_UNSAFE": (_X, None),
    # An authored value carrying a URL or secret material: removed in the form.
    "PAYLOAD_SECRET_MATERIAL": (_F, _PREP),
    "PAYLOAD_EXTERNAL_URL": (_F, _PREP),
}

# The M4 owners' reasons (unprefixed). Only the pricing snapshot is moved from REGISTER, through the
# re-pin; a sold-out source is a supplier fact; the rest have no operator screen.
M4_ACTIONS: Final[Mapping[str, Entry]] = {
    "SOURCE_STOCK_SOLD_OUT": (_N, None),
    # The re-pin has M4 price the Item under the policy's context (B-PRICE1).
    "PRICING_SNAPSHOT_MISSING": (_F, _ACT),
    "PRICING_SNAPSHOT_SUPERSEDED": (_F, _ACT),
    # The image owner's selection reasons: chosen in the workspace's image section (B-EDITOR).
    "IMAGE_SELECTION_MISSING": (_F, _IMAGES),
    "IMAGE_SELECTION_STALE": (_F, _IMAGES),
    "IMAGE_SELECTION_EMPTY": (_F, _IMAGES),
    "IMAGE_SELECTION_RECHECK_REQUIRED": (_F, _IMAGES),
}

# REGISTRATION_ERROR review conditions of the REGISTER execution producer.
REVIEW_ACTIONS: Final[Mapping[str, Entry]] = {
    "REGISTER_INTENT_UNKNOWN": (_R, _ACT),
    "REGISTER_READ_VERIFICATION_OVERDUE": (_R, _ACT),
    "REGISTER_READBACK_MISMATCH": (_X, None),
    # A brake the operator may resume; an AUTH brake waits for the connection to be repaired.
    "REGISTER_SCOPE_PAUSED_POLICY": (_F, _ACT),
    "REGISTER_SCOPE_PAUSED_FAILURE_BUDGET": (_F, _ACT),
    "REGISTER_SCOPE_PAUSED_AUTH": (_F, _CONNECT),
}

# Why a unit could not be evaluated (the owner's refusal code).
NOT_EVALUATED_ACTIONS: Final[Mapping[str, Entry]] = {
    "REGISTER_PREPARATION_ABSENT": (_F, _PREP),
    "REGISTER_TARGET_POLICY_MISSING": (_F, _POLICY),
}

_UNMAPPED: Final[Entry] = (_X, None)


def preflight_action(code: str) -> Entry:
    """What can be done about one registration-preflight reason code, as the owner returned it."""
    if code in REGISTER_ACTIONS:
        return REGISTER_ACTIONS[code]
    for prefix in (M4_BASE_PREFIX, M4_PRICING_PREFIX):
        if code.startswith(prefix):
            return m4_action(code[len(prefix) :])
    return _UNMAPPED


def m4_action(code: str) -> Entry:
    return M4_ACTIONS.get(code, _UNMAPPED)


def review_action(code: str) -> Entry:
    """What can be done about one review condition: the execution producer's own, else the
    preparation producer's (a preflight code), else not implemented."""
    if code in REVIEW_ACTIONS:
        return REVIEW_ACTIONS[code]
    return preflight_action(code)


def not_evaluated_action(code: str) -> Entry:
    return NOT_EVALUATED_ACTIONS.get(code, _UNMAPPED)
