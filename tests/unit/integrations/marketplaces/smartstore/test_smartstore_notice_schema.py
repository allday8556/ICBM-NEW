"""Notice coverage S1: the SmartStore provider notice schema as data.

The packaged ``notice_schema.json`` must be exactly what the two retained evidence files derive, its
loader must refuse an inconsistent document, and it must agree with the pinned CREATE table on every
child that table already projects.
"""

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from integrations.marketplaces.smartstore import notice_schema, product
from integrations.marketplaces.smartstore.notice_schema import (
    NoticePresence,
    NoticeSchemaError,
    NoticeValueType,
    derive_notice_schema,
    load_notice_schema,
    parse_notice_schema,
    render_notice_schema,
)

REPO_ROOT = Path(__file__).resolve().parents[5]
EVIDENCE = REPO_ROOT / "documents" / "evidence" / "marketplace-apis"
REFERENCE = EVIDENCE / "smartstore-notice-schema-2.90.0-2026-10-03.json"
LIVE_CAPTURE = EVIDENCE / "smartstore-notice-capture-2026-10-03.json"
DATA = Path(notice_schema.__file__).with_name(notice_schema.NOTICE_SCHEMA_FILE)

# The retained evidence is immutable: a changed file is a new capture and a new data revision.
REFERENCE_SHA256 = "3062870da85e7064e570d156a5cfd37ef531368a23be23d094187357a4f06ec9"
LIVE_CAPTURE_SHA256 = "8b79ccc252a96a5a61a54795a2cc4306baba2654beb0ad15d6a2ff7af9e17f38"

COMMON = (
    "returnCostReason",
    "noRefundReason",
    "qualityAssuranceStandard",
    "compensationProcedure",
    "troubleShootingContents",
)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _document() -> dict[str, Any]:
    return json.loads(DATA.read_text(encoding="utf-8"))


def test_the_evidence_is_the_retained_capture() -> None:
    assert hashlib.sha256(REFERENCE.read_bytes()).hexdigest() == REFERENCE_SHA256
    assert hashlib.sha256(LIVE_CAPTURE.read_bytes()).hexdigest() == LIVE_CAPTURE_SHA256


def test_the_packaged_data_is_exactly_the_derivation_of_the_evidence() -> None:
    derived = derive_notice_schema(_json(REFERENCE), _json(LIVE_CAPTURE))
    # Byte for byte: nothing in the data is written by hand.
    assert DATA.read_text(encoding="utf-8") == render_notice_schema(derived)


def test_the_schema_covers_every_documented_type_and_no_other() -> None:
    schema = load_notice_schema()
    assert schema.revision == "smartstore-notice-schema/2.90.0-r1"
    assert schema.api_version == "2.90.0"
    assert len(schema.notice_types) == 40
    # The four enum values without a documented child are enumerated, never given one.
    undocumented = {"LODGMENT_RESERVATION", "TRAVEL_PACKAGE", "AIRLINE_TICKET", "RENT_CAR"}
    assert set(schema.notice_types) - set(schema.types) == undocumented
    assert len(schema.types) == 36
    assert all(t.listed_live for t in schema.types.values())
    assert sum(len(t.fields) for t in schema.types.values()) == 597
    # The child mapping is the reference's own, the same one the CREATE table pins.
    assert {k: t.child for k, t in schema.types.items()} == dict(product.NOTICE_TYPE_MEMBERS)


def test_every_field_has_exactly_one_presence_rule_and_required_is_not_user_input() -> None:
    schema = load_notice_schema()
    fields = [f for t in schema.types.values() for f in t.fields]
    assert Counter(f.presence for f in fields) == {
        NoticePresence.REQUIRED: 318,
        NoticePresence.DETAIL_REFERENCE_DEFAULT: 180,
        NoticePresence.OPTIONAL: 35,
        NoticePresence.IF_APPLICABLE: 31,
        NoticePresence.REQUIRED_WITHOUT: 29,
        NoticePresence.ONE_OF: 3,
        NoticePresence.PROVIDER_DEFAULT: 1,
    }
    # The five fields every child repeats are badged required, yet omitting them is the provider's
    # own "상품상세 참조" default.
    for t in schema.types.values():
        for name in COMMON:
            common = t.field(name)
            assert common is not None and common.provider_required
            assert common.presence is NoticePresence.DETAIL_REFERENCE_DEFAULT


def test_value_types_include_non_text_and_an_unconfirmed_type_is_never_chosen() -> None:
    schema = load_notice_schema()
    fields = {(k, f.name): f for k, t in schema.types.items() for f in t.fields}
    assert Counter(f.value_type for f in fields.values()) == {
        NoticeValueType.TEXT: 560,
        NoticeValueType.YEAR_MONTH: 15,
        NoticeValueType.DATE: 12,
        NoticeValueType.BOOLEAN: 5,
        NoticeValueType.INTEGER: 1,
        NoticeValueType.LONG: 1,
        None: 3,
    }
    unconfirmed = {key for key, f in fields.items() if f.value_type is None}
    assert unconfirmed == {
        ("SEASON_APPLIANCES", "releaseDate"),
        ("OFFICE_APPLIANCES", "releaseDate"),
        ("SPORTS_EQUIPMENT", "releaseDate"),
    }
    for notice_type, name in unconfirmed:
        field = fields[(notice_type, name)]
        assert field.unconfirmed and "object" in field.unconfirmed
        assert field.presence is NoticePresence.OPTIONAL
        # Its text sibling stays a documented alternative.
        sibling = fields[(notice_type, "releaseDateText")]
        assert sibling.presence is NoticePresence.REQUIRED_WITHOUT
        assert sibling.required_without == ("releaseDate",)


def test_diet_food_carries_its_booleans_and_its_current_fields() -> None:
    diet = load_notice_schema().types["DIET_FOOD"]
    assert diet.child == "dietFood" and diet.name
    for name in ("geneticallyModified", "importDeclarationCheck"):
        field = diet.field(name)
        assert field is not None
        assert field.value_type is NoticeValueType.BOOLEAN
        assert field.presence is NoticePresence.REQUIRED
    # The 2022 specification's foodType / packDate are not in the current reference.
    assert diet.field("foodType") is None and diet.field("packDate") is None
    expiration = diet.field("expirationDate")
    assert expiration is not None and expiration.deprecated
    consumption = diet.field("consumptionDateText")
    assert consumption is not None and consumption.required_without == ("consumptionDate",)


def test_the_one_of_group_and_the_provider_default() -> None:
    schema = load_notice_schema()
    gift = schema.types["GIFT_CARD"]
    group = ("useStorePlace", "useStoreAddressId", "useStoreUrl")
    for name in group:
        field = gift.field(name)
        assert field is not None and field.one_of == group
    kitchen = schema.types["KITCHEN_UTENSILS"].field("importDeclaration")
    assert kitchen is not None
    assert kitchen.presence is NoticePresence.PROVIDER_DEFAULT
    assert kitchen.provider_default == "false"
    assert kitchen.value_type is NoticeValueType.BOOLEAN


def test_rental_ha_has_its_documented_child_beyond_the_live_list() -> None:
    rental = load_notice_schema().types["RENTAL_HA"]
    assert rental.child == "rentalHa"
    assert len(rental.fields) == 13
    assert sum(f.provider_required for f in rental.fields) == 10
    assert {f.name for f in rental.fields if f.listed_live} == {"maintenance", "specification"}


def _first_field(document: dict[str, Any], notice_type: str, name: str) -> dict[str, Any]:
    return next(f for f in document["types"][notice_type]["fields"] if f["name"] == name)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda d: d.update(revision="smartstore-notice-schema/other"), "NOTICE_SCHEMA_REVISION"),
        (
            lambda d: _first_field(d, "ETC", "itemName").update(presence="MANDATORY"),
            "NOTICE_SCHEMA_MALFORMED",
        ),
        (
            lambda d: _first_field(d, "ETC", "itemName").update(presence="OPTIONAL"),
            "NOTICE_SCHEMA_PRESENCE_MISMATCH",
        ),
        (
            lambda d: _first_field(d, "ETC", "customerServicePhoneNumber").update(
                required_without=["noSuchField"]
            ),
            "NOTICE_SCHEMA_DANGLING_REFERENCE",
        ),
        (
            lambda d: _first_field(d, "SEASON_APPLIANCES", "releaseDate").update(
                value_type="YEAR_MONTH"
            ),
            "NOTICE_SCHEMA_MALFORMED",
        ),
        (
            lambda d: d["types"]["ETC"].update(child="wear"),
            "NOTICE_SCHEMA_MALFORMED",
        ),
    ],
)
def test_an_inconsistent_document_is_refused(change: Any, code: str) -> None:
    document = _document()
    change(document)
    with pytest.raises(NoticeSchemaError) as refused:
        parse_notice_schema(document)
    assert refused.value.code == code


def test_a_type_outside_the_enumeration_is_refused() -> None:
    document = _document()
    document["types"]["NOT_A_TYPE"] = copy.deepcopy(document["types"]["ETC"]) | {"child": "x"}
    with pytest.raises(NoticeSchemaError) as refused:
        parse_notice_schema(document)
    assert refused.value.code == "NOTICE_TYPE_NOT_ENUMERATED"


def test_the_derivation_refuses_evidence_that_disagrees() -> None:
    reference, live = _json(REFERENCE), _json(LIVE_CAPTURE)
    lengthened = copy.deepcopy(live)
    lengthened["types"]["ETC"]["productInfoProvidedNoticeContents"][0]["fieldMaxLength"] = 1
    with pytest.raises(NoticeSchemaError) as refused:
        derive_notice_schema(reference, lengthened)
    assert refused.value.code == "NOTICE_LENGTH_DISAGREES"
    extra = copy.deepcopy(live)
    extra["types"]["ETC"]["productInfoProvidedNoticeContents"].append(
        {"fieldType": "String", "fieldName": "invented", "fieldMaxLength": 10}
    )
    with pytest.raises(NoticeSchemaError) as refused:
        derive_notice_schema(reference, extra)
    assert refused.value.code == "NOTICE_LIVE_FIELD_UNDOCUMENTED"
