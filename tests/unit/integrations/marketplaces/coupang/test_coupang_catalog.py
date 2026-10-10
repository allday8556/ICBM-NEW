"""Official Coupang Categories and Brands contracts over the provider-zero transport."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Credentials,
    Market,
    Method,
)
from integrations.marketplaces.coupang.catalog import (
    CoupangCatalogClient,
    CoupangCatalogContractError,
    leaf_categories,
)
from integrations.marketplaces.coupang.fake_transport import FakeResponse, FakeTransport


@dataclass(frozen=True)
class Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _client(
    *responses: object, market: Market = Market.KR
) -> tuple[CoupangCatalogClient, FakeTransport]:
    transport = FakeTransport(tuple(FakeResponse(200, response) for response in responses))
    context = AuthContext(Credentials("access", "secret", "A0001", 1), market)
    return CoupangCatalogClient(Authenticator(Clock()), context, transport), transport


def _success(data: object) -> dict[str, object]:
    return {"code": "SUCCESS", "message": "", "data": data}


def test_category_tree_and_leaf_projection_preserve_provider_identity_and_path() -> None:
    client, transport = _client(
        _success(
            {
                "displayItemCategoryCode": 0,
                "name": "ROOT",
                "status": "ACTIVE",
                "child": [
                    {
                        "displayItemCategoryCode": 10,
                        "name": "식품",
                        "status": "ACTIVE",
                        "child": [
                            {
                                "displayItemCategoryCode": 11,
                                "name": "차",
                                "status": "ACTIVE",
                                "child": [],
                            }
                        ],
                    }
                ],
            }
        )
    )

    root = client.category_tree()

    leaves = leaf_categories(root)
    assert len(leaves) == 1
    leaf = leaves[0]
    assert (leaf.category_id, leaf.name, leaf.whole_category_name, leaf.leaf) == (
        "11",
        "차",
        "식품 > 차",
        True,
    )
    assert transport.requests[0].target.method is Method.GET
    assert transport.requests[0].target.path.endswith("/meta/display-categories")
    assert transport.requests[0].body == b""


def test_all_category_read_paths_are_exact_and_vendor_identity_is_not_an_argument() -> None:
    client, transport = _client(
        _success({"displayItemCategoryCode": 78877, "name": "차", "status": "ACTIVE", "child": []}),
        _success(True),
        _success(False),
    )

    assert client.category(78877).category_id == "78877"
    assert client.category_valid("78877") is True
    assert client.auto_category_agreed() is False
    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories/78877",
        "/v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories/78877/status",
        "/v2/providers/seller_api/apis/api/v1/marketplace/vendors/A0001/check-auto-category-agreed",
    ]


def test_recommendation_uses_documented_body_and_remains_a_suggestion() -> None:
    client, transport = _client(
        {
            "code": 200,
            "message": "OK",
            "data": {
                "autoCategorizationPredictionResultType": "SUCCESS",
                "comment": "candidate",
                "predictedCategoryId": "63955",
                "predictedCategoryName": "세탁세제",
            },
        }
    )

    result = client.recommend_category(
        "가루 세제",
        brand="브랜드",
        attributes={"형태": "분말"},
        seller_sku_code="sku-1",
    )

    assert result.provider_suggestion_only is True
    assert result.predicted_category_id == "63955"
    request = transport.requests[0]
    assert request.target.method is Method.POST
    assert request.target.path == "/v2/providers/openapi/apis/api/v1/categorization/predict"
    assert json.loads(request.body) == {
        "attributes": {"형태": "분말"},
        "brand": "브랜드",
        "productName": "가루 세제",
        "sellerSkuCode": "sku-1",
    }


def test_category_metadata_retains_notices_attributes_documents_and_certifications() -> None:
    client, _ = _client(
        _success(
            {
                "isAllowSingleItem": True,
                "attributes": [
                    {
                        "attributeTypeName": "수량",
                        "dataType": "NUMBER",
                        "basicUnit": "개",
                        "usableUnits": ["개", "개입"],
                        "required": "MANDATORY",
                        "groupNumber": "NONE",
                        "exposed": "EXPOSED",
                    }
                ],
                "noticeCategories": [
                    {
                        "noticeCategoryName": "식품",
                        "noticeCategoryDetailNames": [
                            {"noticeCategoryDetailName": "용량", "required": "MANDATORY"}
                        ],
                    }
                ],
                "requiredDocumentNames": [{"templateName": "신고서", "required": "OPTIONAL"}],
                "certifications": [
                    {
                        "certificationType": "FOOD",
                        "name": "식품인증",
                        "dataType": "CODE",
                        "required": "RECOMMEND",
                    }
                ],
                "allowedOfferConditions": ["NEW"],
            }
        )
    )

    metadata = client.category_metadata(78877)

    assert metadata.allows_single_item is True
    assert metadata.attributes[0].usable_units == ("개", "개입")
    assert metadata.notices[0].details[0].name == "용량"
    assert metadata.required_documents[0].template_name == "신고서"
    assert metadata.certifications[0].data_type == "CODE"
    assert metadata.allowed_offer_conditions == ("NEW",)


def test_brand_family_retains_documented_fields_and_exact_paths() -> None:
    client, transport = _client(
        _success(
            {
                "page": 1,
                "countPerPage": 10,
                "totalCount": 1,
                "items": [
                    {
                        "brandId": "KR-5",
                        "brandName": "NIKE",
                        "brandLogoUrl": "https://example.com/nike.png",
                        "isUIDRequired": True,
                        "allowedUIDTypes": ["GTIN", "MPN"],
                    }
                ],
            }
        ),
        _success([{"brandId": "KR-5", "brandName": "NIKE"}]),
        _success(
            {
                "brandId": "KR-5",
                "brandName": "NIKE",
                "brandLogoUrl": None,
                "isUIDRequired": True,
                "allowedUIDTypes": ["GTIN"],
            }
        ),
    )

    page = client.search_brands("NIKE")
    enrolled = client.enrolled_brands()
    brand = client.brand("KR-5")

    assert (page.total_count, page.items[0].allowed_uid_types) == (1, ("GTIN", "MPN"))
    assert enrolled[0].name == "NIKE"
    assert brand.uid_required is True
    assert [request.target.path for request in transport.requests] == [
        "/v2/providers/seller_api/apis/api/v1/marketplace/brands/search",
        "/v2/providers/seller_api/apis/api/v1/marketplace/brands/enrolled",
        "/v2/providers/seller_api/apis/api/v1/marketplace/brands/KR-5",
    ]


def test_catalog_family_fails_closed_before_transport() -> None:
    with pytest.raises(CoupangCatalogContractError, match="KR only"):
        _client(market=Market.TW)

    client, transport = _client()
    with pytest.raises(CoupangCatalogContractError, match="numeric"):
        client.category("12/../34")
    with pytest.raises(CoupangCatalogContractError, match="invalid format"):
        client.brand("KR-5?x=1")
    with pytest.raises(CoupangCatalogContractError, match="countPerPage"):
        client.search_brands("NIKE", count_per_page=11)
    assert transport.requests == []


def test_malformed_success_response_is_not_treated_as_provider_fact() -> None:
    client, _ = _client(_success({"displayItemCategoryCode": "bad"}))

    with pytest.raises(CoupangCatalogContractError):
        client.category(10)
