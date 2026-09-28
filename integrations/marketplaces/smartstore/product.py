"""The SmartStore product CREATE wire contract of a frozen RegistrationSnapshot (M5 PR-D; CREATE
adoption slice, ADR-0020 §4 order 1).

The input is only the PR-C canonical payload — the immutable Snapshot's own outbound values. This
module never re-prices, never re-reads a source fact, never invents a category, a notice or an
option, and never consults the current Draft (ADR-0014 §6, §11; kickoff §3).

**The adopted request contract.** ``docs/evidence/marketplace-apis/PRODUCT_CREATE.md`` § SmartStore
records the official 2.89.0 CREATE contract field by field (packet ``5746489554``, reviews
``5768199984`` / ``5768247290``, field packet ``5861477977``, required/conditional packet
``5861933729``). The CREATE adoption slice freezes it here: the request top level is
``originProduct`` plus ``smartstoreChannelProduct``; ``windowChannelProduct`` is a separate Shopping
Window channel structure and is **never emitted**.

**What this module may spell** is exactly what that record captures — the nested request field
names, the provider's globally required fields, the M5-relevant conditional rules and the documented
limits and defaults. Two rules bound every projection:

* a structure that is *not required* is emitted only when the immutable Snapshot owns its values and
  the documented condition applies; the schema containing a structure is never a reason to send it;
* a value the official evidence does not carry — an enumeration, a value type, a notice type
  child — is **never invented**. It is recorded as a named *gap*, the projection is not
  ``sendable``, and the execution owner refuses with ``REGISTER_WIRE_NOT_SENDABLE`` before any
  transport exists.

The gaps that hold at this adoption are the ones the evidence record lists as not captured: the
accepted values of the required ``originProduct.statusType``; the value type of the required
``smartstoreChannelProduct.naverShoppingRegistration``; the publication decision behind the required
``channelProductDisplayStatusType`` (its two write values *are* captured, but no Snapshot owns which
one ICBM publishes with); the type-specific child of ``productInfoProvidedNotice``, whose field set
is captured for no notice type at all; and, for an option listing, whether an option combination's
price is absolute or a difference. Each is fail-closed, never a default.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.register.model import ListingShape
from app.register.sanitize import safe_provider_reference

WIRE_ENCODING_VERSION: Final = "smartstore-register-wire/v2"

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
# The channel structure's own required fields; only the display status' write values are captured.
FIELD_NAVER_SHOPPING_REGISTRATION: Final = "naverShoppingRegistration"
FIELD_CHANNEL_DISPLAY_STATUS: Final = "channelProductDisplayStatusType"
CHANNEL_DISPLAY_STATUS_WRITE_VALUES: Final = ("ON", "SUSPENSION")
# Required, but the accepted values are not captured, so it is never emitted.
FIELD_STATUS_TYPE: Final = "statusType"

# The numbered option-name keys of the combination form.
_GROUP_NAME_KEYS: Final = ("optionGroupName1", "optionGroupName2", "optionGroupName3")
_OPTION_NAME_KEYS: Final = ("optionName1", "optionName2", "optionName3")

# The gaps the captured official evidence leaves open. Each names the exact path it blocks; none is
# ever filled with a default, a guess or an ICBM preference.
GAP_STATUS_TYPE: Final = (
    f"{FIELD_ORIGIN_PRODUCT}.{FIELD_STATUS_TYPE}: required, but its accepted values are not"
    " captured by the official evidence and the Snapshot owns none"
)
GAP_SHOPPING_REGISTRATION: Final = (
    f"{FIELD_CHANNEL_PRODUCT}.{FIELD_NAVER_SHOPPING_REGISTRATION}: required, but its value type is"
    " not captured by the official evidence"
)
GAP_CHANNEL_DISPLAY_STATUS: Final = (
    f"{FIELD_CHANNEL_PRODUCT}.{FIELD_CHANNEL_DISPLAY_STATUS}: required; ON and SUSPENSION are the"
    " captured write values, but no Snapshot owns which one ICBM publishes with"
)
GAP_NOTICE_TYPE_CHILD: Final = (
    f"{FIELD_ORIGIN_PRODUCT}.{FIELD_DETAIL_ATTRIBUTE}.{FIELD_NOTICE}: the type-specific child and"
    " its field set are captured for no productInfoProvidedNoticeType, so the required notice"
    " cannot be projected from the reviewed metadata"
)
GAP_OPTION_PRICE_SEMANTICS: Final = (
    f"{FIELD_OPTION_COMBINATIONS}[].price: whether an option combination price is absolute or a"
    " difference from salePrice is not captured, so neither the value nor the documented default"
    " may be relied on"
)


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


@dataclass(frozen=True)
class WireProjection:
    """The adopted CREATE request of one frozen Snapshot, plus every gap that keeps it unsendable.

    ``document`` holds exactly the structures the captured official evidence supports and the
    Snapshot owns; ``gaps`` names every part the evidence does not carry. ``gaps`` is empty only
    when the whole required request is projectable, and only then is the document ``sendable``.
    """

    encoding_version: str
    listing_shape: ListingShape
    codes: SellerCodes
    document: dict[str, Any]
    image_references: tuple[str, ...]
    gaps: tuple[str, ...]
    # The reviewed notice the Snapshot owns, kept as evidence of what a projectable notice child
    # would be filled from. It is never emitted while GAP_NOTICE_TYPE_CHILD stands.
    notice_type: str | None = None
    notice_fields: Mapping[str, str] = field(default_factory=dict)

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


def _detail_content(payload: Mapping[str, Any]) -> str:
    detail = payload.get("detail")
    body = detail.get("body") if isinstance(detail, Mapping) else None
    if not isinstance(body, str) or not body.strip():
        raise WireContractError("WIRE_DETAIL_CONTENT_MISSING", "detailContent is required")
    return body


def _leaf_category_id(payload: Mapping[str, Any]) -> str:
    """The category the Snapshot froze.

    ICBM-side, never a provider requirement: the 2.89.0 schema does **not** mark
    ``leafCategoryId`` required, so ICBM's own authoring/preflight category policy is never cited
    as provider-required. The value is the operator-reviewed category id, emitted verbatim.
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


def _image_references(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """The provider references of the listing's images, representative first, deduplicated in
    Item and position order. Every one must already be a prepared, sanitized provider reference:
    an image the provider does not yet hold is not encodable (ADR-0014 §3 B2)."""
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
            if asset.get("role") == "REPRESENTATIVE" and representative is None:
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


def _notice(payload: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """The reviewed notice the Snapshot owns: its type and the fields the operator supplied.

    Only fields the Snapshot carries are read, each under the key the reviewed metadata named.
    Nothing is added to "complete" the notice, and a field the operator left to the product detail
    stays out: the evidence states the notice fields are category-specific, that a value may be
    left to the product detail, and that conditional fields are omitted when they do not apply.

    The provider requires a notice for registration, so a Snapshot that owns none is refused here.
    Projecting it is a different question: no ``productInfoProvidedNoticeType`` child is captured,
    so :data:`GAP_NOTICE_TYPE_CHILD` keeps the structure unemitted (and the request unsendable).
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
    reviewed: dict[str, str] = {}
    for key, value in sorted(fields.items()):
        if not isinstance(value, Mapping):
            raise WireContractError("WIRE_VALUE_NOT_TEXT", f"notice.{key}")
        if value.get("detail_page_reference"):
            # "미입력 시 상품상세 참조": the value is left out, never filled with a placeholder.
            continue
        reviewed[str(key)] = _text(value, f"notice.{key}")
    if not reviewed:
        raise WireContractError("WIRE_NOTICE_MISSING", "every notice field was left to the detail")
    return notice_type, reviewed


def _option_dimensions(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """The option-name dimensions of a multi-Item listing: the same dimensions for every Item, at
    most the three an ordinary combination option allows. The Snapshot's option values are display
    values; the identity is the seller code."""
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
            key: str(options[name])
            for key, name in zip(_OPTION_NAME_KEYS, dimensions, strict=False)
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
    references = _image_references(items)
    notice_type, notice_fields = _notice(payload)
    gaps: list[str] = [GAP_STATUS_TYPE, GAP_NOTICE_TYPE_CHILD]

    detail_attribute: dict[str, Any] = {
        FIELD_SELLER_CODE_INFO: {FIELD_SELLER_MANAGEMENT_CODE: codes.seller_management_code},
    }
    if len(items) > 1:
        dimensions = _option_dimensions(items)
        detail_attribute[FIELD_OPTION_INFO] = _option_info(items, codes.option_codes, dimensions)
        gaps.append(GAP_OPTION_PRICE_SEMANTICS)
    origin_product: dict[str, Any] = {
        FIELD_NAME: _text(payload["name"], "name"),
        FIELD_DETAIL: _detail_content(payload),
        FIELD_IMAGES: _images(references),
        FIELD_SALE_PRICE: _sale_price(items),
        FIELD_LEAF_CATEGORY_ID: _leaf_category_id(payload),
        FIELD_DETAIL_ATTRIBUTE: detail_attribute,
    }
    # smartstoreChannelProduct owns no projectable field at this adoption: both of its required
    # fields are gaps, and every optional one (channelProductName, bbsSeq,
    # storeKeepExclusiveProduct) is unowned, so the structure is not emitted at all rather than
    # sent half-built. windowChannelProduct is out of scope and is never emitted.
    gaps.extend((GAP_SHOPPING_REGISTRATION, GAP_CHANNEL_DISPLAY_STATUS))
    document: dict[str, Any] = {FIELD_ORIGIN_PRODUCT: origin_product}
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
