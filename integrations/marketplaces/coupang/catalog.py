"""Provider-zero Coupang category and brand API contracts.

The nine endpoints in this module are the documented KR category/brand family.  Requests are
signed with :mod:`integrations.marketplaces.coupang.auth` and can only be handed to the in-memory
fake transport.  A recommendation is retained as a suggestion; this module has no category
adoption or metadata-current-pointer capability.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.stages.register.category_catalog import ProviderCategory
from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Market,
    Method,
    RequestTarget,
)
from integrations.marketplaces.coupang.fake_transport import FakeRequest, FakeTransport

_SELLER_API = "/v2/providers/seller_api/apis/api/v1/marketplace"
_CATEGORY_PREDICT = "/v2/providers/openapi/apis/api/v1/categorization/predict"
_CATEGORY_CODE = re.compile(r"^[0-9]+$")
_BRAND_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class CoupangCatalogContractError(ValueError):
    """A local request or retained provider response violates the official contract."""


@dataclass(frozen=True)
class CategoryNode:
    category_id: str
    name: str
    status: str
    children: tuple[CategoryNode, ...] = ()


@dataclass(frozen=True)
class CategoryAttribute:
    name: str
    data_type: str
    basic_unit: str | None
    usable_units: tuple[str, ...]
    required: str
    group_number: str
    exposed: str


@dataclass(frozen=True)
class NoticeDetail:
    name: str
    required: str


@dataclass(frozen=True)
class NoticeCategory:
    name: str
    details: tuple[NoticeDetail, ...]


@dataclass(frozen=True)
class RequiredDocument:
    template_name: str
    required: str


@dataclass(frozen=True)
class Certification:
    certification_type: str
    name: str
    data_type: str
    required: str


@dataclass(frozen=True)
class CategoryMetadata:
    allows_single_item: bool
    attributes: tuple[CategoryAttribute, ...]
    notices: tuple[NoticeCategory, ...]
    required_documents: tuple[RequiredDocument, ...]
    certifications: tuple[Certification, ...]
    allowed_offer_conditions: tuple[str, ...]


@dataclass(frozen=True)
class CategoryRecommendation:
    result_type: str
    comment: str
    predicted_category_id: str | None
    predicted_category_name: str | None

    @property
    def provider_suggestion_only(self) -> bool:
        """The provider result never adopts or reviews category metadata."""
        return True


@dataclass(frozen=True)
class Brand:
    brand_id: str
    name: str
    logo_url: str | None = None
    uid_required: bool | None = None
    allowed_uid_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrandSearchPage:
    page: int
    count_per_page: int
    total_count: int
    items: tuple[Brand, ...]


def _text(value: object, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise CoupangCatalogContractError(f"{name} must be a string")
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CoupangCatalogContractError(f"{name} must be an integer")
    return value


def _objects(value: object, name: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CoupangCatalogContractError(f"{name} must be an object array")
    return tuple(value)


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CoupangCatalogContractError(f"{name} must be a string array")
    return tuple(value)


def _data(response: object, success_codes: tuple[object, ...]) -> object:
    if (
        not isinstance(response, dict)
        or response.get("code") not in success_codes
        or "data" not in response
    ):
        raise CoupangCatalogContractError("Coupang response must contain SUCCESS and data")
    return response["data"]


def _category_code(value: int | str) -> str:
    code = str(value)
    if _CATEGORY_CODE.fullmatch(code) is None:
        raise CoupangCatalogContractError("display category code must be numeric")
    return code


def _category(value: object, name: str = "data") -> CategoryNode:
    if not isinstance(value, dict):
        raise CoupangCatalogContractError(f"{name} must be an object")
    raw_children = value.get("child")
    if raw_children is None:
        raw_children = []
    children = tuple(
        _category(child, f"{name}.child") for child in _objects(raw_children, f"{name}.child")
    )
    return CategoryNode(
        str(_integer(value.get("displayItemCategoryCode"), f"{name}.displayItemCategoryCode")),
        _text(value.get("name"), f"{name}.name"),
        _text(value.get("status"), f"{name}.status"),
        children,
    )


def _brand(value: object, name: str = "data") -> Brand:
    if not isinstance(value, dict):
        raise CoupangCatalogContractError(f"{name} must be an object")
    logo = value.get("brandLogoUrl")
    if logo is not None and not isinstance(logo, str):
        raise CoupangCatalogContractError(f"{name}.brandLogoUrl must be a string or null")
    required = value.get("isUIDRequired")
    if required is not None and not isinstance(required, bool):
        raise CoupangCatalogContractError(f"{name}.isUIDRequired must be a boolean")
    uid_types = value.get("allowedUIDTypes", [])
    return Brand(
        _text(value.get("brandId"), f"{name}.brandId"),
        _text(value.get("brandName"), f"{name}.brandName"),
        logo,
        required,
        _strings(uid_types, f"{name}.allowedUIDTypes"),
    )


class CoupangCatalogClient:
    """Signed category/brand calls over the provider-zero fake transport only."""

    def __init__(
        self,
        authenticator: Authenticator,
        context: AuthContext,
        transport: FakeTransport,
    ) -> None:
        if context.market is not Market.KR:
            raise CoupangCatalogContractError("category and brand APIs are documented for KR only")
        self._authenticator = authenticator
        self._context = context
        self._transport = transport

    def _call(
        self,
        method: Method,
        path: str,
        *,
        document: Mapping[str, object] | None = None,
        success_codes: tuple[object, ...] = ("SUCCESS",),
    ) -> object:
        target = RequestTarget(method, path)
        body = (
            b""
            if document is None
            else json.dumps(
                document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )
        response = self._transport.send(
            FakeRequest(target, self._authenticator.headers(self._context, target), body)
        )
        if response.status_code != 200:
            raise CoupangCatalogContractError(f"Coupang HTTP {response.status_code}")
        return _data(response.body, success_codes)

    def category(self, display_category_code: int | str) -> CategoryNode:
        code = _category_code(display_category_code)
        return _category(self._call(Method.GET, f"{_SELLER_API}/meta/display-categories/{code}"))

    def category_tree(self) -> CategoryNode:
        return _category(self._call(Method.GET, f"{_SELLER_API}/meta/display-categories"))

    def category_valid(self, display_category_code: int | str) -> bool:
        code = _category_code(display_category_code)
        data = self._call(Method.GET, f"{_SELLER_API}/meta/display-categories/{code}/status")
        if not isinstance(data, bool):
            raise CoupangCatalogContractError("category validity data must be a boolean")
        return data

    def auto_category_agreed(self) -> bool:
        vendor_id = self._context.credentials.vendor_id
        data = self._call(
            Method.GET, f"{_SELLER_API}/vendors/{vendor_id}/check-auto-category-agreed"
        )
        if not isinstance(data, bool):
            raise CoupangCatalogContractError("auto-category agreement data must be a boolean")
        return data

    def recommend_category(
        self,
        product_name: str,
        *,
        product_description: str | None = None,
        brand: str | None = None,
        attributes: Mapping[str, str] | None = None,
        seller_sku_code: str | None = None,
    ) -> CategoryRecommendation:
        document: dict[str, object] = {"productName": _text(product_name, "productName")}
        for key, value in (
            ("productDescription", product_description),
            ("brand", brand),
            ("sellerSkuCode", seller_sku_code),
        ):
            if value is not None:
                document[key] = _text(value, key)
        if attributes is not None:
            if not all(
                isinstance(k, str) and k and isinstance(v, str) for k, v in attributes.items()
            ):
                raise CoupangCatalogContractError("attributes must be a string mapping")
            document["attributes"] = dict(attributes)
        data = self._call(Method.POST, _CATEGORY_PREDICT, document=document, success_codes=(200,))
        if not isinstance(data, dict):
            raise CoupangCatalogContractError("category recommendation data must be an object")
        result = _text(data.get("autoCategorizationPredictionResultType"), "result type")
        if result not in {"SUCCESS", "FAILURE", "INSUFFICIENT_INFORMATION"}:
            raise CoupangCatalogContractError("unknown category recommendation result type")
        category_id = data.get("predictedCategoryId")
        category_name = data.get("predictedCategoryName")
        if category_id is not None:
            category_id = str(category_id)
        if category_name is not None and not isinstance(category_name, str):
            raise CoupangCatalogContractError("predictedCategoryName must be a string")
        return CategoryRecommendation(
            result,
            _text(data.get("comment", ""), "comment", empty=True),
            category_id,
            category_name,
        )

    def category_metadata(self, display_category_code: int | str) -> CategoryMetadata:
        code = _category_code(display_category_code)
        data = self._call(
            Method.GET,
            f"{_SELLER_API}/meta/category-related-metas/display-category-codes/{code}",
        )
        if not isinstance(data, dict) or not isinstance(data.get("isAllowSingleItem"), bool):
            raise CoupangCatalogContractError("category metadata data is invalid")
        attributes_list: list[CategoryAttribute] = []
        for item in _objects(data.get("attributes", []), "attributes"):
            basic_unit = item.get("basicUnit")
            attributes_list.append(
                CategoryAttribute(
                    _text(item.get("attributeTypeName"), "attributeTypeName"),
                    _text(item.get("dataType"), "dataType"),
                    basic_unit if isinstance(basic_unit, str) else None,
                    _strings(item.get("usableUnits", []), "usableUnits"),
                    _text(item.get("required"), "required"),
                    _text(item.get("groupNumber"), "groupNumber"),
                    _text(item.get("exposed"), "exposed"),
                )
            )
        attributes = tuple(attributes_list)
        notices = tuple(
            NoticeCategory(
                _text(item.get("noticeCategoryName"), "noticeCategoryName"),
                tuple(
                    NoticeDetail(
                        _text(detail.get("noticeCategoryDetailName"), "noticeCategoryDetailName"),
                        _text(detail.get("required"), "required"),
                    )
                    for detail in _objects(
                        item.get("noticeCategoryDetailNames", []), "noticeCategoryDetailNames"
                    )
                ),
            )
            for item in _objects(data.get("noticeCategories", []), "noticeCategories")
        )
        documents = tuple(
            RequiredDocument(
                _text(item.get("templateName"), "templateName"),
                _text(item.get("required"), "required"),
            )
            for item in _objects(data.get("requiredDocumentNames", []), "requiredDocumentNames")
        )
        certifications = tuple(
            Certification(
                _text(item.get("certificationType"), "certificationType"),
                _text(item.get("name"), "name"),
                _text(item.get("dataType"), "dataType"),
                _text(item.get("required"), "required"),
            )
            for item in _objects(data.get("certifications", []), "certifications")
        )
        conditions = _strings(data.get("allowedOfferConditions", []), "allowedOfferConditions")
        return CategoryMetadata(
            data["isAllowSingleItem"],
            attributes,
            notices,
            documents,
            certifications,
            conditions,
        )

    def search_brands(
        self, brand_name: str, *, page: int = 1, count_per_page: int = 10
    ) -> BrandSearchPage:
        if page < 1 or not 1 <= count_per_page <= 10:
            raise CoupangCatalogContractError("brand page must be positive and countPerPage 1..10")
        data = self._call(
            Method.POST,
            f"{_SELLER_API}/brands/search",
            document={
                "brandName": _text(brand_name, "brandName"),
                "countPerPage": count_per_page,
                "page": page,
            },
        )
        if not isinstance(data, dict):
            raise CoupangCatalogContractError("brand search data must be an object")
        return BrandSearchPage(
            _integer(data.get("page"), "page"),
            _integer(data.get("countPerPage"), "countPerPage"),
            _integer(data.get("totalCount"), "totalCount"),
            tuple(_brand(item, "items") for item in _objects(data.get("items"), "items")),
        )

    def enrolled_brands(self) -> tuple[Brand, ...]:
        data = self._call(Method.GET, f"{_SELLER_API}/brands/enrolled")
        return tuple(_brand(item, "data") for item in _objects(data, "data"))

    def brand(self, brand_id: str) -> Brand:
        if _BRAND_ID.fullmatch(brand_id) is None:
            raise CoupangCatalogContractError("brandId has an invalid format")
        return _brand(self._call(Method.GET, f"{_SELLER_API}/brands/{brand_id}"))


def leaf_categories(root: CategoryNode) -> tuple[ProviderCategory, ...]:
    """Flatten an official full category tree into provider-neutral leaf facts."""
    leaves: list[ProviderCategory] = []

    def visit(node: CategoryNode, parents: tuple[str, ...]) -> None:
        names = parents + (() if node.category_id == "0" else (node.name,))
        if not node.children and node.category_id != "0":
            leaves.append(ProviderCategory(node.category_id, node.name, " > ".join(names), True))
            return
        for child in node.children:
            visit(child, names)

    visit(root, ())
    return tuple(sorted(leaves, key=lambda item: item.category_id))


__all__ = [
    "Brand",
    "BrandSearchPage",
    "CategoryAttribute",
    "CategoryMetadata",
    "CategoryNode",
    "CategoryRecommendation",
    "Certification",
    "CoupangCatalogClient",
    "CoupangCatalogContractError",
    "NoticeCategory",
    "NoticeDetail",
    "RequiredDocument",
    "leaf_categories",
]
