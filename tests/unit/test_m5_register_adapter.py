"""M5 PR-D: the typed SmartStore REGISTER adapter — the wire projection of a frozen Snapshot, the
retention profile, the read-back normalizer/comparison, duplicate lookup and asset promotion.

Every provider fact these tests pin comes from the architect-supplied 2.89.0 packet (Issue #89
comment 5746489554). Nothing here performs I/O: the adapter is pure, and the transport contract of
the adopted read-backs is pinned separately in ``test_smartstore_product_reads.py``.
"""

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest

from app.products.image_model import ImageAssetKind
from integrations.marketplaces.smartstore import assets, lookup, product, readback
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
            _asset("DETAIL", "2" * 64, REF_DETAIL),
        ],
    }


def payload(**overrides: Any) -> dict[str, Any]:
    """A frozen Snapshot payload in the exact shape ``app.register.payload`` builds."""
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
    assert projected.encoding_version == "smartstore-register-wire/v2"
    assert projected.document == {
        "originProduct": {
            "name": "테스트 상품",
            "detailContent": "본문",
            "images": {
                "representativeImage": {"url": REF_MAIN},
                "optionalImages": [{"url": REF_DETAIL}],
            },
            "salePrice": 19900,
            # ICBM-side: the 2.89.0 schema does not mark leafCategoryId required, and the value is
            # the operator-reviewed category id emitted verbatim.
            "leafCategoryId": "cat-1",
            "detailAttribute": {"sellerCodeInfo": {"sellerManagementCode": SELLER_CODE}},
        }
    }
    assert projected.image_references == (REF_MAIN, REF_DETAIL)
    # The reviewed notice the Snapshot owns is carried as evidence, never emitted: no notice type
    # child is captured, so nothing of it may be placed on the wire.
    assert projected.notice_type == "Wear2023"
    assert projected.notice_fields == {"material": "면 100%"}
    # The request is not sendable: the official evidence captures neither the accepted values of
    # the required statusType, nor the value type of naverShoppingRegistration, nor a notice child,
    # and no Snapshot owns a channel display status. None of them is ever invented.
    assert not projected.sendable
    assert set(projected.gaps) == {
        product.GAP_STATUS_TYPE,
        product.GAP_NOTICE_TYPE_CHILD,
        product.GAP_SHOPPING_REGISTRATION,
        product.GAP_CHANNEL_DISPLAY_STATUS,
    }


def test_the_projection_never_emits_a_value_the_evidence_does_not_carry() -> None:
    text = json.dumps(product.project(payload()).document, ensure_ascii=False)
    for never in (
        "statusType",
        "naverShoppingRegistration",
        "channelProductDisplayStatusType",
        "smartstoreChannelProduct",
        # A separate Shopping Window channel structure, out of the SmartStore-only scope.
        "windowChannelProduct",
        "productInfoProvidedNotice",
        # Unowned optional structures are omitted rather than defaulted (delivery, stock, A/S).
        "deliveryInfo",
        "stockQuantity",
        "afterServiceInfo",
        "originAreaInfo",
    ):
        assert never not in text


def test_the_projection_is_deterministic_for_the_same_snapshot() -> None:
    first, second = product.project(payload()), product.project(deepcopy(payload()))
    assert json.dumps(first.document, sort_keys=True) == json.dumps(second.document, sort_keys=True)
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
                "publication_assets": [_asset("DETAIL", "2" * 64, REF_DETAIL)],
            }
        ]
    )
    with pytest.raises(product.WireContractError) as refused:
        product.project(details)
    assert refused.value.code == "WIRE_REPRESENTATIVE_IMAGE_MISSING"


def test_more_images_than_the_provider_allows_is_refused() -> None:
    many = [_asset("REPRESENTATIVE", "1" * 64, REF_MAIN)] + [
        _asset("DETAIL", f"{i}" * 64, f"https://shop-phinf.example/a/{i}.jpg") for i in range(10)
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
    option_info = projected.document["originProduct"]["detailAttribute"]["optionInfo"]
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
) -> dict[str, Any]:
    """A provider read-back shaped as an envelope the packet does not prove, so the normalizer
    must recognize the proven leaves wherever they sit."""
    product_node: dict[str, Any] = {
        "name": name,
        "salePrice": price,
        "stockQuantity": 10,
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
    assert result.comparison_contract_version == "smartstore-readback-comparison/v1"
    assert result.normalizer_version == "smartstore-readback-normalizer/v1"
    # The read-back is compared against the projected provider code, exactly (R1).
    assert result.normalized["seller_management_code"] == SELLER_CODE


def test_normalization_is_deterministic_across_envelopes_and_orderings() -> None:
    contract = resolve(ORIGIN_READ)
    expected = readback.normalize(retain(contract, _provider_body()))
    # No envelope is proven, so the same leaves normalize identically however they are nested —
    # including an envelope that names neither originProduct nor channelProduct.
    nested = {"channelProducts": [_provider_body()]}
    flat = _provider_body()["originProduct"]
    other = {"result": {"data": [flat]}}
    assert readback.normalize(retain(contract, nested)) == expected
    assert readback.normalize(retain(contract, dict(flat))) == expected
    assert readback.normalize(retain(contract, other)) == expected


def test_a_value_outside_a_proven_bound_is_not_a_number_this_contract_understands() -> None:
    # The read-back carries a price above the proven maximum: it is dropped, so the comparison
    # cannot silently accept it as the Snapshot's price.
    out_of_range = _compare(price=product.MAX_SALE_PRICE + 10)
    assert out_of_range.normalized["sale_price"] is None
    assert out_of_range.verdict is readback.ReadbackVerdict.MISMATCH
    assert "SALE_PRICE_MISMATCH" in out_of_range.reasons
    contract = resolve(ORIGIN_READ)
    huge_stock = readback.normalize(
        retain(contract, _provider_body() | {"stockQuantity": product.MAX_STOCK_QUANTITY + 1})
    )
    assert huge_stock.stock_quantity is None


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
