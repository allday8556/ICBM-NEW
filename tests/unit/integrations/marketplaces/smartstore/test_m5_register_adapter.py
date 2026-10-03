"""M5 PR-D: the typed SmartStore REGISTER adapter — the wire projection of a frozen Snapshot, the
retention profile, the read-back normalizer/comparison, duplicate lookup and asset promotion.

Every provider fact these tests pin comes from the architect-supplied 2.89.0 packet (Issue #89
comment 5746489554). Nothing here performs I/O: the adapter is pure, and the transport contract of
the adopted read-backs is pinned separately in ``test_smartstore_product_reads.py``.
"""

import contextlib
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from app.stages.products.image_model import ImageAssetKind
from integrations.marketplaces.smartstore import assets, lookup, product, readback
from integrations.marketplaces.smartstore.notice_schema import NoticePresence, load_notice_schema
from integrations.marketplaces.smartstore.registry import ADOPTED, EndpointId, resolve
from integrations.marketplaces.smartstore.retention import retain, retained_query

IDENTITY = "icbm-0123456789abcdef0123456789abcdef"
# The smartstore-seller-management-code/v1 projection of IDENTITY (architect ruling R1): the first
# 30 lowercase hex characters of SHA-256("smartstore-seller-management-code/v1\0" + identity).
SELLER_CODE = hashlib.sha256(
    b"smartstore-seller-management-code/v1\0" + IDENTITY.encode("utf-8")
).hexdigest()[:30]
KEY_A = "rik1-" + "a" * 32
KEY_B = "rik1-" + "b" * 32
REF_MAIN = "https://shop-phinf.example/a/main.jpg"
REF_DETAIL = "https://shop-phinf.example/a/detail.jpg"
ORIGIN_READ = EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2
CHANNEL_READ = EndpointId.SMARTSTORE_CHANNEL_PRODUCT_READ_V2


def _asset(role: str, sha: str, reference: str | None) -> dict[str, Any]:
    return {
        "role": role,
        "position": 0,
        "asset_kind": ImageAssetKind.SOURCE_ASSET.value,
        "sha256": sha,
        "derivation_id": None,
        "qa_result_id": "qa-1",
        "qa_verdict": "PASS",
        "asset_profile": "asset-profile-1",
        "provider_asset_ref": reference,
    }


def _item(key: str, price: int, options: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "registration_item_key": key,
        "item_id": key[-4:],
        "product_group_id": "pg-1",
        "composition_signature": "cs-1",
        "pricing_snapshot_id": "ps-1",
        "sale_price_krw": price,
        "price_basis": "MINIMUM_SALE_PRICE",
        "options": options or {},
        "publication_assets": [
            _asset("REPRESENTATIVE", "1" * 64, REF_MAIN),
            _asset("ADDITIONAL", "2" * 64, REF_DETAIL),
        ],
    }


def payload(**overrides: Any) -> dict[str, Any]:
    """A frozen Snapshot payload in the exact shape ``app.stages.register.payload`` builds."""
    base: dict[str, Any] = {
        "builder_version": "registration-payload/v1",
        "marketplace_key": "smartstore",
        "marketplace_account_id": "mpa-" + "0" * 32,
        "listing_shape": "SEPARATE_LISTINGS",
        "listing_identity": IDENTITY,
        "category": {
            "category_id": "cat-1",
            "mapping_revision": "map-1",
            "taxonomy_revision": "tax-1",
            "metadata_revision": "meta-1",
        },
        "name": {"value": "테스트 상품", "provenance": "OPERATOR_CONFIRMED"},
        "tags": ["tag-a"],
        "attributes": {"brand": {"value": "KM", "provenance": "SOURCE_FACT"}},
        "notice": {
            "notice_type": "Wear2023",
            "fields": {
                "material": {"value": "면 100%", "provenance": "SOURCE_FACT"},
                "manufacturer": {"detail_page_reference": True, "provenance": "SOURCE_FACT"},
            },
        },
        "policy": {"policy_revision": "policy-1", "templates": {}},
        "detail": {"composition_revision": "detail-1", "sections": ["BODY"], "body": "본문"},
        "items": [_item(KEY_A, 19900)],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- seller identities (§7, §8)


def test_seller_codes_come_from_stable_identities_never_from_a_label() -> None:
    # R1: the provider code is the deterministic 30-hex projection of the listing identity, which
    # itself is unchanged and stays ICBM's local spine (ADR-0014 §7).
    codes = product.seller_codes(payload())
    assert codes.seller_management_code == SELLER_CODE
    assert len(SELLER_CODE) == 30 and SELLER_CODE.lower() == SELLER_CODE
    assert all(character in "0123456789abcdef" for character in SELLER_CODE)
    assert codes.listing_identity == IDENTITY
    assert codes.projection_version == "smartstore-seller-management-code/v1"
    assert codes.option_codes == (KEY_A,)
    # A mutable display value changes nothing: not the name, not the tags, not an option label.
    renamed = payload(
        name={"value": "완전히 다른 이름", "provenance": "OPERATOR_CONFIRMED"}, tags=["zz"]
    )
    assert product.seller_codes(renamed) == codes


def test_the_option_codes_travel_verbatim_because_no_length_rule_is_captured() -> None:
    # The official evidence proves no length or charset bound for sellerManagerCode, so no
    # truncation or hashing rule is invented for it; the registration_item_key travels as it is.
    # Only sellerManagementCode has a documented 30-character bound, which R1's projection meets.
    codes = product.seller_codes(payload(items=[_item(KEY_A, 19900), _item(KEY_B, 19900)]))
    assert codes.option_codes == (KEY_A, KEY_B)
    assert all(code.isascii() and code.strip() == code for code in codes.option_codes)


def test_the_seller_management_code_projection_is_deterministic_and_versioned() -> None:
    other = "icbm-" + "f" * 32
    assert product.seller_management_code(IDENTITY) == SELLER_CODE
    assert product.seller_management_code(other) != SELLER_CODE
    # It is a projection, never a replacement: the identity is not recoverable from the code.
    assert IDENTITY not in SELLER_CODE


# ---------------------------------------------------------------- the wire projection (§3)


def test_the_projection_states_only_captured_fields_and_names_its_gaps() -> None:
    projected = product.project(payload())
    assert projected.encoding_version == "smartstore-register-wire/v6"
    assert projected.document.mapping() == {
        # Both required channel members, each with the value ICBM owns (5915900049 D1, D2.1).
        "smartstoreChannelProduct": {
            "channelProductDisplayStatusType": "ON",
            "naverShoppingRegistration": True,
        },
        "originProduct": {
            # E2 (Issue #89 `5868542027`): on registration the CREATE endpoint accepts only SALE.
            "statusType": "SALE",
            "name": "테스트 상품",
            "detailContent": "본문",
            "images": {
                "representativeImage": {"url": REF_MAIN},
                "optionalImages": [{"url": REF_DETAIL}],
            },
            "salePrice": 19900,
            # The registration seed (D2.2), never a source quantity.
            "stockQuantity": 1,
            # Required on registration (packet 5862400626); the value is the operator-reviewed
            # category id the Snapshot froze, emitted verbatim.
            "leafCategoryId": "cat-1",
            "detailAttribute": {"sellerCodeInfo": {"sellerManagementCode": SELLER_CODE}},
        },
    }
    assert projected.image_references == (REF_MAIN, REF_DETAIL)
    # The reviewed notice the Snapshot owns is kept as evidence. Its type has no captured child
    # in the pinned table, so nothing of it is placed on the wire.
    assert projected.notice_type == "Wear2023"
    assert projected.notice_fields == {"material": "면 100%"}
    assert not projected.sendable
    assert projected.gaps == (product.GAP_NOTICE_TYPE_CHILD,)


def test_the_registration_stock_quantity_is_the_owned_seed() -> None:
    # D2.2: stockQuantity is required on registration and must be at least 1 (5862400626). ICBM
    # registers the seed 1 for every projected Snapshot — never the documented option default 0,
    # never a source quantity — and it is no gap.
    for items in (
        None,
        [_item(KEY_A, 19900, {"색상": "빨강"}), _item(KEY_B, 19900, {"색상": "파랑"})],
    ):
        projected = product.project(payload() if items is None else payload(items=items))
        assert projected.document.mapping()["originProduct"]["stockQuantity"] == 1
        assert not any("stockQuantity" in gap for gap in projected.gaps)
    assert product.REGISTRATION_STOCK_QUANTITY == 1
    assert not hasattr(product, "GAP_REGISTRATION_STOCK_QUANTITY")
    # A document carrying any other quantity is outside the frozen contract.
    body = product.project(payload()).document.mapping()
    for other in (0, 2, 10, True, "1"):
        body["originProduct"]["stockQuantity"] = other
        with pytest.raises(product.WireContractError) as refused:
            product.create_document(IDENTITY, body)
        assert refused.value.code == "WIRE_DOCUMENT_VALUE_INVALID", other


def test_the_channel_members_carry_the_owned_values_only() -> None:
    # E1 closes the *type* of naverShoppingRegistration; D2.1 owns its value: true, ICBM's
    # publication intent. The display status is ON (D1). Neither is derived from the provider.
    projected = product.project(payload())
    channel = projected.document.mapping()["smartstoreChannelProduct"]
    assert channel == {"channelProductDisplayStatusType": "ON", "naverShoppingRegistration": True}
    assert product.REGISTRATION_NAVER_SHOPPING_REGISTRATION is True
    assert product.NAVER_SHOPPING_REGISTRATION_VALUES == (True, False)
    assert not hasattr(product, "GAP_SHOPPING_REGISTRATION")
    assert not any("naverShoppingRegistration" in gap for gap in projected.gaps)
    # E2 closes the statusType gap: it is projected, and no gap names it.
    assert projected.document.mapping()["originProduct"]["statusType"] == "SALE"
    assert not hasattr(product, "GAP_STATUS_TYPE")
    assert not hasattr(product, "GAP_CHANNEL_DISPLAY_STATUS")
    body = projected.document.mapping()
    for value in (False, "true", 1, None):
        body["smartstoreChannelProduct"]["naverShoppingRegistration"] = value
        with pytest.raises(product.WireContractError) as refused:
            product.create_document(IDENTITY, body)
        assert refused.value.code == "WIRE_DOCUMENT_VALUE_INVALID", value
    # The option-price gap is unchanged: an option listing is still never sendable.
    items = [_item(KEY_A, 19900, {"색상": "빨강"}), _item(KEY_B, 19900, {"색상": "파랑"})]
    options = product.project(payload(items=items, notice=_etc_notice()))
    assert options.gaps == (product.GAP_OPTION_PRICE_SEMANTICS,)
    assert options.sendable is False


def test_the_projection_never_emits_a_value_the_evidence_does_not_carry() -> None:
    projected = product.project(payload()).document
    text = projected.canonical_json
    for never in (
        # A separate Shopping Window channel structure, out of the SmartStore-only scope.
        "windowChannelProduct",
        # The reviewed type of this Snapshot has no captured child: no notice at all.
        "productInfoProvidedNotice",
        # Unowned optional structures are omitted rather than defaulted (delivery, A/S, origin).
        "deliveryInfo",
        "afterServiceInfo",
        "originAreaInfo",
        # Unowned optional channel members.
        "channelProductName",
        "bbsSeq",
        "storeKeepExclusiveProduct",
    ):
        assert never not in text


# ---------------------------------------------------------------- the notice child (D2.3)


def _value(text: str) -> dict[str, Any]:
    return {"value": text, "provenance": "OPERATOR_CONFIRMED"}


def _etc_notice(**fields: dict[str, Any] | None) -> dict[str, Any]:
    """A reviewed ETC (기타 재화) notice with every required member, as the payload freezes it."""
    values: dict[str, Any] = {
        "itemName": _value("텀블러"),
        "modelName": _value("TB-500"),
        "manufacturer": _value("KM통상"),
        "customerServicePhoneNumber": _value("02-000-0000"),
        "returnCostReason": {"detail_page_reference": True, "provenance": "OPERATOR_CONFIRMED"},
    }
    values.update(fields)
    return {
        "notice_type": "ETC",
        "fields": {key: value for key, value in values.items() if value is not None},
    }


def test_the_child_mapping_is_the_provider_notice_schemas() -> None:
    members = product.NOTICE_TYPE_MEMBERS
    # Every child the provider notice schema documents (notice coverage S3), and no other.
    assert len(members) == 36
    assert members["ETC"] == "etc" and members["GENERAL_FOOD"] == "generalFood"
    assert members["SPORTS_EQUIPMENT"] == "sportsEquipment" and members["RENTAL_HA"] == "rentalHa"
    assert members["CELLPHONE"] == "cellPhone" and members["MICROELECTRONICS"] == "microElectronics"
    for undocumented in ("LODGMENT_RESERVATION", "TRAVEL_PACKAGE", "AIRLINE_TICKET", "RENT_CAR"):
        assert undocumented not in members
    assert product.NOTICE_SCHEMA_REVISION == "smartstore-notice-schema/2.90.0-r1"
    common = {
        "returnCostReason",
        "noRefundReason",
        "qualityAssuranceStandard",
        "compensationProcedure",
        "troubleShootingContents",
    }
    for schema in load_notice_schema().types.values():
        fields = {f.name: f for f in schema.fields}
        assert common <= set(fields)
        assert all(
            fields[name].presence is NoticePresence.DETAIL_REFERENCE_DEFAULT for name in common
        )


def test_a_captured_notice_makes_a_single_item_listing_sendable() -> None:
    projected = product.project(payload(notice=_etc_notice()))
    notice = projected.document.mapping()["originProduct"]["detailAttribute"][
        "productInfoProvidedNotice"
    ]
    # Exactly the child of the reviewed type, with exactly the reviewed text values. The member
    # left to the product detail is out: its documented default is "상품상세 참조".
    assert notice == {
        "productInfoProvidedNoticeType": "ETC",
        "etc": {
            "itemName": "텀블러",
            "modelName": "TB-500",
            "manufacturer": "KM통상",
            "customerServicePhoneNumber": "02-000-0000",
        },
    }
    assert projected.gaps == ()
    assert projected.sendable is True
    assert product.completeness_gaps(projected.document) == ()


@pytest.mark.parametrize(
    "notice_type",
    ["Wear2023", "etc", "Etc", " ETC", "RENT_CAR", "LODGMENT_RESERVATION", "기타"],
)
def test_an_uncaptured_notice_type_stays_a_gap_and_never_falls_back(notice_type: str) -> None:
    notice = _etc_notice()
    notice["notice_type"] = notice_type
    projected = product.project(payload(notice=notice))
    assert projected.gaps == (product.GAP_NOTICE_TYPE_CHILD,)
    assert projected.sendable is False
    assert "productInfoProvidedNotice" not in projected.document.canonical_json
    assert product.completeness_gaps(projected.document) == (product.GAP_NOTICE_TYPE_CHILD,)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        # A member the child does not document, including another type's member.
        ({"material": _value("면")}, "WIRE_DOCUMENT_FIELD_UNKNOWN"),
        ({"importDeclaration": _value("true")}, "WIRE_DOCUMENT_FIELD_UNKNOWN"),
        # A required member without a documented default, missing or left to the detail.
        ({"itemName": None}, "WIRE_DOCUMENT_FIELD_MISSING"),
        (
            {"modelName": {"detail_page_reference": True, "provenance": "OPERATOR_CONFIRMED"}},
            "WIRE_DOCUMENT_FIELD_MISSING",
        ),
        # customerServicePhoneNumber is required without afterServiceDirector.
        ({"customerServicePhoneNumber": None}, "WIRE_DOCUMENT_FIELD_MISSING"),
        # A value past its documented bound.
        ({"itemName": _value("x" * 51)}, "WIRE_DOCUMENT_VALUE_INVALID"),
    ],
    ids=[
        "another-type-member",
        "boolean-member",
        "required-missing",
        "required-left-to-detail",
        "conditional-missing",
        "too-long",
    ],
)
def test_a_notice_outside_its_child_contract_is_refused(fields: dict[str, Any], code: str) -> None:
    with pytest.raises(product.WireContractError) as refused:
        product.project(payload(notice=_etc_notice(**fields)))
    assert refused.value.code == code


def test_a_conditional_member_is_satisfied_by_its_alternative() -> None:
    # customerServicePhoneNumber is required only without afterServiceDirector.
    notice = _etc_notice(
        customerServicePhoneNumber=None, afterServiceDirector=_value("홍길동 02-111-1111")
    )
    projected = product.project(payload(notice=notice))
    child = projected.document.mapping()["originProduct"]["detailAttribute"][
        "productInfoProvidedNotice"
    ]["etc"]
    assert "customerServicePhoneNumber" not in child
    assert projected.sendable is True


def test_a_year_month_member_is_validated_never_reformatted() -> None:
    wear = {
        "notice_type": "WEAR",
        "fields": {
            "material": _value("면 100%"),
            "color": _value("흰색"),
            "size": _value("M"),
            "manufacturer": _value("KM통상"),
            "caution": _value("찬물 세탁"),
            "warrantyPolicy": _value("소비자분쟁해결기준"),
            "afterServiceDirector": _value("KM통상 02-000-0000"),
            "packDate": _value("2026-09"),
        },
    }
    projected = product.project(payload(notice=wear))
    child = projected.document.mapping()["originProduct"]["detailAttribute"][
        "productInfoProvidedNotice"
    ]["wear"]
    # packDate is given, so its text alternative is not required.
    assert child["packDate"] == "2026-09" and "packDateText" not in child
    assert projected.sendable is True
    for bad in ("2026-9", "2026-13", "2026.09", "Sep 2026"):
        wear["fields"]["packDate"] = _value(bad)
        with pytest.raises(product.WireContractError) as refused:
            product.project(payload(notice=wear))
        assert refused.value.code == "WIRE_DOCUMENT_VALUE_INVALID", bad
    # Without either, the text alternative is the required one.
    del wear["fields"]["packDate"]
    with pytest.raises(product.WireContractError) as refused:
        product.project(payload(notice=wear))
    assert refused.value.code == "WIRE_DOCUMENT_FIELD_MISSING"


def test_a_notice_document_names_exactly_its_own_child() -> None:
    body = product.project(payload(notice=_etc_notice())).document.mapping()
    notice = body["originProduct"]["detailAttribute"]["productInfoProvidedNotice"]
    for broken, code in (
        ({**notice, "wear": {}}, "WIRE_NOTICE_CHILD_MISMATCH"),
        ({"productInfoProvidedNoticeType": "ETC"}, "WIRE_NOTICE_CHILD_MISMATCH"),
        (
            {"productInfoProvidedNoticeType": "WEAR", "etc": notice["etc"]},
            "WIRE_NOTICE_CHILD_MISMATCH",
        ),
        ({"productInfoProvidedNoticeType": "RENT_CAR"}, "WIRE_NOTICE_TYPE_NOT_CAPTURED"),
        ({"productInfoProvidedNoticeType": "BAG", "bag": {}}, "WIRE_DOCUMENT_FIELD_MISSING"),
        ({"etc": notice["etc"]}, "WIRE_DOCUMENT_FIELD_MISSING"),
    ):
        body["originProduct"]["detailAttribute"]["productInfoProvidedNotice"] = broken
        with pytest.raises(product.WireContractError) as refused:
            product.create_document(IDENTITY, body)
        assert refused.value.code == code, broken


def test_the_projection_is_deterministic_for_the_same_snapshot() -> None:
    first, second = product.project(payload()), product.project(deepcopy(payload()))
    assert first.document.canonical_json == second.document.canonical_json
    assert (first.codes, first.image_references) == (second.codes, second.image_references)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            {"name": {"detail_page_reference": True, "provenance": "AI_SUGGESTION"}},
            "WIRE_VALUE_NOT_TEXT",
        ),
        ({"notice": None}, "WIRE_NOTICE_MISSING"),
        ({"items": [_item(KEY_A, 1_000_000_000)]}, "WIRE_SALE_PRICE_OUT_OF_RANGE"),
    ],
)
def test_a_snapshot_that_breaks_a_proven_rule_is_refused(change: dict[str, Any], code: str) -> None:
    with pytest.raises(product.WireContractError) as refused:
        product.project(payload(**change))
    assert refused.value.code == code


def test_an_unprepared_or_unsafe_image_is_never_projected() -> None:
    unprepared = payload(
        items=[
            {
                **_item(KEY_A, 19900),
                "publication_assets": [_asset("REPRESENTATIVE", "1" * 64, None)],
            }
        ]
    )
    with pytest.raises(product.WireContractError) as missing:
        product.project(unprepared)
    assert missing.value.code == "WIRE_IMAGE_NOT_PREPARED"
    signed = payload(
        items=[
            {
                **_item(KEY_A, 19900),
                "publication_assets": [
                    _asset("REPRESENTATIVE", "1" * 64, "https://cdn.example/a.jpg?sig=abc")
                ],
            }
        ]
    )
    with pytest.raises(product.WireContractError) as unsafe:
        product.project(signed)
    assert unsafe.value.code == "WIRE_IMAGE_REFERENCE_UNSAFE"


def test_a_listing_without_a_representative_image_is_refused() -> None:
    # The packet proves a representative image is required; a detail image never stands in.
    details = payload(
        items=[
            {
                **_item(KEY_A, 19900),
                "publication_assets": [_asset("ADDITIONAL", "2" * 64, REF_DETAIL)],
            }
        ]
    )
    with pytest.raises(product.WireContractError) as refused:
        product.project(details)
    assert refused.value.code == "WIRE_REPRESENTATIVE_IMAGE_MISSING"


def test_more_images_than_the_provider_allows_is_refused() -> None:
    many = [_asset("REPRESENTATIVE", "1" * 64, REF_MAIN)] + [
        _asset("ADDITIONAL", f"{i}" * 64, f"https://shop-phinf.example/a/{i}.jpg")
        for i in range(10)
    ]
    with pytest.raises(product.WireContractError) as refused:
        product.project(payload(items=[{**_item(KEY_A, 19900), "publication_assets": many}]))
    assert refused.value.code == "WIRE_IMAGE_COUNT_EXCEEDED"


def test_a_single_listing_with_options_keeps_every_item_and_bounds_its_dimensions() -> None:
    items = [_item(KEY_A, 19900, {"색상": "빨강"}), _item(KEY_B, 19900, {"색상": "파랑"})]
    projected = product.project(payload(listing_shape="SINGLE_LISTING_WITH_OPTIONS", items=items))
    assert projected.codes.option_codes == (KEY_A, KEY_B)
    # The combination form's own keys are captured, so the structure is projected; the identity of
    # each row is its seller code, never a display label.
    document = projected.document.mapping()
    option_info = document["originProduct"]["detailAttribute"]["optionInfo"]
    assert option_info == {
        "optionCombinationGroupNames": {"optionGroupName1": "색상"},
        "optionCombinations": [
            {"optionName1": "빨강", "sellerManagerCode": KEY_A},
            {"optionName1": "파랑", "sellerManagerCode": KEY_B},
        ],
    }
    # Whether a combination price is absolute or a difference is not captured, so neither the
    # value nor the documented default may be relied on: it stays a gap.
    assert product.GAP_OPTION_PRICE_SEMANTICS in projected.gaps
    assert "price" not in json.dumps(option_info)
    four = [
        _item(KEY_A, 19900, {"a": "1", "b": "2", "c": "3", "d": "4"}),
        _item(KEY_B, 19900, {"a": "1", "b": "2", "c": "3", "d": "5"}),
    ]
    with pytest.raises(product.WireContractError) as refused:
        product.project(payload(listing_shape="SINGLE_LISTING_WITH_OPTIONS", items=four))
    assert refused.value.code == "WIRE_OPTION_DIMENSIONS_EXCEEDED"


def test_differing_item_prices_are_refused_because_option_price_semantics_are_unproven() -> None:
    items = [_item(KEY_A, 19900, {"색상": "빨강"}), _item(KEY_B, 25900, {"색상": "파랑"})]
    with pytest.raises(product.WireContractError) as refused:
        product.project(payload(listing_shape="SINGLE_LISTING_WITH_OPTIONS", items=items))
    assert refused.value.code == "WIRE_OPTION_PRICE_SEMANTICS_UNPROVEN"


# ---------------------------------------------------------------- retention profile (§15)


def test_retention_keeps_only_allow_listed_leaves_whatever_the_envelope_is() -> None:
    contract = resolve(ORIGIN_READ)
    body = {
        "originProduct": {
            "name": "테스트 상품",
            "salePrice": 19900,
            "detailContent": "<p>본문</p>",  # not on the allow-list
            "images": [{"url": REF_MAIN, "order": 1}],
            "sellerCodeInfo": {"sellerManagementCode": SELLER_CODE, "sellerBarcode": "880123"},
        },
        "traceId": "t-1",
        "accessToken": "Bearer abcdefghijklmnop",
    }
    kept = retain(contract, body)
    text = json.dumps(kept, ensure_ascii=False)
    assert "detailContent" not in text and "본문" not in text
    assert "sellerBarcode" not in text and "880123" not in text
    assert "accessToken" not in text and "Bearer" not in text and "traceId" not in text
    assert kept["originProduct"]["sellerCodeInfo"] == {"sellerManagementCode": SELLER_CODE}
    assert kept["originProduct"]["images"] == [{"url": REF_MAIN}]


def test_no_adopted_endpoint_may_send_a_query_key() -> None:
    for contract in ADOPTED.values():
        assert contract.safe_query_keys == frozenset()
        assert retained_query(contract, {}) == {}
        with pytest.raises(ValueError, match="may not send query keys"):
            retained_query(contract, {"page": "1"})


# ---------------------------------------------------------------- read-back and comparison


def _provider_body(
    *,
    name: str = "테스트 상품",
    price: int = 19900,
    codes: tuple[str, ...] = (KEY_A,),
    url: str = REF_MAIN,
    management: str | None = SELLER_CODE,
    stock: Any = 1,
) -> dict[str, Any]:
    """A provider read-back shaped as an envelope the packet does not prove, so the normalizer
    must recognize the proven leaves wherever they sit."""
    product_node: dict[str, Any] = {
        "name": name,
        "salePrice": price,
        # The registration seed the Snapshot's projection sent (D2.2).
        "stockQuantity": stock,
        "images": {"representativeImage": {"url": url}},
        "optionCombinations": [{"sellerManagerCode": code} for code in codes],
    }
    if management is not None:
        product_node["sellerCodeInfo"] = {"sellerManagementCode": management}
    return {"originProduct": product_node}


def _compare(**kwargs: Any) -> readback.Comparison:
    contract = resolve(ORIGIN_READ)
    return readback.compare(payload(), retain(contract, _provider_body(**kwargs)))


def test_an_exact_read_back_matches_the_snapshot() -> None:
    result = _compare()
    assert (result.verdict, result.reasons) == (readback.ReadbackVerdict.MATCH, ())
    assert result.comparison_contract_version == "smartstore-readback-comparison/v4"
    assert result.normalizer_version == "smartstore-readback-normalizer/v3"
    # The read-back is compared against the projected provider code, exactly (R1).
    assert result.normalized["seller_management_code"] == SELLER_CODE


# ---------------------------------------------------------------- the published state (§11)


def _origin_read(*, sale: Any = "SALE", display: Any = "ON", **kwargs: Any) -> dict[str, Any]:
    """The documented origin-read envelope (Issue #89 5911962320): the sale status under
    ``originProduct``, the SmartStore display status under ``smartstoreChannelProduct``."""
    body = _provider_body(**kwargs)
    if sale is not None:
        body["originProduct"]["statusType"] = sale
    if display is not None:
        body["smartstoreChannelProduct"] = {"channelProductDisplayStatusType": display}
    return body


def _compare_origin(**kwargs: Any) -> readback.Comparison:
    return readback.compare(payload(), retain(resolve(ORIGIN_READ), _origin_read(**kwargs)))


def _expect(monkeypatch: pytest.MonkeyPatch, sale: str | None, display: str | None) -> None:
    """Give the comparison an explicit expectation, as an owner of the display status would."""
    monkeypatch.setattr(
        readback,
        "expected_published_state",
        lambda _payload: readback.ExpectedPublishedState(sale, display),
    )


def test_the_origin_read_keeps_the_two_status_leaves_and_the_channel_read_does_not() -> None:
    body = _origin_read()
    kept = retain(resolve(ORIGIN_READ), body)
    assert kept["originProduct"]["statusType"] == "SALE"
    assert kept["smartstoreChannelProduct"] == {"channelProductDisplayStatusType": "ON"}
    # The channel-product read response is not captured: nothing of it is claimed.
    channel = retain(resolve(CHANNEL_READ), body)
    assert "statusType" not in channel["originProduct"]
    assert "smartstoreChannelProduct" not in channel


def test_both_halves_are_read_at_their_documented_paths_only() -> None:
    contract = resolve(ORIGIN_READ)
    listing = readback.normalize(retain(contract, _origin_read(sale="SALE", display="ON")))
    assert (listing.sale_status, listing.display_status) == ("SALE", "ON")
    assert listing.canonical()["sale_status"] == "SALE"
    assert listing.canonical()["display_status"] == "ON"
    # A status that sits anywhere else is never taken for the documented member: the window
    # channel's display status, a status nested deeper, or an envelope around the product.
    elsewhere = _origin_read(sale=None, display=None)
    elsewhere["windowChannelProduct"] = {"channelProductDisplayStatusType": "ON"}
    elsewhere["groupProduct"] = {"statusType": "SALE"}
    elsewhere["originProduct"]["detail"] = {"statusType": "SALE"}
    listing = readback.normalize(retain(contract, elsewhere))
    assert (listing.sale_status, listing.display_status) == (None, None)
    wrapped = readback.normalize(retain(contract, {"result": _origin_read()}))
    assert (wrapped.sale_status, wrapped.display_status) == (None, None)


@pytest.mark.parametrize(
    ("sale", "display"),
    [("ON_SALE", "DISPLAYED"), ("sale", "on"), (1, True), ("", "")],
)
def test_a_value_outside_the_documented_enumerations_is_unreadable(sale: Any, display: Any) -> None:
    listing = readback.normalize(
        retain(resolve(ORIGIN_READ), _origin_read(sale=sale, display=display))
    )
    assert (listing.sale_status, listing.display_status) == (None, None)


def test_the_snapshot_expects_exactly_sale_and_on() -> None:
    # The expectation is what the Snapshot's own CREATE projection writes: SALE, the only CREATE
    # input, and ON, the owned display status (architect resolution 5915900049 D1).
    expected = readback.expected_published_state(payload())
    assert (expected.sale_status, expected.display_status, expected.complete) == (
        "SALE",
        "ON",
        True,
    )
    assert readback.reads_published_state() is True
    assert readback.proves_published_state() is True
    # A Snapshot that cannot be projected expects nothing, so nothing can be proven for it.
    broken = readback.expected_published_state({"items": []})
    assert (broken.sale_status, broken.display_status, broken.complete) == (None, None, False)


def test_sale_on_read_back_proves_the_published_state() -> None:
    result = _compare_origin()
    assert result.verdict is readback.ReadbackVerdict.MATCH, result.reasons
    assert result.normalized["published_state"] == "SALE/ON"


@pytest.mark.parametrize("display", ["SUSPENSION", "WAIT"])
def test_another_display_status_is_a_mismatch_and_proves_nothing(display: str) -> None:
    result = _compare_origin(display=display)
    assert result.verdict is readback.ReadbackVerdict.MISMATCH
    assert result.reasons == ("DISPLAY_STATUS_MISMATCH",)
    assert "published_state" not in result.normalized


@pytest.mark.parametrize(
    "missing",
    [{"sale": None}, {"display": None}, {"sale": None, "display": None}, {"display": "DISPLAYED"}],
    ids=["no-sale", "no-display", "neither", "unreadable-display"],
)
def test_a_missing_or_unreadable_half_proves_nothing(missing: dict[str, Any]) -> None:
    result = _compare_origin(**missing)
    assert result.verdict is readback.ReadbackVerdict.MATCH, result.reasons
    assert "published_state" not in result.normalized


@pytest.mark.parametrize("sale", ["OUTOFSTOCK", "WAIT", "SUSPENSION", "PROHIBITION", "DELETE"])
def test_a_sale_status_other_than_the_registered_one_is_a_mismatch(sale: str) -> None:
    result = _compare_origin(sale=sale)
    assert result.verdict is readback.ReadbackVerdict.MISMATCH
    assert result.reasons == ("SALE_STATUS_MISMATCH",)
    assert "published_state" not in result.normalized


def test_a_published_state_is_stated_only_when_both_halves_equal_an_explicit_expectation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _expect(monkeypatch, "SALE", "ON")
    proven = _compare_origin()
    assert proven.verdict is readback.ReadbackVerdict.MATCH
    assert proven.normalized["published_state"] == "SALE/ON"
    # The display status differs from the expected one: a mismatch of the listing.
    hidden = _compare_origin(display="SUSPENSION")
    assert hidden.reasons == ("DISPLAY_STATUS_MISMATCH",)
    assert "published_state" not in hidden.normalized
    # A half that did not come back is not a mismatch, and no state is stated.
    for missing in ({"sale": None}, {"display": None}, {"sale": None, "display": None}):
        unread = _compare_origin(**missing)
        assert unread.verdict is readback.ReadbackVerdict.MATCH, missing
        assert "published_state" not in unread.normalized
    # Any other mismatch never carries a published state either.
    renamed = _compare_origin(name="another name")
    assert renamed.verdict is readback.ReadbackVerdict.MISMATCH
    assert "published_state" not in renamed.normalized
    # Another expectation is proven by exactly its own state.
    _expect(monkeypatch, "SALE", "SUSPENSION")
    assert _compare_origin(display="SUSPENSION").normalized["published_state"] == "SALE/SUSPENSION"
    assert _compare_origin(display="ON").reasons == ("DISPLAY_STATUS_MISMATCH",)
    # An expectation that names only one half proves nothing.
    _expect(monkeypatch, "SALE", None)
    assert "published_state" not in _compare_origin().normalized


def test_normalization_is_deterministic_across_envelopes_and_orderings() -> None:
    contract = resolve(ORIGIN_READ)
    expected = readback.normalize(retain(contract, _provider_body()))
    # No envelope is proven, so the same leaves normalize identically however they are nested —
    # including an envelope that names neither originProduct nor channelProduct.
    nested = {"channelProducts": [_provider_body()]}
    flat = _provider_body()["originProduct"]
    other = {"result": {"data": [flat]}}
    # The stock quantity is the one leaf read at its documented path (normalizer v3): outside
    # ``originProduct`` it is not read at all.
    unplaced = replace(expected, stock_quantity=None)
    assert expected.stock_quantity == 1
    assert readback.normalize(retain(contract, nested)) == unplaced
    assert readback.normalize(retain(contract, dict(flat))) == unplaced
    assert readback.normalize(retain(contract, other)) == unplaced


def test_a_value_outside_a_proven_bound_is_not_a_number_this_contract_understands() -> None:
    # The read-back carries a price above the proven maximum: it is dropped, so the comparison
    # cannot silently accept it as the Snapshot's price.
    out_of_range = _compare(price=product.MAX_SALE_PRICE + 10)
    assert out_of_range.normalized["sale_price"] is None
    assert out_of_range.verdict is readback.ReadbackVerdict.MISMATCH
    assert "SALE_PRICE_MISMATCH" in out_of_range.reasons
    contract = resolve(ORIGIN_READ)
    huge_stock = readback.normalize(
        retain(contract, _provider_body(stock=product.MAX_STOCK_QUANTITY + 1))
    )
    assert huge_stock.stock_quantity is None


@pytest.mark.parametrize("stock", [0, 2, 10, None, True, "1"])
def test_a_read_back_stock_other_than_the_seed_is_a_mismatch(stock: Any) -> None:
    # D2.2: the seed the Snapshot's projection sent is compared exactly. A different, missing or
    # unreadable quantity is a mismatch for review, never a confirmation.
    result = _compare_origin(stock=stock)
    assert result.verdict is readback.ReadbackVerdict.MISMATCH
    assert result.reasons == ("STOCK_QUANTITY_MISMATCH",)
    assert "published_state" not in result.normalized


def test_the_stock_seed_is_read_only_at_the_origin_product() -> None:
    body = _origin_read()
    del body["originProduct"]["stockQuantity"]
    # An option row or another node carrying a stock quantity is never taken for the seed.
    body["originProduct"]["optionCombinations"][0]["stockQuantity"] = 1
    body["stockQuantity"] = 1
    result = readback.compare(payload(), retain(resolve(ORIGIN_READ), body))
    assert result.normalized["stock_quantity"] is None
    assert result.reasons == ("STOCK_QUANTITY_MISMATCH",)
    assert readback.expected_stock_quantity(payload()) == 1
    assert readback.expected_stock_quantity({"items": []}) is None


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"name": "다른 이름"}, "NAME_MISMATCH"),
        ({"price": 25900}, "SALE_PRICE_MISMATCH"),
        ({"management": "icbm-" + "f" * 32}, "SELLER_MANAGEMENT_CODE_MISMATCH"),
        ({"codes": (KEY_B,)}, "OPTION_UNITS_MISSING"),
        ({"url": "https://cdn.example/a.jpg?sig=abc"}, "IMAGE_REFERENCE_UNSAFE"),
    ],
)
def test_a_field_or_identity_difference_is_a_mismatch(kwargs: dict[str, Any], reason: str) -> None:
    result = _compare(**kwargs)
    assert result.verdict is readback.ReadbackVerdict.MISMATCH
    assert reason in result.reasons


def test_a_single_listing_read_back_missing_one_option_is_a_whole_listing_mismatch() -> None:
    frozen = payload(
        listing_shape="SINGLE_LISTING_WITH_OPTIONS",
        items=[_item(KEY_A, 19900, {"색상": "빨강"}), _item(KEY_B, 19900, {"색상": "파랑"})],
    )
    contract = resolve(ORIGIN_READ)
    result = readback.compare(frozen, retain(contract, _provider_body(codes=(KEY_A,))))
    # R3: the listing as a whole is a MISMATCH; the absent unit is never a second CREATE.
    assert result.verdict is readback.ReadbackVerdict.MISMATCH
    assert result.missing_option_codes == (KEY_B,)
    assert result.reasons == ("OPTION_UNITS_MISSING",)


def test_a_read_back_without_the_seller_code_is_unreadable_never_a_confirmation() -> None:
    result = _compare(management=None)
    assert result.verdict is readback.ReadbackVerdict.UNREADABLE
    assert result.reasons == ("READBACK_NO_SELLER_MANAGEMENT_CODE",)


def test_an_unsafe_provider_reference_never_enters_the_comparison_evidence() -> None:
    signed = "https://cdn.example/a.jpg?sig=abc"
    result = _compare(url=signed)
    evidence = json.dumps(result.canonical(), ensure_ascii=False)
    assert signed not in evidence
    assert result.normalized["unsafe_image_references"] == 1
    assert result.normalized["image_references"] == []


# ---------------------------------------------------------------- duplicate lookup (§13)


def test_duplicate_lookup_is_fail_closed_and_never_fabricates_no_match() -> None:
    source = lookup.SmartStoreDuplicateLookup()
    assert not source.available()
    with pytest.raises(lookup.DuplicateLookupUnavailableError) as refused:
        source.evidence(marketplace_account_id="mpa-1", listing_identity=IDENTITY)
    assert refused.value.code == "SMARTSTORE_DUPLICATE_LOOKUP_NOT_ADOPTED"
    # The refusal names the endpoint and stays a refusal: no verdict is invented from existence.
    assert "NO_MATCH" not in str(refused.value)


# ---------------------------------------------------------------- image upload outcome (§4)


def _promote(retained: dict[str, Any]) -> assets.UploadOutcome:
    return assets.promote(
        retained,
        asset_kind=ImageAssetKind.SOURCE_ASSET,
        sha256="1" * 64,
        derivation_id=None,
        asset_profile="asset-profile-1",
        candidate_fingerprint="c" * 64,
    )


def test_an_unambiguous_upload_becomes_exactly_one_prepared_asset() -> None:
    outcome = _promote({"images": [{"url": REF_MAIN}]})
    assert not outcome.ambiguous
    assert outcome.asset is not None
    assert outcome.asset.provider_asset_ref == REF_MAIN
    assert outcome.asset.candidate_fingerprint == "c" * 64
    assert not outcome.asset.unsafe()


@pytest.mark.parametrize(
    ("retained", "reason"),
    [
        ({}, "UPLOAD_NO_REFERENCE_RETURNED"),
        ({"images": []}, "UPLOAD_NO_REFERENCE_RETURNED"),
        ({"images": [{"url": REF_MAIN}, {"url": REF_DETAIL}]}, "UPLOAD_REFERENCE_NOT_UNIQUE"),
        # Review of 3484434: two references are two, even when they carry the same string. The
        # upload proved two, so one artifact's provider identity is not established.
        ({"images": [{"url": REF_MAIN}, {"url": REF_MAIN}]}, "UPLOAD_REFERENCE_NOT_UNIQUE"),
        (
            {"images": [{"url": REF_MAIN}], "thumbnails": [{"url": REF_MAIN}]},
            "UPLOAD_REFERENCE_NOT_UNIQUE",
        ),
        ({"images": [{"url": "https://cdn.example/a.jpg?sig=abc"}]}, "UPLOAD_REFERENCE_UNSAFE"),
    ],
)
def test_an_ambiguous_upload_never_becomes_a_provider_asset_identity(
    retained: dict[str, Any], reason: str
) -> None:
    outcome = _promote(retained)
    assert (outcome.ambiguous, outcome.asset, outcome.ambiguous_reason) == (True, None, reason)


def test_the_upload_request_names_one_artifact_only() -> None:
    request = assets.upload_request(
        access_token="fixture-token",
        credential_generation=3,
        session_generation=7,
        filename="artifact.png",
        media_type="image/png",
        content=b"png",
    )
    assert (request.filename, request.media_type, request.content) == (
        "artifact.png",
        "image/png",
        b"png",
    )


def _malformed_payloads() -> list[tuple[str, dict[str, Any]]]:
    """Every top-level key of a frozen payload, and of its Items, removed or given another type."""
    cases: list[tuple[str, dict[str, Any]]] = []
    base = payload()
    for key in base:
        for label, value in (
            ("missing", ...),
            ("none", None),
            ("list", []),
            ("text", "x"),
            ("int", 1),
        ):
            broken = deepcopy(base)
            if value is ...:
                del broken[key]
            else:
                broken[key] = value
            cases.append((f"{key}:{label}", broken))
    option_items = [_item(KEY_A, 19900, {"색상": "빨강"}), _item(KEY_B, 19900, {"색상": "파랑"})]
    for key in option_items[0]:
        for label, value in (("missing", ...), ("none", None), ("list", ["x"]), ("text", "x")):
            broken = payload(items=deepcopy(option_items))
            if value is ...:
                del broken["items"][0][key]
            else:
                broken["items"][0][key] = value
            cases.append((f"items[0].{key}:{label}", broken))
    # Nested values the projection reads: option names and values, notice field names and values.
    for label, options in (
        ("option-value-int", {"색상": 5}),
        ("option-value-empty", {"색상": " "}),
        ("option-value-mapping", {"색상": {"value": "빨강"}}),
        ("option-name-int", {1: "빨강"}),
    ):
        items = deepcopy(option_items)
        items[0]["options"] = options
        cases.append((f"items[0].options:{label}", payload(items=items)))
    for label, fields in (
        ("notice-field-name-int", {1: {"value": "면", "provenance": "SOURCE_FACT"}}),
        ("notice-field-value-int", {"material": {"value": 5, "provenance": "SOURCE_FACT"}}),
        ("notice-field-text", {"material": "면"}),
    ):
        broken = deepcopy(base)
        broken["notice"]["fields"] = fields
        cases.append((f"notice.fields:{label}", broken))
    return cases


@pytest.mark.parametrize(
    "options",
    [{"색상": 5}, {"색상": " "}, {"색상": {"value": "빨강"}}, {1: "빨강"}],
    ids=["int", "blank", "mapping", "int-name"],
)
def test_a_non_text_option_is_refused_never_coerced(options: dict[Any, Any]) -> None:
    # Option names and values are authored text carried verbatim; str() would turn a broken
    # Snapshot into a plausible display value on the wire.
    items = [_item(KEY_A, 19900, {"색상": "빨강"}), _item(KEY_B, 19900, {"색상": "파랑"})]
    items[0]["options"] = options
    with pytest.raises(product.WireContractError) as refused:
        product.project(payload(items=items))
    assert refused.value.code == "WIRE_VALUE_NOT_TEXT"


@pytest.mark.parametrize(("case", "broken"), _malformed_payloads(), ids=lambda v: str(v)[:40])
def test_a_malformed_snapshot_is_only_ever_a_wire_contract_refusal(
    case: str, broken: dict[str, Any]
) -> None:
    # The sender turns a WireContractError into one local, transmission-precluded refusal. Any
    # other exception would escape that path, so a malformed Snapshot — a key missing or of another
    # type, at the top level or in an Item — may only ever project, or refuse as a wire-contract
    # error; never a KeyError, TypeError or AttributeError.
    with contextlib.suppress(product.WireContractError):
        product.project(broken)


def test_a_detail_body_image_is_never_sent_as_a_gallery_image() -> None:
    # Issue #219 §2.2: until the detail composition places images, a detail-body image has no
    # place in the CREATE request; it is refused, never moved into optionalImages.
    detail = _asset("DETAIL", "3" * 64, "https://shop-phinf.pstatic.net/detail.jpg")
    item = {**_item(KEY_A, 19900)}
    item["publication_assets"] = [*item["publication_assets"], detail]
    with pytest.raises(product.WireContractError) as refused:
        product.project(payload(items=[item]))
    assert refused.value.code == "WIRE_DETAIL_IMAGE_NOT_PLACEABLE"
