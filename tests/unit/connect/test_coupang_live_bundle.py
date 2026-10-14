from types import SimpleNamespace
from typing import Any

import pytest

from app.stages.collect.assets import StoredAsset
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from app.stages.connect.coupang.live_bundle import build_live_test_product_bundle
from integrations.marketplaces.coupang.auth import Credentials
from integrations.marketplaces.coupang.live_mutations import TEST_PRODUCT_BINDINGS


def _metadata(*, document_required: bool = False) -> dict[str, object]:
    return {
        "allows_single_item": True,
        "attributes": [
            {
                "name": "수량",
                "data_type": "NUMBER",
                "basic_unit": "개",
                "usable_units": ["개"],
                "required": "MANDATORY",
                "group_number": "NONE",
                "exposed": "EXPOSED",
            }
        ],
        "notices": [
            {
                "name": "기타 재화",
                "details": [{"name": "품명 및 모델명", "required": "MANDATORY"}],
            }
        ],
        "required_documents": [{"template_name": "신고서", "required": "MANDATORY"}]
        if document_required
        else [],
        "certifications": [],
        "allowed_offer_conditions": ["NEW"],
    }


def _revision(
    source_product_id: str, *, locator: str = "https://img.example.com/square.jpg"
) -> Any:
    return SimpleNamespace(
        supplier_key="icbm-synthetic",
        source_product_id=source_product_id,
        images=(
            ImageReference(
                role=ImageRole.REPRESENTATIVE,
                ordinal=0,
                host="img.example.com",
                provenance="test",
                status=FieldStatus.CONFIRMED,
                sha256="a" * 64,
                locator=locator,
            ),
        ),
        fingerprints_intact=lambda: True,
    )


def _bundle(*, metadata: dict[str, object] | None = None, asset: StoredAsset | None = None):
    revisions = {
        binding.source_product_id: _revision(binding.source_product_id)
        for binding in TEST_PRODUCT_BINDINGS
    }
    codes = tuple(str(100 + index) for index in range(8))
    metadata_by_code = {code: metadata or _metadata() for code in codes}
    return build_live_test_product_bundle(
        credentials=Credentials("access", "secret", "vendor-1", 1),
        vendor_user_id="wing-user",
        logistics={
            "companyContactNumber": "01000000000",
            "deliveryCompanyCode": "CJGLS",
            "outboundShippingPlaceCode": "1",
            "returnAddress": "부산광역시",
            "returnAddressDetail": "테스트",
            "returnCenterCode": "2",
            "returnChargeName": "테스트 반품지",
            "returnZipCode": "00000",
        },
        category_codes_by_test=codes,
        category_metadata=metadata_by_code,
        revision_of=lambda supplier, source: revisions.get(source),
        asset_of=lambda sha256: asset or StoredAsset(sha256, "image/jpeg", 100_000, 1000, 1000),
    )


def test_builds_exact_closed_bundle_with_external_images_and_no_repr_secrets() -> None:
    bundle = _bundle()

    assert len(bundle.plans) == 11
    assert [plan.binding for plan in bundle.plans] == list(TEST_PRODUCT_BINDINGS)
    for index, plan in enumerate(bundle.plans, start=1):
        create = plan.create_document.value()
        assert create["requested"] is False
        assert create["sellerProductName"] == f"ICBM TEST {index}"
        item = create["items"][0]
        assert item["externalVendorSku"] == TEST_PRODUCT_BINDINGS[index - 1].source_product_id
        assert item["images"] == [
            {
                "imageType": "REPRESENTATION",
                "vendorPath": "https://img.example.com/square.jpg",
            }
        ]
        assert item["attributes"] == [{"attributeTypeName": "수량", "attributeValueName": "1개"}]
    assert "wing-user" not in repr(bundle)
    assert "secret" not in repr(bundle)


def test_digest_is_deterministic_and_tests_nine_to_eleven_reuse_test_one_category() -> None:
    first = _bundle()
    second = _bundle()

    assert first.payload_digest == second.payload_digest
    categories = [plan.create_document.value()["displayCategoryCode"] for plan in first.plans]
    assert categories[8:] == [categories[0], categories[0], categories[0]]


@pytest.mark.parametrize(
    "asset",
    [
        StoredAsset("a" * 64, "image/webp", 100_000, 1000, 1000),
        StoredAsset("a" * 64, "image/jpeg", 3_000_001, 1000, 1000),
        StoredAsset("a" * 64, "image/jpeg", 100_000, 1000, 900),
        StoredAsset("a" * 64, "image/jpeg", 100_000, 499, 499),
    ],
)
def test_rejects_images_outside_coupang_representative_contract(asset: StoredAsset) -> None:
    with pytest.raises(ValueError, match="NO_ACCEPTABLE_REPRESENTATIVE_IMAGE"):
        _bundle(asset=asset)


def test_required_account_document_holds_before_any_provider_call() -> None:
    with pytest.raises(ValueError, match="REQUIRES_ACCOUNT_DOCUMENT"):
        _bundle(metadata=_metadata(document_required=True))
