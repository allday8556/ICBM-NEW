"""ADR-0038 §6 (O3): the SmartStore CREATE projection of a frozen 구매대행 Snapshot, pure.

The input is a frozen payload in the shape ``app.stages.register.payload`` builds, with the
``overseas`` section (``overseas-listing/v1``). Proven: each projected member, its source and its
rule; every uncaptured member stays a named gap; a section that is not exactly the frozen one is
refused, never trimmed; and a Snapshot without the section projects exactly as under v10.
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
    assert projected.encoding_version == "smartstore-register-wire/v11"


def test_the_overseas_address_is_the_shipping_address_and_nothing_else_moves() -> None:
    domestic = product.project(payload(policy=deepcopy(POLICY))).document.mapping()
    overseas = product.project(foreign()).document.mapping()
    claim = overseas["originProduct"]["deliveryInfo"]["claimDeliveryInfo"]
    assert claim["shippingAddressId"] == 200441299
    # Only the shipping address and the customs term differ from the domestic projection.
    domestic["originProduct"]["deliveryInfo"]["claimDeliveryInfo"]["shippingAddressId"] = 200441299
    domestic["originProduct"]["detailAttribute"]["customsTaxType"] = "INCLUDED"
    assert overseas == domestic


def test_the_uncaptured_cross_border_members_are_named_gaps_never_sent() -> None:
    projected = product.project(foreign())
    assert projected.gaps[-3:] == product.OVERSEAS_GAPS
    assert not projected.sendable
    text = projected.document.canonical_json
    for never in (
        "businessCustomsClearanceSaleYn",
        "kcCertifiedProductExclusionYn",
        "kcExemptionType",
        "KC_EXEMPTION_OBJECT",
        "importer",
        "invented importer",
    ):
        assert never not in text, never
    # No import code is guessed: the origin stays the v8 detail-reference code.
    attribute = projected.document.mapping()["originProduct"]["detailAttribute"]
    assert attribute["originAreaInfo"] == {"originAreaCode": product.ORIGIN_AREA_DETAIL_CODE}


def test_the_document_carries_no_unknown_member_for_a_foreign_listing() -> None:
    body = product.project(foreign()).document.mapping()
    body["originProduct"]["deliveryInfo"]["businessCustomsClearanceSaleYn"] = False
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
