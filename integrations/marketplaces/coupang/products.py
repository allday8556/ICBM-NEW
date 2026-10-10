"""Provider-zero contracts for Coupang's 22 Products endpoints.

The client prepares exact documented method/path/query/body combinations, signs them through the
C-AUTH-1 owner, and hands them only to the in-memory fake transport.  Seller product, Coupang
product and vendor item identities are distinct types.  This module grants neither a socket nor
LIVE mutation authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import NewType
from urllib.parse import quote, urlencode

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Market,
    Method,
    RequestTarget,
)
from integrations.marketplaces.coupang.fake_transport import FakeRequest, FakeTransport

SellerProductId = NewType("SellerProductId", int)
ProductId = NewType("ProductId", int)
VendorItemId = NewType("VendorItemId", int)

_API = "/v2/providers/seller_api/apis/api/v1/marketplace"
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CoupangProductContractError(ValueError):
    """The local request or retained fake response violates the official endpoint contract."""


class ProductEndpoint(StrEnum):
    SELLER_AUTO_OPTION_ENABLE = "SELLER_AUTO_OPTION_ENABLE"
    ITEM_AUTO_OPTION_ENABLE = "ITEM_AUTO_OPTION_ENABLE"
    ITEM_ORIGINAL_PRICE_UPDATE = "ITEM_ORIGINAL_PRICE_UPDATE"
    ITEM_PRICE_UPDATE = "ITEM_PRICE_UPDATE"
    ITEM_QUANTITY_UPDATE = "ITEM_QUANTITY_UPDATE"
    PRODUCT_DELETE = "PRODUCT_DELETE"
    SELLER_AUTO_OPTION_DISABLE = "SELLER_AUTO_OPTION_DISABLE"
    ITEM_AUTO_OPTION_DISABLE = "ITEM_AUTO_OPTION_DISABLE"
    PRODUCT_REPLACE = "PRODUCT_REPLACE"
    PRODUCT_CREATE = "PRODUCT_CREATE"
    PRODUCT_LIST = "PRODUCT_LIST"
    PRODUCT_LIST_TIMEFRAME = "PRODUCT_LIST_TIMEFRAME"
    PRODUCT_PARTIAL_UPDATE = "PRODUCT_PARTIAL_UPDATE"
    PRODUCT_HISTORY = "PRODUCT_HISTORY"
    PRODUCT_INFLOW_STATUS = "PRODUCT_INFLOW_STATUS"
    PRODUCT_BY_EXTERNAL_SKU = "PRODUCT_BY_EXTERNAL_SKU"
    ITEM_INVENTORY = "ITEM_INVENTORY"
    PRODUCT_READ = "PRODUCT_READ"
    PRODUCT_PARTIAL_READ = "PRODUCT_PARTIAL_READ"
    PRODUCT_APPROVAL = "PRODUCT_APPROVAL"
    ITEM_SALES_RESUME = "ITEM_SALES_RESUME"
    ITEM_SALES_STOP = "ITEM_SALES_STOP"


@dataclass(frozen=True)
class FrozenDocument:
    """Canonical JSON object; no field can be added between validation and fake transport."""

    body: bytes = field(repr=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FrozenDocument:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError) as exc:
            raise CoupangProductContractError("product document must be canonical JSON") from exc
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):
            raise CoupangProductContractError("product document must be an object")
        return cls(encoded)

    def value(self) -> dict[str, object]:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True)
class ProductResult:
    endpoint: ProductEndpoint
    code: str
    message: str
    document: FrozenDocument
    next_token: str | None = None

    def data(self) -> object:
        return self.document.value().get("data")


@dataclass(frozen=True)
class DeleteEligibility:
    """Caller-owned evidence for the two documented product-delete preconditions."""

    all_items_stopped: bool
    awaiting_approval: bool

    def require_deletable(self) -> None:
        if not self.all_items_stopped:
            raise CoupangProductContractError("all product items must be stopped before deletion")
        if self.awaiting_approval:
            raise CoupangProductContractError("a product awaiting approval cannot be deleted")


@dataclass(frozen=True)
class ProductListQuery:
    next_token: int | None = None
    max_per_page: int = 10
    seller_product_id: SellerProductId | None = None
    seller_product_name: str | None = None
    status: str | None = None
    manufacture: str | None = None
    created_at: str | None = None
    violation_types: tuple[str, ...] = ()
    violation_type_and_or: str | None = None

    def __post_init__(self) -> None:
        if self.next_token is not None and self.next_token < 1:
            raise CoupangProductContractError("nextToken must be positive")
        if not 1 <= self.max_per_page <= 100:
            raise CoupangProductContractError("maxPerPage must be in 1..100")
        if self.seller_product_name is not None and len(self.seller_product_name) > 20:
            raise CoupangProductContractError("sellerProductName must be at most 20 characters")
        if self.created_at is not None and _DATE.fullmatch(self.created_at) is None:
            raise CoupangProductContractError("createdAt must use yyyy-MM-dd")
        if len(self.violation_types) >= 2 and self.violation_type_and_or not in {"AND", "OR"}:
            raise CoupangProductContractError(
                "violationTypeAndOr is required for two or more violation types"
            )


_CREATE_REQUIRED = frozenset(
    {
        "displayCategoryCode",
        "sellerProductName",
        "vendorId",
        "saleStartedAt",
        "saleEndedAt",
        "deliveryMethod",
        "deliveryCompanyCode",
        "deliveryChargeType",
        "deliveryCharge",
        "freeShipOverAmount",
        "deliveryChargeOnReturn",
        "remoteAreaDeliverable",
        "unionDeliveryType",
        "returnCenterCode",
        "returnChargeName",
        "companyContactNumber",
        "returnZipCode",
        "returnAddress",
        "returnAddressDetail",
        "returnCharge",
        "outboundShippingPlaceCode",
        "vendorUserId",
        "requested",
        "items",
    }
)
_ITEM_REQUIRED = frozenset(
    {
        "itemName",
        "originalPrice",
        "salePrice",
        "maximumBuyCount",
        "maximumBuyForPerson",
        "maximumBuyForPersonPeriod",
        "outboundShippingTimeDay",
        "unitCount",
        "adultOnly",
        "taxType",
        "parallelImported",
        "overseasPurchased",
        "pccNeeded",
        "images",
        "attributes",
        "contents",
    }
)
_PARTIAL_FIELDS = frozenset(
    {
        "sellerProductId",
        "companyContactNumber",
        "deliveryCharge",
        "deliveryChargeOnReturn",
        "deliveryChargeType",
        "deliveryCompanyCode",
        "deliveryMethod",
        "extraInfoMessage",
        "freeShipOverAmount",
        "outboundShippingPlaceCode",
        "outboundShippingTimeDay",
        "remoteAreaDeliverable",
        "returnAddress",
        "returnAddressDetail",
        "returnCenterCode",
        "returnCharge",
        "returnChargeName",
        "returnZipCode",
        "unionDeliveryType",
    }
)


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CoupangProductContractError(f"{name} must be a positive integer")
    return value


def _nonnegative(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CoupangProductContractError(f"{name} must be a non-negative integer")
    return value


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CoupangProductContractError(f"{name} must be non-empty")
    return value


def _validate_create(document: FrozenDocument, vendor_id: str) -> None:
    value = document.value()
    missing = sorted(_CREATE_REQUIRED - value.keys())
    if missing:
        raise CoupangProductContractError(f"product document is missing: {', '.join(missing)}")
    if value.get("vendorId") != vendor_id:
        raise CoupangProductContractError("product vendorId must match the signing vendor")
    name = value.get("sellerProductName")
    if not isinstance(name, str) or not name or len(name) > 100:
        raise CoupangProductContractError("sellerProductName must be 1..100 characters")
    if not isinstance(value.get("displayCategoryCode"), int):
        raise CoupangProductContractError("displayCategoryCode must be an integer")
    if not isinstance(value.get("requested"), bool):
        raise CoupangProductContractError("requested must be a boolean")
    for field_name in ("saleStartedAt", "saleEndedAt"):
        date = value.get(field_name)
        if not isinstance(date, str) or _DATE_TIME.fullmatch(date) is None:
            raise CoupangProductContractError(f"{field_name} must use yyyy-MM-ddTHH:mm:ss")
    items = value.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise CoupangProductContractError("items must contain 1..200 product items")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise CoupangProductContractError(f"items[{index}] must be an object")
        missing_item = sorted(_ITEM_REQUIRED - item.keys())
        if missing_item:
            raise CoupangProductContractError(
                f"items[{index}] is missing: {', '.join(missing_item)}"
            )
        if not isinstance(item.get("attributes"), list) or not item["attributes"]:
            raise CoupangProductContractError(f"items[{index}].attributes must not be empty")
        if not isinstance(item.get("images"), list) or not item["images"]:
            raise CoupangProductContractError(f"items[{index}].images must not be empty")
        unit_count = item.get("unitCount")
        if not isinstance(unit_count, int) or isinstance(unit_count, bool) or unit_count < 1:
            raise CoupangProductContractError(
                "unitCount is per-listing unit count and must be positive"
            )


def _query(pairs: Sequence[tuple[str, object]]) -> str:
    encoded: list[tuple[str, str]] = []
    for key, value in pairs:
        if value is None:
            continue
        if isinstance(value, bool):
            encoded.append((key, str(value).lower()))
        else:
            encoded.append((key, str(value)))
    return urlencode(encoded)


class CoupangProductClient:
    """All Products endpoints, executable only against ``FakeTransport``."""

    def __init__(
        self,
        authenticator: Authenticator,
        context: AuthContext,
        transport: FakeTransport,
    ) -> None:
        self._authenticator = authenticator
        self._context = context
        self._transport = transport

    def _call(
        self,
        endpoint: ProductEndpoint,
        method: Method,
        path: str,
        *,
        query: str = "",
        document: FrozenDocument | None = None,
        accepted_codes: tuple[object, ...] = ("SUCCESS", "200", 200),
    ) -> ProductResult:
        target = RequestTarget(method, path, query)
        response = self._transport.send(
            FakeRequest(
                target,
                self._authenticator.headers(self._context, target),
                b"" if document is None else document.body,
            )
        )
        if response.status_code != 200 or not isinstance(response.body, dict):
            raise CoupangProductContractError(f"Coupang HTTP {response.status_code}")
        body = response.body
        code = body.get("code")
        if code not in accepted_codes:
            raise CoupangProductContractError("Coupang product response was not successful")
        message = body.get("message", "")
        if not isinstance(message, str):
            raise CoupangProductContractError("Coupang product response message is invalid")
        retained = FrozenDocument.from_mapping(body)
        token = body.get("nextToken")
        if token is not None and not isinstance(token, str | int):
            raise CoupangProductContractError("nextToken must be a string or integer")
        return ProductResult(
            endpoint, str(code), message, retained, None if token is None else str(token)
        )

    def create(self, document: FrozenDocument) -> tuple[SellerProductId, ProductResult]:
        _validate_create(document, self._context.credentials.vendor_id)
        result = self._call(
            ProductEndpoint.PRODUCT_CREATE,
            Method.POST,
            f"{_API}/seller-products",
            document=document,
        )
        data = result.data()
        if isinstance(data, dict) and data.get("code") == "SUCCESS":
            data = data.get("data")
        return SellerProductId(_positive(data, "sellerProductId")), result

    def replace(self, document: FrozenDocument) -> ProductResult:
        _validate_create(document, self._context.credentials.vendor_id)
        _positive(document.value().get("sellerProductId"), "sellerProductId")
        return self._call(
            ProductEndpoint.PRODUCT_REPLACE,
            Method.PUT,
            f"{_API}/seller-products",
            document=document,
        )

    def partial_update(
        self, seller_product_id: SellerProductId, document: FrozenDocument
    ) -> ProductResult:
        identity = _positive(seller_product_id, "sellerProductId")
        value = document.value()
        if set(value) - _PARTIAL_FIELDS:
            raise CoupangProductContractError("partial update contains an undocumented field")
        if value.get("sellerProductId") != identity:
            raise CoupangProductContractError("body sellerProductId must match path identity")
        return self._call(
            ProductEndpoint.PRODUCT_PARTIAL_UPDATE,
            Method.PUT,
            f"{_API}/seller-products/{identity}/partial",
            document=document,
        )

    def read(self, seller_product_id: SellerProductId, *, partial: bool = False) -> ProductResult:
        identity = _positive(seller_product_id, "sellerProductId")
        suffix = "/partial" if partial else ""
        endpoint = ProductEndpoint.PRODUCT_PARTIAL_READ if partial else ProductEndpoint.PRODUCT_READ
        return self._call(endpoint, Method.GET, f"{_API}/seller-products/{identity}{suffix}")

    def list(self, request: ProductListQuery | None = None) -> ProductResult:
        request = ProductListQuery() if request is None else request
        pairs: list[tuple[str, object]] = [
            ("vendorId", self._context.credentials.vendor_id),
            ("nextToken", request.next_token),
            ("maxPerPage", request.max_per_page),
            ("sellerProductId", request.seller_product_id),
            ("sellerProductName", request.seller_product_name),
            ("status", request.status),
            ("manufacture", request.manufacture),
            ("createdAt", request.created_at),
        ]
        pairs.extend(("violationTypes", value) for value in request.violation_types)
        pairs.append(("violationTypeAndOr", request.violation_type_and_or))
        return self._call(
            ProductEndpoint.PRODUCT_LIST,
            Method.GET,
            f"{_API}/seller-products",
            query=_query(pairs),
        )

    def list_timeframe(self, created_at_from: str, created_at_to: str) -> ProductResult:
        start = self._official_datetime(created_at_from, "createdAtFrom")
        end = self._official_datetime(created_at_to, "createdAtTo")
        if end < start or (end - start).total_seconds() > 600:
            raise CoupangProductContractError(
                "product timeframe must be ordered and at most 10 minutes"
            )
        return self._call(
            ProductEndpoint.PRODUCT_LIST_TIMEFRAME,
            Method.GET,
            f"{_API}/seller-products/time-frame",
            query=_query(
                [
                    ("vendorId", self._context.credentials.vendor_id),
                    ("createdAtFrom", created_at_from),
                    ("createdAtTo", created_at_to),
                ]
            ),
        )

    @staticmethod
    def _official_datetime(value: str, name: str) -> datetime:
        if _DATE_TIME.fullmatch(value) is None:
            raise CoupangProductContractError(f"{name} must use yyyy-MM-ddTHH:mm:ss")
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")

    def history(
        self,
        seller_product_id: SellerProductId,
        *,
        next_token: int | None = None,
        max_per_page: int = 10,
    ) -> ProductResult:
        identity = _positive(seller_product_id, "sellerProductId")
        if next_token is not None:
            _positive(next_token, "nextToken")
        _positive(max_per_page, "maxPerPage")
        return self._call(
            ProductEndpoint.PRODUCT_HISTORY,
            Method.GET,
            f"{_API}/seller-products/{identity}/histories",
            query=_query([("nextToken", next_token), ("maxPerPage", max_per_page)]),
        )

    def inflow_status(self) -> ProductResult:
        return self._call(
            ProductEndpoint.PRODUCT_INFLOW_STATUS,
            Method.GET,
            f"{_API}/seller-products/inflow-status",
        )

    def by_external_sku(self, external_vendor_sku_code: str) -> ProductResult:
        code = quote(_text(external_vendor_sku_code, "externalVendorSkuCode"), safe="")
        return self._call(
            ProductEndpoint.PRODUCT_BY_EXTERNAL_SKU,
            Method.GET,
            f"{_API}/seller-products/external-vendor-sku-codes/{code}",
        )

    def inventory(self, vendor_item_id: VendorItemId) -> ProductResult:
        identity = _positive(vendor_item_id, "vendorItemId")
        return self._call(
            ProductEndpoint.ITEM_INVENTORY,
            Method.GET,
            f"{_API}/vendor-items/{identity}/inventories",
        )

    def change_price(
        self,
        vendor_item_id: VendorItemId,
        price: int,
        *,
        force: bool = False,
        auto_min_sale_price: int | None = None,
        auto_pricing_active: bool | None = None,
    ) -> ProductResult:
        identity = _positive(vendor_item_id, "vendorItemId")
        value = _positive(price, "price")
        if value % 10:
            raise CoupangProductContractError("price must use the documented 10-won unit")
        if (auto_min_sale_price is None) != (auto_pricing_active is None):
            raise CoupangProductContractError(
                "apMinSalePrice and apActive must be supplied together"
            )
        if auto_min_sale_price is not None and not 0 < auto_min_sale_price < value:
            raise CoupangProductContractError("apMinSalePrice must be positive and less than price")
        return self._call(
            ProductEndpoint.ITEM_PRICE_UPDATE,
            Method.PUT,
            f"{_API}/vendor-items/{identity}/prices/{value}",
            query=_query(
                [
                    ("forceSalePriceUpdate", force),
                    ("apMinSalePrice", auto_min_sale_price),
                    ("apActive", auto_pricing_active),
                ]
            ),
        )

    def change_original_price(
        self, vendor_item_id: VendorItemId, original_price: int
    ) -> ProductResult:
        identity = _positive(vendor_item_id, "vendorItemId")
        price = _nonnegative(original_price, "originalPrice")
        if price % 10:
            raise CoupangProductContractError("originalPrice must use the documented 10-won unit")
        return self._call(
            ProductEndpoint.ITEM_ORIGINAL_PRICE_UPDATE,
            Method.PUT,
            f"{_API}/vendor-items/{identity}/original-prices/{price}",
        )

    def change_quantity(self, vendor_item_id: VendorItemId, quantity: int) -> ProductResult:
        identity = _positive(vendor_item_id, "vendorItemId")
        value = _nonnegative(quantity, "quantity")
        return self._call(
            ProductEndpoint.ITEM_QUANTITY_UPDATE,
            Method.PUT,
            f"{_API}/vendor-items/{identity}/quantities/{value}",
        )

    def delete(
        self, seller_product_id: SellerProductId, *, eligibility: DeleteEligibility
    ) -> ProductResult:
        eligibility.require_deletable()
        identity = _positive(seller_product_id, "sellerProductId")
        return self._call(
            ProductEndpoint.PRODUCT_DELETE,
            Method.DELETE,
            f"{_API}/seller-products/{identity}",
        )

    def request_approval(self, seller_product_id: SellerProductId) -> ProductResult:
        identity = _positive(seller_product_id, "sellerProductId")
        return self._call(
            ProductEndpoint.PRODUCT_APPROVAL,
            Method.PUT,
            f"{_API}/seller-products/{identity}/approvals",
        )

    def resume_sales(self, vendor_item_id: VendorItemId) -> ProductResult:
        return self._item_action(vendor_item_id, ProductEndpoint.ITEM_SALES_RESUME, "sales/resume")

    def stop_sales(self, vendor_item_id: VendorItemId) -> ProductResult:
        return self._item_action(vendor_item_id, ProductEndpoint.ITEM_SALES_STOP, "sales/stop")

    def _item_action(
        self, vendor_item_id: VendorItemId, endpoint: ProductEndpoint, suffix: str
    ) -> ProductResult:
        identity = _positive(vendor_item_id, "vendorItemId")
        return self._call(endpoint, Method.PUT, f"{_API}/vendor-items/{identity}/{suffix}")

    def seller_auto_option(self, *, enabled: bool) -> ProductResult:
        self._require_kr_auto_option()
        endpoint = (
            ProductEndpoint.SELLER_AUTO_OPTION_ENABLE
            if enabled
            else ProductEndpoint.SELLER_AUTO_OPTION_DISABLE
        )
        suffix = "opt-in" if enabled else "opt-out"
        return self._call(
            endpoint,
            Method.POST,
            f"{_API}/seller/auto-generated/{suffix}",
            accepted_codes=("SUCCESS", "PROCESSING"),
        )

    def item_auto_option(self, vendor_item_id: VendorItemId, *, enabled: bool) -> ProductResult:
        self._require_kr_auto_option()
        identity = _positive(vendor_item_id, "vendorItemId")
        endpoint = (
            ProductEndpoint.ITEM_AUTO_OPTION_ENABLE
            if enabled
            else ProductEndpoint.ITEM_AUTO_OPTION_DISABLE
        )
        suffix = "opt-in" if enabled else "opt-out"
        return self._call(
            endpoint,
            Method.POST,
            f"{_API}/vendor-items/{identity}/auto-generated/{suffix}",
            accepted_codes=("SUCCESS", "PROCESSING"),
        )

    def _require_kr_auto_option(self) -> None:
        if self._context.market is not Market.KR:
            raise CoupangProductContractError(
                "auto-generated option APIs are documented for KR only"
            )


__all__ = [
    "CoupangProductClient",
    "CoupangProductContractError",
    "DeleteEligibility",
    "FrozenDocument",
    "ProductEndpoint",
    "ProductId",
    "ProductListQuery",
    "ProductResult",
    "SellerProductId",
    "VendorItemId",
]
