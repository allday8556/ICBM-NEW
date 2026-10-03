"""The SmartStore product CREATE wire contract of a frozen RegistrationSnapshot (M5 PR-D; CREATE
adoption slice, ADR-0020 §4 order 1).

The input is only the PR-C canonical payload — the immutable Snapshot's own outbound values. This
module never re-prices, never re-reads a source fact, never invents a category, a notice or an
option, and never consults the current Draft (ADR-0014 §6, §11; kickoff §3).

**The adopted request contract.** ``documents/evidence/marketplace-apis/PRODUCT_CREATE.md`` §
SmartStore records the official 2.89.0 CREATE contract field by field (packet ``5746489554``,
reviews ``5768199984`` / ``5768247290``, field packet ``5861477977``, required/conditional packet
``5861933729``, registration-requirement packet ``5862400626``, value-level packet ``5868542027``).
The CREATE adoption slice freezes it here. The provider's request top level is ``originProduct``
plus the required ``smartstoreChannelProduct``, and this projection emits both. Every value of them
is either a Snapshot value or an ICBM-owned first-vertical decision (architect resolution
``5915900049`` D1, D2); anything else stays a named *gap* and is never guessed.
``windowChannelProduct`` is a separate Shopping Window channel structure, out of scope, and is
**never emitted**.

**What this module may spell** is exactly what that record captures — the nested request field
names, the fields required on registration, the M5-relevant conditional rules and the documented
limits and defaults. Two rules bound every projection:

* a structure that is *not required* is emitted only when the immutable Snapshot owns its values and
  the documented condition applies; the schema containing a structure is never a reason to send it;
* a value the official evidence does not carry — an enumeration, a value type, a notice type
  child — is **never invented**, and neither is a value whose type the evidence does carry but
  which no ICBM owner decides. It is recorded as a named *gap*, the projection is not
  ``sendable``. The REGISTER execution owner refuses such a projection with
  ``REGISTER_WIRE_NOT_SENDABLE`` before it opens an Attempt, and the SmartStore CREATE sender —
  which re-projects the Snapshot itself — refuses it again with its own adapter-level code
  ``SMARTSTORE_CREATE_WIRE_NOT_SENDABLE``; neither lets a transport exist. The two codes name the
  same condition at two layers, each owned by its layer.

Both rules would be worth little if an arbitrary mapping could still be handed to the wire, so the
request is checked as a whole and then frozen: :func:`create_document` refuses any path the
captured evidence does not record, any value outside a documented bound, and any body whose
``sellerManagementCode`` is not this listing identity's projection — and keeps what survives as
canonical JSON in an immutable :class:`CreateDocument`. Nothing can be added to a request, or
changed in one, between the projection and the wire, and the endpoint caller accepts that frozen
document and nothing else.

The value-level packet ``5868542027`` (``NAVER-P0-VALUES-CREATE-289``) closes two request facts and
no more: CREATE accepts only ``SALE`` as ``originProduct.statusType`` (E2), which is therefore
projected; and ``smartstoreChannelProduct.naverShoppingRegistration`` is a required JSON boolean
(E1) — which closes its *type* only. Which boolean ICBM publishes with is the ICBM decision of D2.1
below.

**The display status is owned** (architect resolution ``5915900049`` D1): the first vertical
registers the SmartStore channel with ``channelProductDisplayStatusType = ON``, an ICBM publication
decision frozen with the Snapshot's projection and never derived from the provider or a session.
It is projected as a constant, like ``SALE``, and it is what the read-back compares the published
state against (``readback.expected_published_state``).

**The remaining CREATE values are owned** (architect resolution ``5915900049`` D2), each frozen
with the Snapshot's projection and never derived from the provider or a session:

* D2.1 — ``naverShoppingRegistration = true``: ICBM's publication intent. It asserts nothing about
  the account; the official contract stores ``false`` for a non-advertiser, and no account
  capability is inferred from it.
* D2.2 — ``originProduct.stockQuantity = 1``: an ICBM registration seed, not a claim that the
  supplier holds one unit. A Snapshot exists only for a unit whose final preflight is ``READY`` —
  a sold-out source is ``BLOCKED`` by the M4 readiness it consumes — and the read-back compares the
  seed exactly (``readback.compare``). No source quantity is fabricated.
* D2.3 — the ``productInfoProvidedNotice`` child is selected from the reviewed
  ``CategoryMetadata`` notice type through the provider notice schema
  (:mod:`integrations.marketplaces.smartstore.notice_schema`,
  ``smartstore-notice-schema/2.90.0-r1``; notice coverage S3), never by casing a string. Only the
  Snapshot's own reviewed values are projected, each with its own JSON type; the schema decides
  which fields exist, which are required and which the provider fills when they are omitted. A
  notice type without a documented child stays a named *gap*, and never falls back to another
  child.

The gap that still holds for every Snapshot shape: for an option listing, whether an option
combination's price is absolute or a difference. A single-Item listing whose notice type has a
documented child and whose notice satisfies that child's schema is **sendable**.

Seller-controlled identities are deterministic and stable. The listing's provider management code
is the ``smartstore-seller-management-code/v1`` projection of the listing identity (architect
ruling R1, Issue #89 comment ``5861607665``): the first 30 lowercase hexadecimal characters of
``SHA-256("smartstore-seller-management-code/v1\\0" + listing_identity)``, because the provider
documents a 30-character bound on ``sellerManagementCode`` while the internal
``listing_identity/v1`` is 37 characters. It is an ICBM decision, not a NAVER fact, it never
replaces ``listing_identity`` locally (ADR-0014 §7 is unchanged), and it is the code compared
exactly on read-back and on a later positive reconcile. An option unit's code stays its
``registration_item_key`` verbatim: the evidence proves no length or charset bound for
``sellerManagerCode``, so no truncation or hashing rule is invented for it.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Any, Final

from app.stages.register.detail import DetailPlan, DetailPlanError, UploadedProviderAsset
from app.stages.register.detail import plan_of as _plan_of
from app.stages.register.detail import render as _render_detail
from app.stages.register.model import ListingShape
from app.stages.register.sanitize import safe_provider_reference
from integrations.marketplaces.smartstore.notice_schema import (
    NoticeFieldSchema,
    NoticePresence,
    NoticeValueType,
    emittable,
    load_notice_schema,
)

# v3: the document carries smartstoreChannelProduct with the owned display status (5915900049 D1).
# v4: the owned naverShoppingRegistration, registration stockQuantity and notice child (D2).
# v5: the notice child of every documented type, with typed values, from the provider notice schema
# (notice coverage S3).
# v6: B-DETAIL — a plan Snapshot's detailContent is rendered by the trusted REGISTER renderer from
# the frozen plan and the Snapshot's uploaded provider asset identities; its detail images are
# placed there and never in the gallery. A BODY-only Snapshot projects exactly as under v5.
WIRE_ENCODING_VERSION: Final = "smartstore-register-wire/v6"

# Architect ruling R1: the provider projection of the internal listing identity.
SELLER_MANAGEMENT_CODE_PROJECTION: Final = "smartstore-seller-management-code/v1"
# The provider documents at most 30 characters for sellerManagementCode (OFFICIAL_SUPPORT).
SELLER_MANAGEMENT_CODE_LENGTH: Final = 30

# Documented numeric bounds and defaults of the 원상품 정보 구조체.
MAX_SALE_PRICE: Final = 999_999_990
MAX_STOCK_QUANTITY: Final = 99_999_999
# Ordinary combination options expose up to three option-name dimensions; the fourth is the
# branch/location-specific form and is not adopted.
MAX_OPTION_DIMENSIONS: Final = 3
# One representative image plus at most nine optional images.
MAX_OPTIONAL_IMAGES: Final = 9
MAX_IMAGES: Final = MAX_OPTIONAL_IMAGES + 1

# Provider field names the captured evidence proves, and the only ones this module may spell.
FIELD_ORIGIN_PRODUCT: Final = "originProduct"
FIELD_CHANNEL_PRODUCT: Final = "smartstoreChannelProduct"
FIELD_NAME: Final = "name"
FIELD_DETAIL: Final = "detailContent"
FIELD_IMAGES: Final = "images"
FIELD_REPRESENTATIVE_IMAGE: Final = "representativeImage"
FIELD_OPTIONAL_IMAGES: Final = "optionalImages"
FIELD_URL: Final = "url"
FIELD_SALE_PRICE: Final = "salePrice"
FIELD_STOCK_QUANTITY: Final = "stockQuantity"
FIELD_LEAF_CATEGORY_ID: Final = "leafCategoryId"
FIELD_DETAIL_ATTRIBUTE: Final = "detailAttribute"
FIELD_SELLER_CODE_INFO: Final = "sellerCodeInfo"
FIELD_SELLER_MANAGEMENT_CODE: Final = "sellerManagementCode"
FIELD_OPTION_INFO: Final = "optionInfo"
FIELD_OPTION_GROUP_NAMES: Final = "optionCombinationGroupNames"
FIELD_OPTION_COMBINATIONS: Final = "optionCombinations"
FIELD_OPTION_SELLER_CODE: Final = "sellerManagerCode"
FIELD_NOTICE: Final = "productInfoProvidedNotice"
FIELD_NOTICE_TYPE: Final = "productInfoProvidedNoticeType"
# The channel structure's own required fields. Their wire values are captured — a JSON boolean
# (E1) and the two display-status write values — and ICBM owns the value of each (5915900049 D1,
# D2.1).
FIELD_NAVER_SHOPPING_REGISTRATION: Final = "naverShoppingRegistration"
NAVER_SHOPPING_REGISTRATION_VALUES: Final = (True, False)
# D2.1: ICBM's publication intent, not an assertion that the account is a NAVER Shopping
# advertiser (the provider stores false for a non-advertiser).
REGISTRATION_NAVER_SHOPPING_REGISTRATION: Final = True
FIELD_CHANNEL_DISPLAY_STATUS: Final = "channelProductDisplayStatusType"
CHANNEL_DISPLAY_STATUS_WRITE_VALUES: Final = ("ON", "SUSPENSION")
# The ICBM first-vertical publication decision (architect resolution 5915900049 D1): a registered
# listing is displayed. It is not a claim that NAVER documents buyer visibility or read timing.
REGISTRATION_DISPLAY_STATUS: Final = "ON"
# Required. On registration the CREATE endpoint accepts only SALE (E2, packet 5868542027): the
# broader shared-schema values are update or read states, never a CREATE input.
FIELD_STATUS_TYPE: Final = "statusType"
CREATE_STATUS_TYPE: Final = "SALE"
# D2.2: the registration seed, required on registration (at least 1, packet 5862400626). It is not
# a supplier quantity, and it is compared exactly on read-back.
REGISTRATION_STOCK_QUANTITY: Final = 1

# The numbered option-name keys of the combination form.
_GROUP_NAME_KEYS: Final = ("optionGroupName1", "optionGroupName2", "optionGroupName3")
_OPTION_NAME_KEYS: Final = ("optionName1", "optionName2", "optionName3")

# The gaps the captured official evidence, or the absence of an ICBM-owned value, leaves open. Each
# names the exact path it blocks; none is ever filled with a default, a guess or an ICBM preference.
GAP_NOTICE_TYPE_CHILD: Final = (
    f"{FIELD_ORIGIN_PRODUCT}.{FIELD_DETAIL_ATTRIBUTE}.{FIELD_NOTICE}: required on registration, but"
    " the reviewed notice type has no documented request child in the provider notice schema, so"
    " no notice is emitted and none is ever guessed or taken from another type"
)
GAP_OPTION_PRICE_SEMANTICS: Final = (
    f"{FIELD_OPTION_COMBINATIONS}[].price: whether an option combination price is absolute or a"
    " difference from salePrice is not captured, so neither the value nor the documented default"
    " may be relied on"
)


# ----------------------------------------------------- the product-information notice (D2.3)
#
# ``originProduct.detailAttribute.productInfoProvidedNotice`` is a required
# ``productInfoProvidedNoticeType`` and exactly one sibling child object carrying that type's
# fields. The provider notice schema owns both: the type → child mapping and every child's fields,
# value types, presence rules and length bounds (notice coverage S1, derived from the retained
# 2.90.0 reference and live capture). A deprecated field, and a field whose wire type the sources
# disagree on, is never emitted.
NOTICE_SCHEMA_REVISION: Final = load_notice_schema().revision

# Every type the schema documents a child for. The enum values LODGMENT_RESERVATION,
# TRAVEL_PACKAGE, AIRLINE_TICKET and RENT_CAR carry no documented child and are absent, never
# derived from their names.
NOTICE_TYPE_MEMBERS: Final[Mapping[str, str]] = MappingProxyType(
    {notice_type: schema.child for notice_type, schema in load_notice_schema().types.items()}
)
_YEAR_MONTH: Final = re.compile(r"\A[0-9]{4}-(0[1-9]|1[0-2])\Z")
_DATE: Final = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_INTEGER_BOUNDS: Final[Mapping[NoticeValueType, int]] = MappingProxyType(
    {NoticeValueType.INTEGER: 2**31, NoticeValueType.LONG: 2**63}
)


def _emittable_fields(notice_type: str) -> Mapping[str, NoticeFieldSchema] | None:
    schema = load_notice_schema().types.get(notice_type)
    if schema is None:
        return None
    return {f.name: f for f in schema.fields if emittable(f)}


class WireContractError(ValueError):
    """The Snapshot cannot be projected onto the adopted provider contract."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def seller_management_code(listing_identity: str) -> str:
    """The provider ``sellerManagementCode`` of one listing identity (ruling R1).

    Deterministic, versioned and stable for the listing identity, and short enough for the
    provider's documented 30-character bound. It is ICBM's own correlation projection: the provider
    guarantees no uniqueness for this field, so an exact match is never by itself a proof
    (ADR-0014 §17.2, §28.2, M5-31).
    """
    if not isinstance(listing_identity, str) or not listing_identity:
        raise WireContractError("WIRE_LISTING_IDENTITY_MISSING", "the Snapshot has no identity")
    material = SELLER_MANAGEMENT_CODE_PROJECTION.encode("utf-8") + b"\0"
    digest = hashlib.sha256(material + listing_identity.encode("utf-8")).hexdigest()
    return digest[:SELLER_MANAGEMENT_CODE_LENGTH]


@dataclass(frozen=True)
class SellerCodes:
    """The seller-controlled identities of one provider listing, derived from stable identities
    only — never from a product name, an option label, a price or an order position."""

    seller_management_code: str
    option_codes: tuple[str, ...]
    # The internal identity the provider code projects. It stays ICBM's local spine (ADR-0014 §7).
    listing_identity: str
    projection_version: str = SELLER_MANAGEMENT_CODE_PROJECTION

    def canonical(self) -> dict[str, Any]:
        return {
            "projection_version": self.projection_version,
            "listing_identity": self.listing_identity,
            FIELD_SELLER_MANAGEMENT_CODE: self.seller_management_code,
            FIELD_OPTION_SELLER_CODE: list(self.option_codes),
        }


# ----------------------------------------------------- the adopted CREATE request schema
#
# The request-side twin of the endpoint's retained-response profile (ADR-0014 §15), and equally
# deny-by-default: only the paths the captured official evidence records may exist in a request
# document, each with the value type and the bound that evidence gives it. A document carrying
# anything else is refused here, never trimmed into shape — the wire boundary has to be able to
# trust that what it encodes is this projection's output over an immutable Snapshot and nothing
# else.
#
# ``smartstoreChannelProduct`` carries exactly its two required members, each with the one value
# ICBM owns (5915900049 D1, D2.1); no optional channel member is owned, so none may appear.
# ``windowChannelProduct`` is out of scope and never appears.
_DOCUMENT_KEYS: Final = frozenset({FIELD_ORIGIN_PRODUCT, FIELD_CHANNEL_PRODUCT})
_CHANNEL_KEYS: Final = frozenset({FIELD_CHANNEL_DISPLAY_STATUS, FIELD_NAVER_SHOPPING_REGISTRATION})
_ORIGIN_KEYS: Final = frozenset(
    {
        FIELD_STATUS_TYPE,
        FIELD_NAME,
        FIELD_DETAIL,
        FIELD_IMAGES,
        FIELD_SALE_PRICE,
        FIELD_STOCK_QUANTITY,
        FIELD_LEAF_CATEGORY_ID,
        FIELD_DETAIL_ATTRIBUTE,
    }
)
_IMAGES_KEYS: Final = frozenset({FIELD_REPRESENTATIVE_IMAGE, FIELD_OPTIONAL_IMAGES})
_IMAGE_KEYS: Final = frozenset({FIELD_URL})
_DETAIL_ATTRIBUTE_KEYS: Final = frozenset({FIELD_SELLER_CODE_INFO, FIELD_OPTION_INFO, FIELD_NOTICE})
_SELLER_CODE_KEYS: Final = frozenset({FIELD_SELLER_MANAGEMENT_CODE})
_OPTION_INFO_KEYS: Final = frozenset({FIELD_OPTION_GROUP_NAMES, FIELD_OPTION_COMBINATIONS})


def _object(value: Any, path: str, allowed: frozenset[str]) -> Mapping[str, Any]:
    """One request object whose every key is on this path's allow-list."""
    if not isinstance(value, Mapping):
        raise WireContractError("WIRE_DOCUMENT_MALFORMED", f"{path} is not an object")
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise WireContractError("WIRE_DOCUMENT_FIELD_UNKNOWN", f"{path}.{unknown[0]}")
    return value


def _required(node: Mapping[str, Any], path: str, names: Sequence[str]) -> None:
    missing = [name for name in names if name not in node]
    if missing:
        raise WireContractError("WIRE_DOCUMENT_FIELD_MISSING", f"{path}.{missing[0]}")


def _string(value: Any, path: str, *, limit: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WireContractError("WIRE_DOCUMENT_VALUE_INVALID", f"{path} is not text")
    if limit is not None and len(value) > limit:
        raise WireContractError("WIRE_DOCUMENT_VALUE_INVALID", f"{path} exceeds {limit}")
    return value


def _bounded_int(value: Any, path: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise WireContractError("WIRE_DOCUMENT_VALUE_INVALID", f"{path} is not within {maximum}")
    return value


def _array(value: Any, path: str, *, maximum: int | None = None) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes) or not value:
        raise WireContractError("WIRE_DOCUMENT_MALFORMED", f"{path} is not a non-empty array")
    if maximum is not None and len(value) > maximum:
        raise WireContractError("WIRE_DOCUMENT_VALUE_INVALID", f"{path} exceeds {maximum} entries")
    return value


def _validate_image(value: Any, path: str) -> None:
    image = _object(value, path, _IMAGE_KEYS)
    _required(image, path, (FIELD_URL,))
    reference = _string(image[FIELD_URL], f"{path}.{FIELD_URL}")
    # The same rule the Snapshot was frozen under: an image the provider does not already hold, or
    # a reference carrying signed material, is never encodable (ADR-0014 §3 B2).
    if not safe_provider_reference(reference):
        raise WireContractError("WIRE_IMAGE_REFERENCE_UNSAFE", f"{path}.{FIELD_URL}")


def _validate_images(value: Any, path: str) -> None:
    images = _object(value, path, _IMAGES_KEYS)
    _required(images, path, (FIELD_REPRESENTATIVE_IMAGE,))
    _validate_image(images[FIELD_REPRESENTATIVE_IMAGE], f"{path}.{FIELD_REPRESENTATIVE_IMAGE}")
    if FIELD_OPTIONAL_IMAGES in images:
        optional_path = f"{path}.{FIELD_OPTIONAL_IMAGES}"
        entries = _array(images[FIELD_OPTIONAL_IMAGES], optional_path, maximum=MAX_OPTIONAL_IMAGES)
        for index, entry in enumerate(entries):
            _validate_image(entry, f"{optional_path}[{index}]")


def _validate_option_info(value: Any, path: str) -> None:
    """The combination form, and only it: the evidence gives the keys of no other option form.

    The option-name dimensions are the numbered keys ``optionGroupName1..n``, from 1 and without a
    hole, at most the three an ordinary combination option allows; every combination row carries
    exactly those same numbered ``optionName`` keys plus its own seller code.
    """
    info = _object(value, path, _OPTION_INFO_KEYS)
    _required(info, path, (FIELD_OPTION_GROUP_NAMES, FIELD_OPTION_COMBINATIONS))
    group_path = f"{path}.{FIELD_OPTION_GROUP_NAMES}"
    groups = _object(info[FIELD_OPTION_GROUP_NAMES], group_path, frozenset(_GROUP_NAME_KEYS))
    dimensions = len(groups)
    if dimensions == 0:
        # A combination form with no option-name dimension is no supported option form: fail closed.
        raise WireContractError(
            "WIRE_OPTION_DIMENSIONS_MISSING", f"{group_path} names no option dimension"
        )
    if set(groups) != set(_GROUP_NAME_KEYS[:dimensions]):
        raise WireContractError("WIRE_DOCUMENT_MALFORMED", f"{group_path} is not numbered from 1")
    for key in _GROUP_NAME_KEYS[:dimensions]:
        _string(groups[key], f"{group_path}.{key}")
    names = frozenset(_OPTION_NAME_KEYS[:dimensions])
    row_path = f"{path}.{FIELD_OPTION_COMBINATIONS}"
    codes: list[str] = []
    for index, entry in enumerate(_array(info[FIELD_OPTION_COMBINATIONS], row_path)):
        where = f"{row_path}[{index}]"
        row = _object(entry, where, names | {FIELD_OPTION_SELLER_CODE})
        _required(row, where, (*sorted(names), FIELD_OPTION_SELLER_CODE))
        for key in names:
            _string(row[key], f"{where}.{key}")
        codes.append(_string(row[FIELD_OPTION_SELLER_CODE], f"{where}.{FIELD_OPTION_SELLER_CODE}"))
    if len(set(codes)) != len(codes):
        raise WireContractError("WIRE_ITEM_CODES_NOT_DISTINCT", f"{row_path}: a code repeats")


def _notice_value(member: NoticeFieldSchema, value: Any, path: str) -> None:
    """One field value of its documented type, bound and form — validated, never reformatted."""
    value_type = member.value_type
    if value_type in (NoticeValueType.TEXT, NoticeValueType.YEAR_MONTH, NoticeValueType.DATE):
        text = _string(value, path, limit=member.max_length)
        if value_type is NoticeValueType.YEAR_MONTH and not _YEAR_MONTH.match(text):
            raise WireContractError("WIRE_DOCUMENT_VALUE_INVALID", f"{path} is not yyyy-MM")
        if value_type is NoticeValueType.DATE:
            try:
                valid = bool(_DATE.match(text)) and date.fromisoformat(text) is not None
            except ValueError:
                valid = False
            if not valid:
                raise WireContractError("WIRE_DOCUMENT_VALUE_INVALID", f"{path} is not yyyy-MM-dd")
        return
    if value_type is NoticeValueType.BOOLEAN:
        if not isinstance(value, bool):
            raise WireContractError("WIRE_DOCUMENT_VALUE_INVALID", f"{path} is not a boolean")
        return
    bound = _INTEGER_BOUNDS.get(value_type) if value_type is not None else None
    if bound is None or isinstance(value, bool) or not isinstance(value, int):
        raise WireContractError("WIRE_DOCUMENT_VALUE_INVALID", f"{path} is not an integer")
    if not -bound <= value < bound:
        raise WireContractError("WIRE_DOCUMENT_VALUE_INVALID", f"{path} is out of range")


def _validate_notice(value: Any, path: str) -> None:
    """The notice of the provider notice schema: a documented type, exactly its own child, only
    that child's emittable fields with values of their documented type, bound and form, and every
    field the schema requires present — the ones the provider fills when omitted excepted."""
    notice = _object(value, path, frozenset({FIELD_NOTICE_TYPE, *NOTICE_TYPE_MEMBERS.values()}))
    _required(notice, path, (FIELD_NOTICE_TYPE,))
    notice_type = notice[FIELD_NOTICE_TYPE]
    members = _emittable_fields(notice_type) if isinstance(notice_type, str) else None
    if members is None:
        raise WireContractError(
            "WIRE_NOTICE_TYPE_NOT_CAPTURED", f"{path}.{FIELD_NOTICE_TYPE} has no documented child"
        )
    child_key = NOTICE_TYPE_MEMBERS[notice_type]
    if set(notice) != {FIELD_NOTICE_TYPE, child_key}:
        # Exactly the child of this type: another type's child, or a second one, is never sent.
        raise WireContractError("WIRE_NOTICE_CHILD_MISMATCH", f"{path} is not exactly {child_key}")
    child_path = f"{path}.{child_key}"
    child = _object(notice[child_key], child_path, frozenset(members))
    for name, field_value in child.items():
        _notice_value(members[name], field_value, f"{child_path}.{name}")
    for name, member in members.items():
        if name in child:
            continue
        missing = member.presence is NoticePresence.REQUIRED
        if member.presence is NoticePresence.REQUIRED_WITHOUT:
            # A condition naming only fields that are never emitted leaves this field required.
            missing = not any(other in child for other in member.required_without)
        if member.presence is NoticePresence.ONE_OF and name == min(
            n for n in member.one_of if n in members
        ):
            missing = not any(other in child for other in member.one_of)
        if missing:
            raise WireContractError("WIRE_DOCUMENT_FIELD_MISSING", f"{child_path}.{name}")


def _validate_document(body: Mapping[str, Any], listing_identity: str) -> None:
    """Check one projected request body against the adopted CREATE request contract.

    Deny-by-default over the whole document, plus the provenance the wire boundary must be able to
    trust: the body's ``sellerManagementCode`` has to be the
    ``smartstore-seller-management-code/v1`` projection of the listing identity the document claims,
    so a body that did not come from this Snapshot's projection cannot be handed on as if it had.
    """
    document = _object(body, "document", _DOCUMENT_KEYS)
    _required(document, "document", (FIELD_ORIGIN_PRODUCT, FIELD_CHANNEL_PRODUCT))
    channel = _object(document[FIELD_CHANNEL_PRODUCT], FIELD_CHANNEL_PRODUCT, _CHANNEL_KEYS)
    _required(channel, FIELD_CHANNEL_PRODUCT, sorted(_CHANNEL_KEYS))
    if channel[FIELD_NAVER_SHOPPING_REGISTRATION] is not REGISTRATION_NAVER_SHOPPING_REGISTRATION:
        # A JSON boolean (E1), and exactly the one ICBM publishes with (D2.1).
        raise WireContractError(
            "WIRE_DOCUMENT_VALUE_INVALID",
            f"{FIELD_CHANNEL_PRODUCT}.{FIELD_NAVER_SHOPPING_REGISTRATION} is not true",
        )
    if channel[FIELD_CHANNEL_DISPLAY_STATUS] != REGISTRATION_DISPLAY_STATUS:
        # One of the two captured write values, and exactly the one ICBM registers with (D1).
        raise WireContractError(
            "WIRE_DOCUMENT_VALUE_INVALID",
            f"{FIELD_CHANNEL_PRODUCT}.{FIELD_CHANNEL_DISPLAY_STATUS} is not"
            f" {REGISTRATION_DISPLAY_STATUS}",
        )
    origin = _object(document[FIELD_ORIGIN_PRODUCT], FIELD_ORIGIN_PRODUCT, _ORIGIN_KEYS)
    _required(origin, FIELD_ORIGIN_PRODUCT, sorted(_ORIGIN_KEYS))
    if origin[FIELD_STATUS_TYPE] != CREATE_STATUS_TYPE:
        # E2: on registration only SALE may be entered; any other value is not a CREATE input.
        raise WireContractError(
            "WIRE_DOCUMENT_VALUE_INVALID",
            f"{FIELD_ORIGIN_PRODUCT}.{FIELD_STATUS_TYPE} is not {CREATE_STATUS_TYPE}",
        )
    _string(origin[FIELD_NAME], f"{FIELD_ORIGIN_PRODUCT}.{FIELD_NAME}")
    _string(origin[FIELD_DETAIL], f"{FIELD_ORIGIN_PRODUCT}.{FIELD_DETAIL}")
    _string(origin[FIELD_LEAF_CATEGORY_ID], f"{FIELD_ORIGIN_PRODUCT}.{FIELD_LEAF_CATEGORY_ID}")
    _bounded_int(
        origin[FIELD_SALE_PRICE], f"{FIELD_ORIGIN_PRODUCT}.{FIELD_SALE_PRICE}", MAX_SALE_PRICE
    )
    stock_path = f"{FIELD_ORIGIN_PRODUCT}.{FIELD_STOCK_QUANTITY}"
    if _bounded_int(origin[FIELD_STOCK_QUANTITY], stock_path, MAX_STOCK_QUANTITY) != (
        REGISTRATION_STOCK_QUANTITY
    ):
        # The registration seed and nothing else (D2.2): no source quantity is ever sent.
        raise WireContractError(
            "WIRE_DOCUMENT_VALUE_INVALID", f"{stock_path} is not {REGISTRATION_STOCK_QUANTITY}"
        )
    _validate_images(origin[FIELD_IMAGES], f"{FIELD_ORIGIN_PRODUCT}.{FIELD_IMAGES}")
    attribute_path = f"{FIELD_ORIGIN_PRODUCT}.{FIELD_DETAIL_ATTRIBUTE}"
    attribute = _object(origin[FIELD_DETAIL_ATTRIBUTE], attribute_path, _DETAIL_ATTRIBUTE_KEYS)
    _required(attribute, attribute_path, (FIELD_SELLER_CODE_INFO,))
    seller_path = f"{attribute_path}.{FIELD_SELLER_CODE_INFO}"
    seller = _object(attribute[FIELD_SELLER_CODE_INFO], seller_path, _SELLER_CODE_KEYS)
    _required(seller, seller_path, (FIELD_SELLER_MANAGEMENT_CODE,))
    code = _string(
        seller[FIELD_SELLER_MANAGEMENT_CODE],
        f"{seller_path}.{FIELD_SELLER_MANAGEMENT_CODE}",
        limit=SELLER_MANAGEMENT_CODE_LENGTH,
    )
    if code != seller_management_code(listing_identity):
        raise WireContractError(
            "WIRE_DOCUMENT_NOT_THIS_SNAPSHOT",
            "the request does not carry this listing identity's management code",
        )
    if FIELD_OPTION_INFO in attribute:
        _validate_option_info(attribute[FIELD_OPTION_INFO], f"{attribute_path}.{FIELD_OPTION_INFO}")
    if FIELD_NOTICE in attribute:
        _validate_notice(attribute[FIELD_NOTICE], f"{attribute_path}.{FIELD_NOTICE}")


@dataclass(frozen=True)
class CreateDocument:
    """One CREATE request body, validated against the adopted contract and frozen with its
    provenance.

    It exists only as the output of :func:`project` over an immutable Snapshot payload, and it
    holds that body as its canonical JSON text — so there is nothing left to mutate between the
    projection and the send, and the durable digest, the evidence and the wire bytes are all the
    same document (ADR-0014 §15, B4). :meth:`mapping` hands out a fresh copy every time;
    :meth:`encoded` is the exact request body. The caller accepts this type and nothing else, so an
    unvalidated mapping, or one changed after the Snapshot was projected, can never be sent.
    """

    encoding_version: str
    listing_identity: str
    canonical_json: str

    def mapping(self) -> dict[str, Any]:
        """A fresh plain copy of the request body; mutating it cannot reach the wire."""
        return dict(json.loads(self.canonical_json))

    def encoded(self) -> bytes:
        """The exact request bytes: canonical UTF-8 JSON, a function of this document alone."""
        return self.canonical_json.encode("utf-8")


def create_document(listing_identity: str, body: Mapping[str, Any]) -> CreateDocument:
    """Validate one projected request body against the adopted contract and freeze it."""
    _validate_document(body, listing_identity)
    try:
        canonical = json.dumps(
            dict(body), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise WireContractError(
            "WIRE_DOCUMENT_MALFORMED", "the request is not JSON-encodable"
        ) from exc
    return CreateDocument(
        encoding_version=WIRE_ENCODING_VERSION,
        listing_identity=listing_identity,
        canonical_json=canonical,
    )


def verified(document: object) -> CreateDocument:
    """The same document, re-proven to be a validated projection output — or a refusal.

    :class:`CreateDocument` is a plain frozen dataclass, so its constructor alone proves nothing: a
    document built directly, outside :func:`project`, could carry any JSON. The wire
    boundary therefore re-runs the whole adopted-contract validation over the document's own body
    and identity and requires the canonical text to be exactly what :func:`create_document` would
    freeze. Only a document that survives that is ever encoded onto the wire.
    """
    if not isinstance(document, CreateDocument):
        raise WireContractError("WIRE_DOCUMENT_NOT_FROZEN", "the request is not a CREATE document")
    if document.encoding_version != WIRE_ENCODING_VERSION:
        raise WireContractError("WIRE_DOCUMENT_VERSION", document.encoding_version)
    try:
        body = json.loads(document.canonical_json)
    except (TypeError, ValueError) as exc:
        raise WireContractError("WIRE_DOCUMENT_MALFORMED", "the request is not JSON") from exc
    if not isinstance(body, dict):
        raise WireContractError("WIRE_DOCUMENT_MALFORMED", "the request is not an object")
    again = create_document(document.listing_identity, body)
    if again != document:
        raise WireContractError(
            "WIRE_DOCUMENT_NOT_CANONICAL", "the request text is not its validated canonical form"
        )
    return document


# The request parts the provider requires on registration, each with the named gap that stands while
# it is absent from a document (packets 5861477977, 5861933729, 5862400626, 5868542027). They are
# read from the document body itself, so completeness never depends on who built the document.
# The schema itself requires the channel's two members and the registration stock quantity, so the
# notice is the one required part a validated document can still lack.
_REQUIRED_ON_REGISTRATION: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    ((FIELD_ORIGIN_PRODUCT, FIELD_DETAIL_ATTRIBUTE, FIELD_NOTICE), GAP_NOTICE_TYPE_CHILD),
)


def completeness_gaps(document: CreateDocument) -> tuple[str, ...]:
    """The provider-required registration parts a validated document does not carry.

    Computed from the document body alone — never from a projection's or a caller's claim — so the
    wire boundary can refuse an incomplete CREATE whoever built it. The channel members and the
    registration stock quantity are owned (5915900049 D1, D2) and required by the schema itself;
    the notice is absent from a document whose reviewed type has no captured child.
    """
    body = document.mapping()
    gaps: list[str] = []
    for path, gap in _REQUIRED_ON_REGISTRATION:
        node: Any = body
        for key in path:
            node = node.get(key) if isinstance(node, Mapping) else None
        if node is None:
            gaps.append(gap)
    return tuple(gaps)


@dataclass(frozen=True)
class WireProjection:
    """The adopted CREATE request of one frozen Snapshot, plus every gap that keeps it unsendable.

    ``document`` is the validated, frozen :class:`CreateDocument` holding exactly the structures the
    captured official evidence supports and the Snapshot owns; ``gaps`` names every part the
    evidence does not carry or no ICBM owner decides. ``gaps`` is empty only when the whole required
    request is projectable, and only then is the document ``sendable``.
    """

    encoding_version: str
    listing_shape: ListingShape
    codes: SellerCodes
    document: CreateDocument
    image_references: tuple[str, ...]
    gaps: tuple[str, ...]
    # The reviewed notice the Snapshot owns. It is emitted as the child of its type when that type
    # has a documented child (D2.3), and kept here either way as the evidence it was projected from.
    notice_type: str | None = None
    notice_fields: Mapping[str, Any] = field(default_factory=dict)

    @property
    def sendable(self) -> bool:
        """Whether this request may be handed to the adopted CREATE endpoint at all."""
        return not self.gaps


def _items(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The Snapshot's Items, or a refusal. A payload that is not a registration payload is a
    broken caller, never a half-projected request: it fails as a wire-contract error like every
    other refusal, so the sender can turn it into one local, transmission-precluded refusal."""
    if not isinstance(payload, Mapping):
        raise WireContractError("WIRE_PAYLOAD_MALFORMED", "the payload is not a mapping")
    items = payload.get("items")
    if not isinstance(items, Sequence) or isinstance(items, str) or not items:
        raise WireContractError("WIRE_PAYLOAD_MALFORMED", "the payload carries no Item")
    if not all(isinstance(item, Mapping) for item in items):
        raise WireContractError("WIRE_PAYLOAD_MALFORMED", "an Item is not a mapping")
    return tuple(items)


def seller_codes(payload: Mapping[str, Any]) -> SellerCodes:
    """The listing's provider management code and its option codes, in Snapshot Item order."""
    items = _items(payload)
    identity = payload.get("listing_identity")
    if not isinstance(identity, str) or not identity:
        raise WireContractError("WIRE_LISTING_IDENTITY_MISSING", "the Snapshot has no identity")
    keys = [item.get("registration_item_key") for item in items]
    if not all(isinstance(key, str) and key for key in keys):
        raise WireContractError("WIRE_PAYLOAD_MALFORMED", "an Item carries no key")
    codes = tuple(str(key) for key in keys)
    if len(set(codes)) != len(codes):
        raise WireContractError("WIRE_ITEM_CODES_NOT_DISTINCT", "two Items share a code")
    return SellerCodes(
        seller_management_code=seller_management_code(identity),
        option_codes=codes,
        listing_identity=identity,
    )


def _text(value: object, path: str) -> str:
    """One outbound text value of the Snapshot. ``상세페이지 참조`` is a notice/attribute
    representation; a listing name that claims it is a broken Snapshot, not a value."""
    if not isinstance(value, Mapping) or value.get("detail_page_reference"):
        raise WireContractError("WIRE_VALUE_NOT_TEXT", f"{path} carries no outbound text")
    text = value.get("value")
    if not isinstance(text, str) or not text.strip():
        raise WireContractError("WIRE_VALUE_NOT_TEXT", f"{path} is empty")
    return text


# B-DETAIL: the renderer's refusals, by the wire code each one is.
_DETAIL_WIRE_CODES: Final = MappingProxyType(
    {
        "DETAIL_PLAN_MALFORMED": "WIRE_DETAIL_PLAN_MALFORMED",
        "DETAIL_RENDERER_UNKNOWN": "WIRE_DETAIL_RENDERER_UNKNOWN",
        "DETAIL_IMAGE_NOT_UPLOADED": "WIRE_IMAGE_NOT_PREPARED",
        "DETAIL_ASSET_REFERENCE_UNSAFE": "WIRE_IMAGE_REFERENCE_UNSAFE",
        "DETAIL_CONTENT_EMPTY": "WIRE_DETAIL_CONTENT_MISSING",
    }
)


def _detail_plan(payload: Mapping[str, Any]) -> DetailPlan | None:
    """The frozen B-DETAIL plan of a ``registration-payload/v2`` Snapshot, or ``None`` for a
    BODY-only one. A v2 detail that is not exactly a plan is refused, never read as a body."""
    detail = payload.get("detail")
    if not isinstance(detail, Mapping) or "renderer" not in detail:
        return None
    try:
        return _plan_of(detail)
    except DetailPlanError as refused:
        raise WireContractError(_DETAIL_WIRE_CODES[refused.code], refused.detail) from refused


def _uploaded_detail_assets(
    items: Sequence[Mapping[str, Any]], plan: DetailPlan
) -> dict[tuple[str, str, str], UploadedProviderAsset]:
    """The trusted rendering path's only input besides the plan: the uploaded provider identity of
    each detail image the Snapshot froze. The plan's images and the Snapshot's ``DETAIL`` assets
    must be the same set; a detail image without an uploaded identity is not encodable."""
    uploaded: dict[tuple[str, str, str], UploadedProviderAsset] = {}
    for item in items:
        for asset in item.get("publication_assets") or ():
            if not isinstance(asset, Mapping) or asset.get("role") != "DETAIL":
                continue
            key = (
                str(asset.get("asset_kind")),
                str(asset.get("sha256")),
                str(asset.get("derivation_id") or ""),
            )
            reference = asset.get("provider_asset_ref")
            if reference is None:
                raise WireContractError(
                    "WIRE_IMAGE_NOT_PREPARED", "a detail image has no provider reference yet"
                )
            try:
                uploaded[key] = UploadedProviderAsset(reference)
            except DetailPlanError as refused:
                raise WireContractError("WIRE_IMAGE_REFERENCE_UNSAFE", refused.detail) from refused
    if set(uploaded) != {image.key for image in plan.images}:
        raise WireContractError(
            "WIRE_DETAIL_PLAN_MISMATCH", "the plan's images are not the Snapshot's detail images"
        )
    return uploaded


def _detail_content(payload: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> str:
    plan = _detail_plan(payload)
    if plan is not None:
        try:
            return _render_detail(plan, _uploaded_detail_assets(items, plan))
        except DetailPlanError as refused:
            raise WireContractError(_DETAIL_WIRE_CODES[refused.code], refused.detail) from refused
    # A BODY-only Snapshot (content v1): its frozen body, exactly as before B-DETAIL.
    detail = payload.get("detail")
    body = detail.get("body") if isinstance(detail, Mapping) else None
    if not isinstance(body, str) or not body.strip():
        raise WireContractError("WIRE_DETAIL_CONTENT_MISSING", "detailContent is required")
    return body


def _leaf_category_id(payload: Mapping[str, Any]) -> str:
    """The category the Snapshot froze.

    The CREATE endpoint requires ``leafCategoryId`` on registration (packet ``5862400626``, which
    overrides the generic schema badge). The value is the operator-reviewed category id the
    Snapshot froze, emitted verbatim; a Snapshot without one is refused, never given a guess.
    """
    category = payload.get("category")
    category_id = category.get("category_id") if isinstance(category, Mapping) else None
    if not isinstance(category_id, str) or not category_id.strip():
        raise WireContractError("WIRE_CATEGORY_MISSING", "the Snapshot froze no category")
    return category_id


def _sale_price(items: Sequence[Mapping[str, Any]]) -> int:
    values = [item.get("sale_price_krw") for item in items]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        raise WireContractError("WIRE_PAYLOAD_MALFORMED", "an Item carries no pinned price")
    prices = {int(value) for value in values}  # type: ignore[arg-type]
    if len(prices) != 1:
        # The evidence proves an option price exists and its bound, never whether a combination
        # price is absolute or a difference. Differing Item prices are therefore not encodable.
        raise WireContractError(
            "WIRE_OPTION_PRICE_SEMANTICS_UNPROVEN",
            "the Items of one listing carry different prices",
        )
    price = prices.pop()
    if price <= 0 or price > MAX_SALE_PRICE:
        raise WireContractError("WIRE_SALE_PRICE_OUT_OF_RANGE", f"salePrice {price}")
    return price


def _image_references(
    items: Sequence[Mapping[str, Any]], *, detail_placed: bool = False
) -> tuple[str, ...]:
    """The provider references of the listing's gallery images, representative first,
    deduplicated in Item and position order. Every one must already be a prepared, sanitized
    provider reference: an image the provider does not yet hold is not encodable (ADR-0014 §3 B2).
    A detail image is never a gallery image: it is placed by a plan (``detail_placed``) or
    refused."""
    representative: str | None = None
    others: list[str] = []
    for item in items:
        assets = item.get("publication_assets")
        if not isinstance(assets, Sequence) or isinstance(assets, str):
            raise WireContractError("WIRE_PAYLOAD_MALFORMED", "an Item carries no assets")
        for asset in assets:
            if not isinstance(asset, Mapping):
                raise WireContractError("WIRE_PAYLOAD_MALFORMED", "an asset is not a mapping")
            reference = asset.get("provider_asset_ref")
            if reference is None:
                raise WireContractError(
                    "WIRE_IMAGE_NOT_PREPARED", "an image has no provider reference yet"
                )
            if not isinstance(reference, str) or not safe_provider_reference(reference):
                raise WireContractError("WIRE_IMAGE_REFERENCE_UNSAFE", "unsafe image reference")
            role = asset.get("role")
            if role == "DETAIL" and detail_placed:
                # B-DETAIL: the plan places it in detailContent (_detail_content).
                continue
            if role == "DETAIL":
                # A detail-body image is never a gallery image (Issue #219 §2.2); without a plan
                # that places it there is no place for it in this request.
                raise WireContractError(
                    "WIRE_DETAIL_IMAGE_NOT_PLACEABLE", "a detail-body image is not a gallery image"
                )
            if role == "REPRESENTATIVE" and representative is None:
                representative = reference
            elif reference not in others:
                others.append(reference)
    if representative is None:
        raise WireContractError("WIRE_REPRESENTATIVE_IMAGE_MISSING", "no representative image")
    references = (representative, *(r for r in others if r != representative))
    if len(references) > MAX_IMAGES:
        raise WireContractError(
            "WIRE_IMAGE_COUNT_EXCEEDED", f"{len(references)} images exceed {MAX_IMAGES}"
        )
    return references


def _images(references: Sequence[str]) -> dict[str, Any]:
    """The adopted ``images`` structure: one representative image and the optional images.

    Every URL is one the product-image upload API returned and the Snapshot froze; a supplier
    hotlink can never be here, because the payload builder refuses one (M5-19).
    """
    optional = list(references[1:])
    images: dict[str, Any] = {FIELD_REPRESENTATIVE_IMAGE: {FIELD_URL: references[0]}}
    if optional:
        images[FIELD_OPTIONAL_IMAGES] = [{FIELD_URL: reference} for reference in optional]
    return images


def _notice(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The reviewed notice the Snapshot owns: its type and the fields the operator supplied.

    Only fields the Snapshot carries are read, each under the key the reviewed metadata named and
    with its own JSON type — text, a boolean or an integer. Nothing is added to "complete" the
    notice, and a field the operator left to the product detail stays out: the provider fills those
    with "상품상세 참조" itself, and conditional fields are omitted when they do not apply.

    The provider requires a notice for registration, so a Snapshot that owns none is refused here.
    Projecting it is :func:`_notice_document`'s question.
    """
    notice = payload.get("notice")
    if not isinstance(notice, Mapping):
        raise WireContractError("WIRE_NOTICE_MISSING", "productInfoProvidedNotice is required")
    notice_type = notice.get("notice_type")
    if not isinstance(notice_type, str) or not notice_type.strip():
        raise WireContractError("WIRE_NOTICE_MISSING", "the notice names no reviewed type")
    fields = notice.get("fields")
    if not isinstance(fields, Mapping) or not fields:
        raise WireContractError("WIRE_NOTICE_MISSING", "the notice carries no reviewed field")
    if not all(isinstance(key, str) and key.strip() for key in fields):
        raise WireContractError("WIRE_VALUE_NOT_TEXT", "a notice field name is not text")
    reviewed: dict[str, Any] = {}
    for key, value in sorted(fields.items()):
        if not isinstance(value, Mapping):
            raise WireContractError("WIRE_VALUE_NOT_TEXT", f"notice.{key}")
        if value.get("detail_page_reference"):
            # "미입력 시 상품상세 참조": the value is left out, never filled with a placeholder.
            continue
        raw = value.get("value")
        if isinstance(raw, bool | int):
            reviewed[key] = raw
            continue
        reviewed[key] = _text(value, f"notice.{key}")
    if not reviewed:
        raise WireContractError("WIRE_NOTICE_MISSING", "every notice field was left to the detail")
    return notice_type, reviewed


def _notice_document(notice_type: str, fields: Mapping[str, Any]) -> dict[str, Any] | None:
    """The notice child of the reviewed type, or ``None`` when the type has no documented child.

    The type is looked up in the provider notice schema exactly as reviewed — never cased, trimmed
    or matched loosely — and only the child of that type is built, from the Snapshot's own reviewed
    values under their reviewed field names. A value the operator left to the product detail is
    already out (:func:`_notice`). Unknown or never-emitted fields, values of the wrong type, bound
    or form, and missing required fields are all refused by :func:`create_document`.
    """
    if notice_type not in NOTICE_TYPE_MEMBERS:
        return None
    return {FIELD_NOTICE_TYPE: notice_type, NOTICE_TYPE_MEMBERS[notice_type]: dict(fields)}


def _option_dimensions(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """The option-name dimensions of a multi-Item listing: the same dimensions for every Item, at
    most the three an ordinary combination option allows. The Snapshot's option values are display
    values; the identity is the seller code."""
    for item in items:
        options = item.get("options") or {}
        if not isinstance(options, Mapping):
            raise WireContractError("WIRE_PAYLOAD_MALFORMED", "an Item's options are not a mapping")
        for name, value in options.items():
            # Option names and values are the operator's authored text, carried verbatim: a
            # non-text or empty one is a broken Snapshot, never coerced into a display value.
            if not isinstance(name, str) or not name.strip():
                raise WireContractError("WIRE_VALUE_NOT_TEXT", "an option name is not text")
            if not isinstance(value, str) or not value.strip():
                raise WireContractError("WIRE_VALUE_NOT_TEXT", f"option {name} is not text")
    dimensions = {tuple(sorted(item.get("options") or {})) for item in items}
    if len(dimensions) != 1:
        raise WireContractError(
            "WIRE_OPTION_DIMENSIONS_INCONSISTENT", "the Items declare different option dimensions"
        )
    names = dimensions.pop()
    if not names:
        raise WireContractError(
            "WIRE_OPTION_DIMENSIONS_MISSING", "a multi-Item listing needs option dimensions"
        )
    if len(names) > MAX_OPTION_DIMENSIONS:
        raise WireContractError(
            "WIRE_OPTION_DIMENSIONS_EXCEEDED", f"{len(names)} > {MAX_OPTION_DIMENSIONS}"
        )
    return names


def _option_info(
    items: Sequence[Mapping[str, Any]], codes: Sequence[str], dimensions: Sequence[str]
) -> dict[str, Any]:
    """The adopted combination-form ``optionInfo``.

    Only the combination form is projected: the captured evidence gives the keys of the simple,
    custom and standard option structures for no form but this one, and only an option shape the
    canonical ICBM contracts already allow may be projected at all. Every documented default is
    left to the provider — ``usable`` (``true``), ``stockQuantity`` (0) and ``price`` (0) are not
    emitted, because the Snapshot owns no value for them; the price default is additionally
    blocked by :data:`GAP_OPTION_PRICE_SEMANTICS`.
    """
    group_names = dict(zip(_GROUP_NAME_KEYS, dimensions, strict=False))
    combinations: list[dict[str, Any]] = []
    for item, code in zip(items, codes, strict=True):
        options = item.get("options") or {}
        row: dict[str, Any] = {
            key: options[name] for key, name in zip(_OPTION_NAME_KEYS, dimensions, strict=False)
        }
        row[FIELD_OPTION_SELLER_CODE] = code
        combinations.append(row)
    return {FIELD_OPTION_GROUP_NAMES: group_names, FIELD_OPTION_COMBINATIONS: combinations}


def project(payload: Mapping[str, Any]) -> WireProjection:
    """Project one frozen Snapshot payload onto the adopted SmartStore CREATE request.

    Raises :class:`WireContractError` when the Snapshot itself violates a documented provider rule
    or owns no value the provider requires, and records a *gap* where the official evidence simply
    does not carry what the request would need. A projection with any gap is not ``sendable``.
    """
    items = _items(payload)
    try:
        shape = ListingShape(str(payload.get("listing_shape")))
    except ValueError as exc:
        raise WireContractError(
            "WIRE_PAYLOAD_MALFORMED", "the payload names no listing shape"
        ) from exc
    codes = seller_codes(payload)
    plan = _detail_plan(payload)
    references = _image_references(items, detail_placed=plan is not None and plan.places_images)
    notice_type, notice_fields = _notice(payload)
    gaps: list[str] = []

    detail_attribute: dict[str, Any] = {
        FIELD_SELLER_CODE_INFO: {FIELD_SELLER_MANAGEMENT_CODE: codes.seller_management_code},
    }
    # D2.3: the child of the reviewed notice type, or a named gap — never another type's child.
    notice = _notice_document(notice_type, notice_fields)
    if notice is None:
        gaps.append(GAP_NOTICE_TYPE_CHILD)
    else:
        detail_attribute[FIELD_NOTICE] = notice
    if len(items) > 1:
        dimensions = _option_dimensions(items)
        detail_attribute[FIELD_OPTION_INFO] = _option_info(items, codes.option_codes, dimensions)
        gaps.append(GAP_OPTION_PRICE_SEMANTICS)
    origin_product: dict[str, Any] = {
        # E2: the only status the CREATE endpoint accepts on registration.
        FIELD_STATUS_TYPE: CREATE_STATUS_TYPE,
        FIELD_NAME: _text(payload.get("name"), "name"),
        FIELD_DETAIL: _detail_content(payload, items),
        FIELD_IMAGES: _images(references),
        FIELD_SALE_PRICE: _sale_price(items),
        # D2.2: the registration seed, never a source quantity.
        FIELD_STOCK_QUANTITY: REGISTRATION_STOCK_QUANTITY,
        FIELD_LEAF_CATEGORY_ID: _leaf_category_id(payload),
        FIELD_DETAIL_ATTRIBUTE: detail_attribute,
    }
    # smartstoreChannelProduct: both required members carry the value ICBM owns (5915900049 D1,
    # D2.1); every optional member (channelProductName, bbsSeq, storeKeepExclusiveProduct) is
    # unowned and never emitted. windowChannelProduct is out of scope and is never emitted.
    channel_product: dict[str, Any] = {
        FIELD_CHANNEL_DISPLAY_STATUS: REGISTRATION_DISPLAY_STATUS,
        FIELD_NAVER_SHOPPING_REGISTRATION: REGISTRATION_NAVER_SHOPPING_REGISTRATION,
    }
    # Validated and frozen here, at the one place a request document is ever built: what leaves
    # this function is already checked against the adopted contract and can no longer change.
    document = create_document(
        codes.listing_identity,
        {FIELD_ORIGIN_PRODUCT: origin_product, FIELD_CHANNEL_PRODUCT: channel_product},
    )
    return WireProjection(
        encoding_version=WIRE_ENCODING_VERSION,
        listing_shape=shape,
        codes=codes,
        document=document,
        image_references=references,
        gaps=tuple(gaps),
        notice_type=notice_type,
        notice_fields=notice_fields,
    )
