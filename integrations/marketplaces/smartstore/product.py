"""The SmartStore product wire contract of a frozen RegistrationSnapshot (M5 PR-D).

The input is only the PR-C canonical payload — the immutable Snapshot's own outbound values. This
module never re-prices, never re-reads a source fact, never invents a category, a notice or an
option, and never consults the current Draft (ADR-0014 §6, §11; kickoff §3).

**What the 2.89.0 packet proves, and therefore what this module encodes** (Issue #89 comment
5746489554, 원상품 정보 구조체): the field names ``name``, ``detailContent``, ``images``,
``salePrice``, ``stockQuantity``, ``sellerCodeInfo.sellerManagementCode``, the option-combination
field ``sellerManagerCode`` and ``productInfoProvidedNotice``; that a representative image plus at
most nine optional images are allowed; that ``salePrice`` and an option price are at most
999,999,990 and a stock quantity at most 99,999,999; that ordinary combination options carry at
most three option-name dimensions; and that the notice is required but its fields are
category-specific, with conditional fields omitted rather than filled.

**What it does not prove, and therefore what this module refuses to assemble**: the container
shape of ``images``, the option-combination container and its option-name/value field names, and
the request media type of ``POST /v2/products``. A complete CREATE document cannot be built from
proven names alone, so :func:`project` returns the proven projection and the named gaps, and
:func:`seller_codes` fixes the identities the projection and the read-back comparison share.
Nothing here is sent: ``SMARTSTORE_PRODUCT_CREATE_V2`` stays NOT_ADOPTED (``registry.py``).

Seller-controlled identities are deterministic and stable: the listing's management code is the
listing identity, and an option unit's code is its ``registration_item_key``. Both are already
short ASCII identities, so they travel verbatim — no truncation and no hashing rule is invented,
because the packet proves no length or charset bound for either field (kickoff §7, §8).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.register.model import ListingShape
from app.register.sanitize import safe_provider_reference

WIRE_ENCODING_VERSION: Final = "smartstore-register-wire/v1"

# Proven numeric bounds of the 원상품 정보 구조체 (packet 5746489554).
MAX_SALE_PRICE: Final = 999_999_990
MAX_STOCK_QUANTITY: Final = 99_999_999
MAX_OPTION_DIMENSIONS: Final = 3
# A representative image and at most nine optional images.
MAX_IMAGES: Final = 10

# Proven provider field names this module is allowed to spell.
FIELD_NAME: Final = "name"
FIELD_DETAIL: Final = "detailContent"
FIELD_IMAGES: Final = "images"
FIELD_SALE_PRICE: Final = "salePrice"
FIELD_SELLER_CODE_INFO: Final = "sellerCodeInfo"
FIELD_SELLER_MANAGEMENT_CODE: Final = "sellerManagementCode"
FIELD_OPTION_SELLER_CODE: Final = "sellerManagerCode"
FIELD_NOTICE: Final = "productInfoProvidedNotice"


class WireContractError(ValueError):
    """The Snapshot cannot be projected onto the proven provider contract."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SellerCodes:
    """The seller-controlled identities of one provider listing, derived from stable identities
    only — never from a product name, an option label, a price or an order position."""

    seller_management_code: str
    option_codes: tuple[str, ...]

    def canonical(self) -> dict[str, Any]:
        return {
            FIELD_SELLER_MANAGEMENT_CODE: self.seller_management_code,
            FIELD_OPTION_SELLER_CODE: list(self.option_codes),
        }


@dataclass(frozen=True)
class WireProjection:
    """The part of a CREATE document the packet lets us state, plus the gaps that keep the whole
    document unsendable. ``gaps`` is empty only when every required part is proven."""

    encoding_version: str
    listing_shape: ListingShape
    codes: SellerCodes
    proven: dict[str, Any]
    image_references: tuple[str, ...]
    gaps: tuple[str, ...]

    @property
    def sendable(self) -> bool:
        """Always ``False`` while any gap remains; a CREATE is never sent from PR-D either way."""
        return not self.gaps


def seller_codes(payload: Mapping[str, Any]) -> SellerCodes:
    """The listing's management code and its option codes, in Snapshot Item order."""
    identity = payload["listing_identity"]
    items = payload["items"]
    if not isinstance(identity, str) or not identity:
        raise WireContractError("WIRE_LISTING_IDENTITY_MISSING", "the Snapshot has no identity")
    codes = tuple(str(item["registration_item_key"]) for item in items)
    if len(set(codes)) != len(codes):
        raise WireContractError("WIRE_ITEM_CODES_NOT_DISTINCT", "two Items share a code")
    return SellerCodes(seller_management_code=identity, option_codes=codes)


def _text(value: object, field: str) -> str:
    """One outbound text value of the Snapshot. ``상세페이지 참조`` is a notice/attribute
    representation; a listing name that claims it is a broken Snapshot, not a value."""
    if not isinstance(value, Mapping) or value.get("detail_page_reference"):
        raise WireContractError("WIRE_VALUE_NOT_TEXT", f"{field} carries no outbound text")
    text = value.get("value")
    if not isinstance(text, str) or not text.strip():
        raise WireContractError("WIRE_VALUE_NOT_TEXT", f"{field} is empty")
    return text


def _sale_price(items: Sequence[Mapping[str, Any]]) -> int:
    prices = {int(item["sale_price_krw"]) for item in items}
    if len(prices) != 1:
        # The packet proves an option price exists and its bound, never whether a combination
        # price is absolute or a difference. Differing Item prices are therefore not encodable.
        raise WireContractError(
            "WIRE_OPTION_PRICE_SEMANTICS_UNPROVEN",
            "the Items of one listing carry different prices",
        )
    price = prices.pop()
    if price <= 0 or price > MAX_SALE_PRICE:
        raise WireContractError("WIRE_SALE_PRICE_OUT_OF_RANGE", f"salePrice {price}")
    return price


def _images(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """The provider references of the listing's images, representative first, deduplicated in
    Item and position order. Every one must already be a prepared, sanitized provider reference:
    an image the provider does not yet hold is not encodable (ADR-0014 §3 B2)."""
    representative: str | None = None
    others: list[str] = []
    for item in items:
        for asset in item["publication_assets"]:
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


def _notice(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The notice exactly as the reviewed provider metadata and the operator supplied it.

    Only fields the Snapshot carries are emitted, each under the key the reviewed metadata named.
    Nothing is added to "complete" the notice, and a field the operator left out stays out: the
    packet states the notice fields are category-specific, that a value may be left to the product
    detail, and that conditional fields are omitted when they do not apply (kickoff §11)."""
    notice = payload.get("notice")
    if not isinstance(notice, Mapping):
        raise WireContractError("WIRE_NOTICE_MISSING", "productInfoProvidedNotice is required")
    fields = notice.get("fields")
    if not isinstance(fields, Mapping) or not fields:
        raise WireContractError("WIRE_NOTICE_MISSING", "the notice carries no reviewed field")
    emitted: dict[str, Any] = {}
    for key, value in sorted(fields.items()):
        if not isinstance(value, Mapping):
            raise WireContractError("WIRE_VALUE_NOT_TEXT", f"notice.{key}")
        if value.get("detail_page_reference"):
            # "미입력 시 상품상세 참조": the value is left out, never filled with a placeholder.
            continue
        emitted[key] = _text(value, f"notice.{key}")
    if not emitted:
        raise WireContractError("WIRE_NOTICE_MISSING", "every notice field was left to the detail")
    return emitted


def _option_dimensions(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """The option-name dimensions of a multi-Item listing: the same dimensions for every Item, at
    most the three an ordinary combination option allows (the fourth is branch-specific and is not
    adopted). The Snapshot's option values are display values; the identity is the seller code."""
    dimensions = {tuple(sorted(item.get("options", {}))) for item in items}
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


def project(payload: Mapping[str, Any]) -> WireProjection:
    """The proven projection of one frozen Snapshot payload onto the SmartStore product contract.

    Raises :class:`WireContractError` when the Snapshot itself violates a proven provider rule, and
    records a *gap* when the provider contract is simply not proven enough to assemble that part.
    """
    items = payload["items"]
    shape = ListingShape(payload["listing_shape"])
    codes = seller_codes(payload)
    proven: dict[str, Any] = {
        FIELD_NAME: _text(payload["name"], "name"),
        FIELD_DETAIL: payload["detail"]["body"],
        FIELD_SALE_PRICE: _sale_price(items),
        FIELD_SELLER_CODE_INFO: {FIELD_SELLER_MANAGEMENT_CODE: codes.seller_management_code},
        FIELD_NOTICE: _notice(payload),
    }
    references = _images(items)
    gaps: list[str] = [
        # The image URLs are known and sanitized; the container they sit in is not proven.
        f"{FIELD_IMAGES}: the packet proves images.*.url but not the container shape",
        "request media type of POST /v2/products is not proven",
    ]
    if len(items) > 1:
        _option_dimensions(items)
        gaps.append(
            "option combinations: only sellerManagerCode and the ≤3 dimension bound are proven,"
            " not the combination container or its option-name/value fields"
        )
    return WireProjection(
        encoding_version=WIRE_ENCODING_VERSION,
        listing_shape=shape,
        codes=codes,
        proven=proven,
        image_references=references,
        gaps=tuple(gaps),
    )
