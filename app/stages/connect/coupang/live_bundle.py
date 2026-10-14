"""Build the closed Coupang test-product bundle from local truth and secret preflight facts.

No payload is written to disk.  Account values stay in memory and the returned documents hide
their canonical bytes from ``repr`` through :class:`FrozenDocument`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from app.stages.collect.assets import StoredAsset
from app.stages.collect.facts import FieldStatus
from app.stages.collect.models import SYNTHETIC_SUPPLIER_KEY
from app.stages.collect.revisions import StoredRevision
from integrations.marketplaces.coupang.auth import Credentials
from integrations.marketplaces.coupang.live_mutations import (
    TEST_PRODUCT_BINDINGS,
    TestProductBundle,
)

_MAX_IMAGE_BYTES: Final = 3_000_000
_MIN_IMAGE_EDGE: Final = 500
_MAX_IMAGE_EDGE: Final = 5_000
PRIOR_REJECTED_PAYLOAD_DIGEST: Final = (
    "4adca07b21ec120c120f7a8d62893b4122b7ef785d2eb56979011f179b271796"
)


def _required(value: object) -> bool:
    return isinstance(value, str) and value.upper() in {"MANDATORY", "REQUIRED"}


def _attribute_value(attribute: Mapping[str, object]) -> str:
    name = attribute.get("name")
    data_type = attribute.get("data_type")
    basic_unit = attribute.get("basic_unit")
    if not isinstance(name, str) or not name:
        raise ValueError("COUPANG_CATEGORY_METADATA_INVALID")
    if data_type == "NUMBER":
        return f"1{basic_unit}" if isinstance(basic_unit, str) and basic_unit else "1"
    if data_type in {"STRING", "TEXT"}:
        return "상세페이지 참조"
    raise ValueError("COUPANG_REQUIRED_ATTRIBUTE_NEEDS_OPERATOR_VALUE")


def _attributes(metadata: Mapping[str, object]) -> list[dict[str, str]]:
    rows = metadata.get("attributes")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("COUPANG_CATEGORY_METADATA_INVALID")
    result: list[dict[str, str]] = []
    for row in rows:
        assert isinstance(row, dict)
        if not _required(row.get("required")):
            continue
        name = row.get("name")
        assert isinstance(name, str)
        result.append({"attributeTypeName": name, "attributeValueName": _attribute_value(row)})
    return result


def _notices(metadata: Mapping[str, object]) -> list[dict[str, str]]:
    categories = metadata.get("notices")
    if not isinstance(categories, list) or not all(isinstance(row, dict) for row in categories):
        raise ValueError("COUPANG_CATEGORY_METADATA_INVALID")
    if not categories:
        return []
    category = categories[0]
    assert isinstance(category, dict)
    category_name = category.get("name")
    details = category.get("details")
    if (
        not isinstance(category_name, str)
        or not category_name
        or not isinstance(details, list)
        or not all(isinstance(row, dict) for row in details)
    ):
        raise ValueError("COUPANG_CATEGORY_METADATA_INVALID")
    result: list[dict[str, str]] = []
    for row in details:
        assert isinstance(row, dict)
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("COUPANG_CATEGORY_METADATA_INVALID")
        result.append(
            {
                "noticeCategoryName": category_name,
                "noticeCategoryDetailName": name,
                "content": "상품 상세페이지 참조",
            }
        )
    return result


def _require_no_account_specific_documents(metadata: Mapping[str, object]) -> None:
    for key in ("required_documents", "certifications"):
        rows = metadata.get(key)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("COUPANG_CATEGORY_METADATA_INVALID")
        if any(_required(row.get("required")) for row in rows):
            raise ValueError("COUPANG_CATEGORY_REQUIRES_ACCOUNT_DOCUMENT")


def _representative_image(
    revision: StoredRevision,
    asset_of: Callable[[str], StoredAsset | None],
) -> str:
    for image in revision.images:
        if (
            image.status is not FieldStatus.CONFIRMED
            or image.locator is None
            or image.sha256 is None
        ):
            continue
        asset = asset_of(image.sha256)
        if (
            asset is not None
            and asset.mime_type in {"image/jpeg", "image/png"}
            and asset.byte_size <= _MAX_IMAGE_BYTES
            and asset.width == asset.height
            and _MIN_IMAGE_EDGE <= asset.width <= _MAX_IMAGE_EDGE
        ):
            return image.locator
    raise ValueError("COUPANG_PRODUCT_HAS_NO_ACCEPTABLE_REPRESENTATIVE_IMAGE")


def _category_codes(codes_by_test: tuple[str, ...]) -> tuple[str, ...]:
    if len(codes_by_test) != 8:
        raise ValueError("COUPANG_CATEGORY_RECOMMENDATIONS_INVALID")
    return (*codes_by_test, codes_by_test[0], codes_by_test[0], codes_by_test[0])


def build_live_test_product_bundle(
    *,
    credentials: Credentials,
    vendor_user_id: str,
    logistics: Mapping[str, object],
    category_codes_by_test: tuple[str, ...],
    category_metadata: Mapping[str, Mapping[str, object]],
    revision_of: Callable[[str, str], StoredRevision | None],
    asset_of: Callable[[str], StoredAsset | None],
) -> TestProductBundle:
    """Build exactly test 1..11 with one validated external representative image each."""
    if not vendor_user_id or len(vendor_user_id) > 100:
        raise ValueError("COUPANG_VENDOR_USER_ID_MISSING")
    required_logistics = {
        "companyContactNumber",
        "deliveryCompanyCode",
        "outboundShippingPlaceCode",
        "returnAddress",
        "returnAddressDetail",
        "returnCenterCode",
        "returnChargeName",
        "returnZipCode",
    }
    if set(logistics) != required_logistics:
        raise ValueError("COUPANG_LOGISTICS_SELECTION_INVALID")
    codes = _category_codes(category_codes_by_test)
    products: list[dict[str, object]] = []
    for index, (binding, category_code) in enumerate(
        zip(TEST_PRODUCT_BINDINGS, codes, strict=True), start=1
    ):
        metadata = category_metadata.get(category_code)
        if metadata is None:
            raise ValueError("COUPANG_CATEGORY_METADATA_INCOMPLETE")
        _require_no_account_specific_documents(metadata)
        revision = revision_of(SYNTHETIC_SUPPLIER_KEY, binding.source_product_id)
        if (
            revision is None
            or revision.supplier_key != SYNTHETIC_SUPPLIER_KEY
            or revision.source_product_id != binding.source_product_id
            or not revision.fingerprints_intact()
        ):
            raise ValueError("COUPANG_SYNTHETIC_PRODUCT_BINDING_INVALID")
        image_url = _representative_image(revision, asset_of)
        name = f"ICBM TEST {index}"
        item: dict[str, object] = {
            "itemName": name,
            "originalPrice": 10000,
            "salePrice": 9000,
            "maximumBuyCount": 1,
            "maximumBuyForPerson": 0,
            "maximumBuyForPersonPeriod": 1,
            "outboundShippingTimeDay": 1,
            "unitCount": 1,
            "adultOnly": "EVERYONE",
            "taxType": "TAX",
            "parallelImported": "NOT_PARALLEL_IMPORTED",
            "overseasPurchased": "NOT_OVERSEAS_PURCHASED",
            "pccNeeded": False,
            "externalVendorSku": binding.source_product_id,
            "emptyBarcode": True,
            "emptyBarcodeReason": "테스트 상품으로 바코드 없음",
            "images": [{"imageType": "REPRESENTATION", "vendorPath": image_url}],
            "attributes": _attributes(metadata),
            "notices": _notices(metadata),
            "contents": [
                {
                    "contentsType": "TEXT",
                    "contentDetails": [{"content": name, "detailType": "TEXT"}],
                }
            ],
        }
        create = {
            "displayCategoryCode": int(category_code),
            "sellerProductName": name,
            "vendorId": credentials.vendor_id,
            "saleStartedAt": "2026-10-14T00:00:00",
            "saleEndedAt": "2099-01-01T23:59:59",
            "deliveryMethod": "SEQUENCIAL",
            "deliveryChargeType": "FREE",
            "deliveryCharge": 0,
            "freeShipOverAmount": 0,
            "deliveryChargeOnReturn": 2500,
            "remoteAreaDeliverable": "N",
            "unionDeliveryType": "UNION_DELIVERY",
            **dict(logistics),
            "returnCharge": 2500,
            "vendorUserId": vendor_user_id,
            "requested": False,
            "items": [item],
        }
        products.append(
            {
                "test_name": binding.test_name,
                "synthetic_id": binding.synthetic_id,
                "source_product_id": binding.source_product_id,
                "template": binding.template,
                "create": create,
                "update_message": f"{name} UPDATED",
            }
        )
    return TestProductBundle.from_mapping({"products": products})


__all__ = ["PRIOR_REJECTED_PAYLOAD_DIGEST", "build_live_test_product_bundle"]
