"""ADR-0038 §6 (O3): the SmartStore CREATE projection of a frozen 구매대행 Snapshot, pure.

The input is a frozen payload in the shape ``app.stages.register.payload`` builds, with the
``overseas`` section (``overseas-listing/v1``). Proven: each projected member, its source and its
rule; every uncaptured member stays a named gap; a section that is not exactly the frozen one is
refused, never trimmed; and a Snapshot without the section projects exactly as under v10.

O3b (wire v12): the business-clearance flag and the 구매대행 KC exemption are projected from the
captured 2.90.1 reference, they travel only together with a 구매대행 customs term, and the import
origin code and the 구매대행 certification information stay named gaps.
"""

from copy import deepcopy
from typing import Any

import pytest

from integrations.marketplaces.smartstore import product
from tests.unit.integrations.marketplaces.smartstore.test_m5_register_adapter import (
    IDENTITY,
    payload,
)

DELIVERY: dict[str, Any] = {
    "delivery_type": "DELIVERY",
    "delivery_attribute_type": "NORMAL",
    "delivery_company": "CJGLS",
    "delivery_fee_type": "PAID",
    "base_fee_krw": 3000,
    "delivery_fee_pay_type": "PREPAID",
    "return_delivery_company_priority_type": "PRIMARY",
    "return_delivery_fee_krw": 3000,
    "exchange_delivery_fee_krw": 6000,
    "shipping_address_id": 200441202,
    "return_address_id": 200441203,
}
POLICY: dict[str, Any] = {
    "policy_revision": "policy-1",
    "templates": {},
    "after_service_telephone": "02-000-0000",
    "delivery_policy": DELIVERY,
}


def section(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "version": "overseas-listing/v1",
        "source_currency": "CNY",
        "source_country": "CN",
        "duty_decisions": ["NOT_OVER_150"],
        "shipping_address_id": 200441299,
        "importer": "invented importer",
        "settings_revision_no": 1,
        "settings_fingerprint": "f" * 64,
    }
    values.update(overrides)
    return values


def foreign(**overrides: Any) -> dict[str, Any]:
    return payload(policy=deepcopy(POLICY), overseas=section(**overrides))


def test_a_domestic_snapshot_projects_exactly_as_before() -> None:
    domestic = product.project(payload(policy=deepcopy(POLICY)))
    origin = domestic.document.mapping()["originProduct"]
    assert origin["detailAttribute"]["customsTaxType"] == product.REGISTRATION_CUSTOMS_TAX_TYPE
    assert origin["deliveryInfo"]["claimDeliveryInfo"]["shippingAddressId"] == 200441202
    assert not set(product.OVERSEAS_GAPS) & set(domestic.gaps)
    for never in (
        "businessCustomsClearanceSaleYn",
        "certificationTargetExcludeContent",
        "kcCertifiedProductExclusionYn",
        "kcExemptionType",
    ):
        assert never not in domestic.document.canonical_json, never


@pytest.mark.parametrize(
    ("decisions", "term"),
    [
        (["NOT_OVER_150"], "INCLUDED"),
        (["INCLUDED"], "INCLUDED"),
        (["INCLUDED", "NOT_OVER_150"], "INCLUDED"),
        (["EXCLUDED"], "EXCLUDED"),
    ],
)
def test_customs_tax_type_is_the_pinned_duty_term(decisions: list[str], term: str) -> None:
    projected = product.project(foreign(duty_decisions=decisions))
    attribute = projected.document.mapping()["originProduct"]["detailAttribute"]
    assert attribute["customsTaxType"] == term
    assert projected.encoding_version == "smartstore-register-wire/v12"


def test_the_overseas_address_is_the_shipping_address_and_nothing_else_moves() -> None:
    domestic = product.project(payload(policy=deepcopy(POLICY))).document.mapping()
    overseas = product.project(foreign()).document.mapping()
    claim = overseas["originProduct"]["deliveryInfo"]["claimDeliveryInfo"]
    assert claim["shippingAddressId"] == 200441299
    # Only the shipping address, the customs term, the business-clearance flag and the KC
    # exemption differ from the domestic projection.
    domestic["originProduct"]["deliveryInfo"]["claimDeliveryInfo"]["shippingAddressId"] = 200441299
    domestic["originProduct"]["detailAttribute"]["customsTaxType"] = "INCLUDED"
    domestic["originProduct"]["deliveryInfo"]["businessCustomsClearanceSaleYn"] = False
    domestic["originProduct"]["detailAttribute"]["certificationTargetExcludeContent"] = {
        "kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT",
        "kcExemptionType": "OVERSEAS",
    }
    assert overseas == domestic


def test_the_captured_cross_border_members_are_projected_exactly() -> None:
    projected = product.project(foreign(duty_decisions=["EXCLUDED"]))
    origin = projected.document.mapping()["originProduct"]
    # NAVER-P0-CROSS-BORDER-2901: a JSON boolean under deliveryInfo, false for 구매대행.
    assert origin["deliveryInfo"][product.FIELD_BUSINESS_CUSTOMS_CLEARANCE_SALE_YN] is False
    # The 구매대행 pair of the two captured string enums, under certificationTargetExcludeContent.
    exemption = origin["detailAttribute"][product.FIELD_CERTIFICATION_TARGET_EXCLUDE_CONTENT]
    assert exemption == {
        product.FIELD_KC_CERTIFIED_PRODUCT_EXCLUSION_YN: "KC_EXEMPTION_OBJECT",
        product.FIELD_KC_EXEMPTION_TYPE: "OVERSEAS",
    }
    assert exemption[product.FIELD_KC_CERTIFIED_PRODUCT_EXCLUSION_YN] in (
        product.KC_CERTIFIED_PRODUCT_EXCLUSION_VALUES
    )
    assert exemption[product.FIELD_KC_EXEMPTION_TYPE] in product.KC_EXEMPTION_TYPE_VALUES
    assert origin["detailAttribute"]["customsTaxType"] == "EXCLUDED"
    # The document is still the validated canonical one.
    assert product.verified(projected.document) is projected.document


@pytest.mark.parametrize(
    ("where", "member", "value"),
    [
        ("deliveryInfo", "businessCustomsClearanceSaleYn", True),
        ("deliveryInfo", "businessCustomsClearanceSaleYn", "false"),
        ("deliveryInfo", "businessCustomsClearanceSaleYn", 0),
        ("deliveryInfo", "businessCustomsClearanceSaleYn", None),
        (
            "detailAttribute",
            "certificationTargetExcludeContent",
            {"kcCertifiedProductExclusionYn": "TRUE", "kcExemptionType": "OVERSEAS"},
        ),
        (
            "detailAttribute",
            "certificationTargetExcludeContent",
            {
                "kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT",
                "kcExemptionType": "SAFE_CRITERION",
            },
        ),
        (
            "detailAttribute",
            "certificationTargetExcludeContent",
            {
                "kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT",
                "kcExemptionType": "PARALLEL_IMPORT",
            },
        ),
    ],
)
def test_only_the_purchase_agency_values_are_accepted(where: str, member: str, value: Any) -> None:
    body = product.project(foreign()).document.mapping()
    body["originProduct"][where][member] = value
    with pytest.raises(product.WireContractError) as refused:
        product.create_document(IDENTITY, body)
    assert refused.value.code == "WIRE_DOCUMENT_VALUE_INVALID"


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT"},
        {"kcExemptionType": "OVERSEAS"},
        {
            "kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT",
            "kcExemptionType": "OVERSEAS",
            "childCertifiedProductExclusionYn": False,
        },
        "KC_EXEMPTION_OBJECT",
    ],
)
def test_the_kc_exemption_is_exactly_the_pair(broken: Any) -> None:
    body = product.project(foreign()).document.mapping()
    body["originProduct"]["detailAttribute"]["certificationTargetExcludeContent"] = broken
    with pytest.raises(product.WireContractError) as refused:
        product.create_document(IDENTITY, body)
    assert refused.value.code in {
        "WIRE_DOCUMENT_FIELD_MISSING",
        "WIRE_DOCUMENT_FIELD_UNKNOWN",
        "WIRE_DOCUMENT_MALFORMED",
    }


@pytest.mark.parametrize(
    "drop",
    [
        ("deliveryInfo", "businessCustomsClearanceSaleYn"),
        ("detailAttribute", "certificationTargetExcludeContent"),
    ],
)
def test_the_cross_border_members_travel_together(drop: tuple[str, str]) -> None:
    body = product.project(foreign()).document.mapping()
    del body["originProduct"][drop[0]][drop[1]]
    with pytest.raises(product.WireContractError) as refused:
        product.create_document(IDENTITY, body)
    assert refused.value.code == "WIRE_OVERSEAS_MEMBERS_INCOHERENT"


@pytest.mark.parametrize(
    ("where", "member", "value"),
    [
        ("deliveryInfo", "businessCustomsClearanceSaleYn", False),
        (
            "detailAttribute",
            "certificationTargetExcludeContent",
            {"kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT", "kcExemptionType": "OVERSEAS"},
        ),
    ],
)
def test_a_domestic_document_never_carries_a_cross_border_member(
    where: str, member: str, value: Any
) -> None:
    body = product.project(payload(policy=deepcopy(POLICY))).document.mapping()
    body["originProduct"][where][member] = value
    with pytest.raises(product.WireContractError) as refused:
        product.create_document(IDENTITY, body)
    assert refused.value.code == "WIRE_OVERSEAS_MEMBERS_INCOHERENT"


def test_the_uncaptured_cross_border_members_are_named_gaps_never_sent() -> None:
    projected = product.project(foreign())
    assert projected.gaps[-2:] == product.OVERSEAS_GAPS
    assert not projected.sendable
    text = projected.document.canonical_json
    for never in ("importer", "invented importer", "productCertificationInfos"):
        assert never not in text, never
    # No import code is guessed: the origin stays the v8 detail-reference code.
    attribute = projected.document.mapping()["originProduct"]["detailAttribute"]
    assert attribute["originAreaInfo"] == {"originAreaCode": product.ORIGIN_AREA_DETAIL_CODE}


@pytest.mark.parametrize(
    ("where", "member", "value"),
    [
        ("detailAttribute", "productCertificationInfos", []),
        ("originAreaInfo", "importer", "invented importer"),
        ("originAreaInfo", "content", "중국"),
        ("deliveryInfo", "outboundLocationId", "x"),
    ],
)
def test_the_document_carries_no_unknown_member_for_a_foreign_listing(
    where: str, member: str, value: Any
) -> None:
    body = product.project(foreign()).document.mapping()
    target = body["originProduct"]
    if where == "originAreaInfo":
        target = target["detailAttribute"]
    target[where][member] = value
    with pytest.raises(product.WireContractError) as refused:
        product.create_document(IDENTITY, body)
    assert refused.value.code == "WIRE_DOCUMENT_FIELD_UNKNOWN"


@pytest.mark.parametrize(
    "broken",
    [
        {"version": "overseas-listing/v0"},
        {"shipping_address_id": 0},
        {"shipping_address_id": "200441299"},
        {"shipping_address_id": True},
        {"shipping_address_id": 2**63},
        {"importer": " "},
        {"importer": None},
        {"source_currency": ""},
        {"duty_decisions": []},
        {"duty_decisions": ["MAYBE"]},
        {"duty_decisions": "INCLUDED"},
    ],
)
def test_a_section_that_is_not_the_frozen_one_is_refused(broken: dict[str, Any]) -> None:
    with pytest.raises(product.WireContractError) as refused:
        product.project(foreign(**broken))
    assert refused.value.code == "WIRE_OVERSEAS_MALFORMED"


def test_an_extra_or_missing_section_key_is_refused() -> None:
    extra = foreign()
    extra["overseas"]["kc"] = "OVERSEAS"
    missing = foreign()
    del missing["overseas"]["importer"]
    for broken in (extra, missing):
        with pytest.raises(product.WireContractError) as refused:
            product.project(broken)
        assert refused.value.code == "WIRE_OVERSEAS_MALFORMED"


def test_disagreeing_duty_terms_are_refused_never_averaged() -> None:
    with pytest.raises(product.WireContractError) as refused:
        product.project(foreign(duty_decisions=["EXCLUDED", "INCLUDED"]))
    assert refused.value.code == "WIRE_OVERSEAS_DUTIES_INCONSISTENT"


def test_a_foreign_snapshot_without_a_delivery_policy_is_refused() -> None:
    bare = payload(overseas=section())
    with pytest.raises(product.WireContractError) as refused:
        product.project(bare)
    assert refused.value.code == "WIRE_OVERSEAS_DELIVERY_MISSING"
