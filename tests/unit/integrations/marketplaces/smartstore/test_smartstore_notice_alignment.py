"""Notice coverage S3: metadata ↔ preflight ↔ wire read one SmartStore notice contract.

The REGISTER notice rules of a SmartStore category are the provider notice schema's own for the
reviewed notice type; the CREATE projection validates the notice child against the same schema,
with every value in its own JSON type.
"""

from dataclasses import replace
from typing import Any

import pytest

from app.stages.products.model import ReadinessStatus
from app.stages.register.policy import (
    FieldValueType,
    NoticePolicy,
    notice_aligned,
)
from app.stages.register.preparation import FieldValue
from integrations.marketplaces.smartstore import product
from integrations.marketplaces.smartstore.notice_schema import (
    SmartStoreNoticeRules,
    emittable,
    load_notice_schema,
    notice_policy,
)
from tests.unit.integrations.marketplaces.smartstore.test_m5_register_adapter import payload
from tests.unit.register.test_m5_preflight_rules import (
    LISTING,
    METADATA,
    candidate,
    request,
    resolved,
)

RULES = SmartStoreNoticeRules()


def _value(value: Any) -> dict[str, Any]:
    return {"value": value, "provenance": "OPERATOR_CONFIRMED"}


def _detail() -> dict[str, Any]:
    return {"detail_page_reference": True, "provenance": "OPERATOR_CONFIRMED"}


# ---------------------------------------------------------------- the derived REGISTER rules


def test_every_documented_type_derives_rules_and_an_undocumented_one_is_marked() -> None:
    schema = load_notice_schema()
    for notice_type in schema.notice_types:
        policy = notice_policy(notice_type)
        assert policy.contract == "smartstore-notice-schema/2.90.0-r1"
        if notice_type not in schema.types:
            assert policy.documented is False and policy.fields == ()
            continue
        sendable = [f.name for f in schema.types[notice_type].fields if emittable(f)]
        # Exactly the emittable fields, in the schema's order.
        assert [rule.key for rule in policy.fields] == sendable


def test_required_is_never_user_input_for_a_field_the_provider_fills() -> None:
    rules = {rule.key: rule for rule in notice_policy("DIET_FOOD").fields}
    common = rules["returnCostReason"]
    assert common.required and common.omitted_default and common.detail_page_reference_allowed
    genetically = rules["geneticallyModified"]
    assert genetically.value_type is FieldValueType.BOOLEAN
    assert genetically.required and not genetically.omitted_default
    # Deprecated fields are never declared, so never sent.
    assert "expirationDate" not in rules and "expirationDateText" not in rules
    assert rules["consumptionDateText"].required_without == ("consumptionDate",)
    kitchen = {rule.key: rule for rule in notice_policy("KITCHEN_UTENSILS").fields}
    assert (
        kitchen["importDeclaration"].omitted_default and not kitchen["importDeclaration"].required
    )


def test_an_unconfirmed_field_is_never_declared_and_its_alternative_becomes_required() -> None:
    for notice_type in ("SEASON_APPLIANCES", "OFFICE_APPLIANCES", "SPORTS_EQUIPMENT"):
        rules = {rule.key: rule for rule in notice_policy(notice_type).fields}
        assert "releaseDate" not in rules
        text = rules["releaseDateText"]
        assert text.required and text.required_without == ()
    # Elsewhere releaseDate is confirmed, and the pair stays conditional.
    image = {rule.key: rule for rule in notice_policy("IMAGE_APPLIANCES").fields}
    assert image["releaseDate"].value_type is FieldValueType.YEAR_MONTH
    assert image["releaseDateText"].required_without == ("releaseDate",)


def test_the_gift_card_one_of_group_and_the_integer_fields() -> None:
    rules = {rule.key: rule for rule in notice_policy("GIFT_CARD").fields}
    group = ("useStorePlace", "useStoreAddressId", "useStoreUrl")
    assert all(rules[name].one_of == group for name in group)
    assert rules["useStoreAddressId"].value_type is FieldValueType.INTEGER
    assert rules["periodDays"].value_type is FieldValueType.INTEGER
    assert rules["periodDays"].max_length is None


# ---------------------------------------------------------------- metadata alignment


def test_the_contract_replaces_hand_recorded_notice_fields_of_a_governed_marketplace() -> None:
    recorded = replace(METADATA, notice=NoticePolicy("ETC", METADATA.notice.fields))  # type: ignore[union-attr]
    aligned = notice_aligned(RULES, "smartstore", recorded)
    assert aligned is not None and aligned.notice == notice_policy("ETC")
    # Everything else is the reviewed metadata, unchanged.
    assert replace(aligned, notice=recorded.notice) == recorded
    # Another marketplace, and metadata without a notice, are returned exactly as held.
    assert notice_aligned(RULES, "another", recorded) is recorded
    no_notice = replace(METADATA, notice=None)
    assert notice_aligned(RULES, "smartstore", no_notice) is no_notice
    assert notice_aligned(None, "smartstore", recorded) is recorded
    with pytest.raises(ValueError):
        RULES.notice_policy("another", "ETC")


def test_an_undocumented_notice_type_is_blocked_in_the_preflight() -> None:
    blocked = replace(METADATA, notice=notice_policy("RENT_CAR"))
    result = candidate(request(listing=replace(LISTING, notices={})), resolved(metadata=blocked))
    assert result.status is ReadinessStatus.BLOCKED
    assert ("NOTICE_TYPE_UNDOCUMENTED", "notice:RENT_CAR") in {
        (r.code, r.subject) for r in result.reasons
    }


def test_the_contract_revision_is_part_of_the_fingerprint() -> None:
    notices = {
        "itemName": FieldValue("텀블러"),
        "modelName": FieldValue("TB-500"),
        "manufacturer": FieldValue("KM통상"),
        "customerServicePhoneNumber": FieldValue("02-000-0000"),
    }
    policy = notice_policy("ETC")
    listing = replace(LISTING, notices=notices)
    governed = candidate(
        request(listing=listing), resolved(metadata=replace(METADATA, notice=policy))
    )
    assert governed.status is ReadinessStatus.READY, governed.reasons
    other = replace(policy, contract="smartstore-notice-schema/other")
    moved = candidate(request(listing=listing), resolved(metadata=replace(METADATA, notice=other)))
    assert governed.candidate_fingerprint != moved.candidate_fingerprint


# ---------------------------------------------------------------- the CREATE notice child


def _diet_food(**fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "productName": _value("비타민C 1000"),
        "producer": _value("KM헬스"),
        "location": _value("대한민국"),
        "consumptionDate": _value("2027-01-31"),
        "storageMethod": _value("직사광선을 피해 보관"),
        "weight": _value("60g"),
        "amount": _value("60정"),
        "ingredients": _value("비타민C 100%"),
        "nutritionFacts": _value("비타민C 1000mg"),
        "specification": _value("항산화"),
        "cautionAndSideEffect": _value("1일 1정"),
        "nonMedicinalUsesMessage": _value("질병의 예방 및 치료를 위한 의약품이 아닙니다"),
        "geneticallyModified": _value(False),
        "importDeclarationCheck": _value(False),
        "consumerSafetyCaution": _value("어린이 손이 닿지 않는 곳에 보관"),
        "customerServicePhoneNumber": _value("02-000-0000"),
        "returnCostReason": _detail(),
    }
    values.update(fields)
    return {
        "notice_type": "DIET_FOOD",
        "fields": {key: value for key, value in values.items() if value is not None},
    }


def _child(projected: Any, key: str) -> dict[str, Any]:
    notice = projected.document.mapping()["originProduct"]["detailAttribute"]
    return dict(notice["productInfoProvidedNotice"][key])


def test_a_diet_food_notice_with_its_booleans_is_sendable() -> None:
    projected = product.project(payload(notice=_diet_food()))
    assert projected.sendable is True
    child = _child(projected, "dietFood")
    assert child["geneticallyModified"] is False and child["importDeclarationCheck"] is False
    assert child["consumptionDate"] == "2027-01-31"
    # Left to the provider's own "상품상세 참조"; never filled in.
    assert "returnCostReason" not in child


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"geneticallyModified": _value("false")}, "WIRE_DOCUMENT_VALUE_INVALID"),
        ({"geneticallyModified": _value(0)}, "WIRE_DOCUMENT_VALUE_INVALID"),
        ({"productName": _value(True)}, "WIRE_DOCUMENT_VALUE_INVALID"),
        ({"consumptionDate": _value("2027-02-30")}, "WIRE_DOCUMENT_VALUE_INVALID"),
        ({"consumptionDate": _value("2027-01")}, "WIRE_DOCUMENT_VALUE_INVALID"),
        ({"geneticallyModified": None}, "WIRE_DOCUMENT_FIELD_MISSING"),
        ({"consumptionDate": None}, "WIRE_DOCUMENT_FIELD_MISSING"),
        ({"expirationDate": _value("2027-01-31")}, "WIRE_DOCUMENT_FIELD_UNKNOWN"),
        ({"foodType": _value("건강기능식품")}, "WIRE_DOCUMENT_FIELD_UNKNOWN"),
        ({"productName": _value("x" * 201)}, "WIRE_DOCUMENT_VALUE_INVALID"),
    ],
    ids=[
        "boolean-as-text",
        "boolean-as-integer",
        "text-as-boolean",
        "impossible-date",
        "year-month-for-date",
        "required-boolean-missing",
        "conditional-pair-missing",
        "deprecated-field",
        "field-of-the-2022-specification-only",
        "too-long",
    ],
)
def test_a_diet_food_notice_outside_its_schema_is_refused(
    fields: dict[str, Any], code: str
) -> None:
    with pytest.raises(product.WireContractError) as refused:
        product.project(payload(notice=_diet_food(**fields)))
    assert refused.value.code == code


def test_the_text_alternative_of_a_conditional_date_is_accepted() -> None:
    projected = product.project(
        payload(
            notice=_diet_food(consumptionDate=None, consumptionDateText=_value("제조일로부터 2년"))
        )
    )
    child = _child(projected, "dietFood")
    assert "consumptionDate" not in child and child["consumptionDateText"] == "제조일로부터 2년"


def _gift_card(**fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "issuer": _value("KM상품권"),
        "termsOfUse": _value("유효기간 경과 시 70% 환급"),
        "refundPolicy": _value("잔액 60% 이상 사용 시 환급"),
        "customerServicePhoneNumber": _value("02-000-0000"),
        "periodDays": _value(365),
        "useStoreAddressId": _value(1234567890123),
    }
    values.update(fields)
    return {
        "notice_type": "GIFT_CARD",
        "fields": {key: value for key, value in values.items() if value is not None},
    }


def test_a_gift_card_notice_carries_its_integers_and_needs_one_store() -> None:
    child = _child(product.project(payload(notice=_gift_card())), "giftCard")
    assert child["periodDays"] == 365 and child["useStoreAddressId"] == 1234567890123
    with pytest.raises(product.WireContractError) as refused:
        product.project(payload(notice=_gift_card(useStoreAddressId=None)))
    assert refused.value.code == "WIRE_DOCUMENT_FIELD_MISSING"
    url_only = _gift_card(useStoreAddressId=None, useStoreUrl=_value("store-page"))
    assert product.project(payload(notice=url_only)).sendable is True
    # periodDays is a 32-bit integer; useStoreAddressId a 64-bit one.
    for fields in ({"periodDays": _value(2**31)}, {"useStoreAddressId": _value(2**63)}):
        with pytest.raises(product.WireContractError) as refused:
            product.project(payload(notice=_gift_card(**fields)))
        assert refused.value.code == "WIRE_DOCUMENT_VALUE_INVALID"


def test_an_unconfirmed_release_date_is_never_sent() -> None:
    fields = {
        "itemName": _value("에어컨"),
        "modelName": _value("AC-1"),
        "certificationType": _value("KC"),
        "ratedVoltage": _value("220V"),
        "powerConsumption": _value("1kW"),
        "energyEfficiencyRating": _value("1등급"),
        "manufacturer": _value("KM전자"),
        "size": _value("100x50x30"),
        "area": _value("18평"),
        "installedCharge": _value("없음"),
        "warrantyPolicy": _value("1년"),
        "afterServiceDirector": _value("KM전자 02-000-0000"),
        "releaseDateText": _value("2026년 9월"),
    }
    notice = {"notice_type": "SEASON_APPLIANCES", "fields": fields}
    assert product.project(payload(notice=notice)).sendable is True
    with_release = {**fields, "releaseDate": _value("2026-09")}
    with pytest.raises(product.WireContractError) as refused:
        product.project(
            payload(notice={"notice_type": "SEASON_APPLIANCES", "fields": with_release})
        )
    assert refused.value.code == "WIRE_DOCUMENT_FIELD_UNKNOWN"
    without_text = {k: v for k, v in fields.items() if k != "releaseDateText"}
    with pytest.raises(product.WireContractError) as refused:
        product.project(
            payload(notice={"notice_type": "SEASON_APPLIANCES", "fields": without_text})
        )
    assert refused.value.code == "WIRE_DOCUMENT_FIELD_MISSING"
