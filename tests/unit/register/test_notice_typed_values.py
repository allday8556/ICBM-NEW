"""Notice coverage S2: typed notice values, typed field rules and their Snapshot.

A notice value keeps its own JSON type (text, boolean, integer) and is never coerced; a field rule
states the value type, whether omitting the field is the marketplace's documented default, and the
conditions under which it is required. ``required`` is the marketplace's badge, not "the operator
must type a value".
"""

from dataclasses import replace
from typing import Any

import pytest

from app.platform.core.errors import InputValidationError
from app.stages.products.model import ReadinessStatus
from app.stages.register.category_metadata import (
    RecordRevisionRequest,
    category_metadata_of,
    encode_content,
)
from app.stages.register.execution import decode_field, encode_field
from app.stages.register.payload import build_payload
from app.stages.register.policy import FieldRule, FieldValueType, NoticePolicy
from app.stages.register.preparation import FieldValue
from integrations.marketplaces.smartstore import product
from integrations.marketplaces.smartstore.product import WireContractError
from tests.unit.register.test_m5_preflight_rules import (
    LISTING,
    METADATA,
    candidate,
    codes,
    final,
    request,
    resolved,
)

_T = FieldValueType

TYPED = replace(
    METADATA,
    notice=NoticePolicy(
        "notice-type-typed",
        (
            FieldRule("productName", required=True, max_length=20),
            FieldRule("returnCostReason", required=True, omitted_default=True),
            FieldRule("geneticallyModified", required=True, value_type=_T.BOOLEAN),
            FieldRule("importDeclaration", required=False, value_type=_T.BOOLEAN),
            FieldRule("periodDays", required=False, value_type=_T.INTEGER),
            FieldRule("releaseDate", required=False, value_type=_T.YEAR_MONTH),
            FieldRule("releaseDateText", required=False, required_without=("releaseDate",)),
            FieldRule("consumptionDate", required=False, value_type=_T.DATE),
            FieldRule("useStorePlace", required=False, one_of=("useStorePlace", "useStoreUrl")),
            FieldRule("useStoreUrl", required=False, one_of=("useStorePlace", "useStoreUrl")),
        ),
    ),
)

TYPED_NOTICES: dict[str, FieldValue] = {
    "productName": FieldValue("텀블러"),
    "geneticallyModified": FieldValue(False),
    "periodDays": FieldValue(30),
    "releaseDate": FieldValue("2026-09"),
    "consumptionDate": FieldValue("2027-01-31"),
    "useStoreUrl": FieldValue("store-page"),
}


def _evaluate(notices: dict[str, FieldValue]) -> Any:
    return candidate(request(listing=replace(LISTING, notices=notices)), resolved(metadata=TYPED))


def _reasons(notices: dict[str, FieldValue]) -> set[tuple[str, str]]:
    return {(r.code, r.subject) for r in _evaluate(notices).reasons}


def test_typed_values_are_ready_and_frozen_with_their_own_json_type() -> None:
    assert _evaluate(TYPED_NOTICES).status is ReadinessStatus.READY
    req = request(listing=replace(LISTING, notices=TYPED_NOTICES))
    fields = build_payload(final(req, resolved(metadata=TYPED))).payload["notice"]["fields"]
    assert fields["geneticallyModified"]["value"] is False
    assert fields["periodDays"]["value"] == 30 and type(fields["periodDays"]["value"]) is int
    assert fields["releaseDate"]["value"] == "2026-09"
    # Nothing is filled in: the field whose omission is the marketplace default stays absent.
    assert "returnCostReason" not in fields and "importDeclaration" not in fields


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("geneticallyModified", FieldValue("false")),
        ("geneticallyModified", FieldValue(0)),
        ("periodDays", FieldValue("30")),
        ("periodDays", FieldValue(True)),
        ("productName", FieldValue(True)),
        ("releaseDate", FieldValue(202609)),
    ],
)
def test_a_value_of_another_type_is_never_coerced(key: str, value: FieldValue) -> None:
    assert _reasons({**TYPED_NOTICES, key: value}) == {
        ("FIELD_VALUE_TYPE_MISMATCH", f"notice:{key}")
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("releaseDate", "2026-13"),
        ("releaseDate", "2026-9"),
        ("releaseDate", "2026-09-01"),
        ("consumptionDate", "2027-02-30"),
        ("consumptionDate", "2027-01"),
    ],
)
def test_a_text_value_outside_its_documented_form_is_never_reformatted(
    key: str, value: str
) -> None:
    assert _reasons({**TYPED_NOTICES, key: FieldValue(value)}) == {
        ("FIELD_VALUE_FORM_INVALID", f"notice:{key}")
    }


def test_an_integer_outside_a_signed_64_bit_value_is_invalid() -> None:
    assert _reasons({**TYPED_NOTICES, "periodDays": FieldValue(2**63)}) == {
        ("FIELD_VALUE_FORM_INVALID", "notice:periodDays")
    }


def test_a_required_field_whose_omission_is_the_marketplace_default_is_never_missing() -> None:
    assert "returnCostReason" not in TYPED_NOTICES
    assert _reasons(TYPED_NOTICES) == set()
    # A required field without that default is still missing — a boolean included.
    without = {k: v for k, v in TYPED_NOTICES.items() if k != "geneticallyModified"}
    assert _reasons(without) == {("NOTICE_REQUIRED_MISSING", "notice:geneticallyModified")}


def test_a_field_required_without_another_is_missing_only_when_both_are_absent() -> None:
    neither = {k: v for k, v in TYPED_NOTICES.items() if k != "releaseDate"}
    assert _reasons(neither) == {("NOTICE_REQUIRED_MISSING", "notice:releaseDateText")}
    text_only = {**neither, "releaseDateText": FieldValue("상세페이지 참조 아님")}
    assert _reasons(text_only) == set()


def test_a_one_of_group_is_missing_once_and_only_when_none_is_present() -> None:
    none = {k: v for k, v in TYPED_NOTICES.items() if k != "useStoreUrl"}
    assert _reasons(none) == {("NOTICE_REQUIRED_MISSING", "notice:useStorePlace")}
    both = {**TYPED_NOTICES, "useStorePlace": FieldValue("매장")}
    assert _reasons(both) == set()


def test_the_json_type_of_a_value_is_part_of_the_fingerprint() -> None:
    def fingerprint(key: str, value: FieldValue) -> str:
        notices = {**TYPED_NOTICES, key: value}
        result = candidate(
            request(listing=replace(LISTING, notices=notices)), resolved(metadata=TYPED)
        )
        return result.candidate_fingerprint

    assert fingerprint("periodDays", FieldValue(30)) != fingerprint("periodDays", FieldValue("30"))
    assert fingerprint("geneticallyModified", FieldValue(True)) != fingerprint(
        "geneticallyModified", FieldValue(1)
    )
    # A listing of text values only is unchanged by any of this.
    text = candidate(request(), resolved())
    assert text.status is ReadinessStatus.READY
    assert codes(text) == set()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"required_without": ("k",)},
        {"one_of": ("other", "third")},
        {"one_of": ("k",)},
        {"value_type": _T.BOOLEAN, "max_length": 10},
        {"value_type": _T.DATE, "detail_page_reference_allowed": True},
    ],
)
def test_an_inconsistent_field_rule_is_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        FieldRule("k", required=False, **kwargs)


def test_a_recorded_value_round_trips_with_its_type_and_nothing_else_is_decoded() -> None:
    for value in ("텍스트", True, False, 30):
        assert decode_field(encode_field(FieldValue(value))).value == value
        assert type(decode_field(encode_field(FieldValue(value))).value) is type(value)
    with pytest.raises(ValueError):
        decode_field(
            {"value": ["a"], "provenance": "OPERATOR_CONFIRMED", "detail_page_reference": False}
        )


# ---------------------------------------------------------------- the reviewed metadata document


def _rule(key: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "key": key,
        "required": False,
        "detail_page_reference_allowed": False,
        "missing_status": "REVIEW_REQUIRED",
        "max_length": None,
    }
    values.update(overrides)
    return values


def _save(fields: list[dict[str, Any]]) -> dict[str, Any]:
    body = RecordRevisionRequest.model_validate(
        {
            "actor": "operator-1",
            "expected_current_revision": None,
            "content_provenance": "OPERATOR_CONFIRMED",
            "evidence_reference": "seller-center/notice/2026-10-03",
            "reviewed": True,
            "content": {
                "leaf": True,
                "registrable": True,
                "name_max_length": 100,
                "attributes": [],
                "notice": {"notice_type": "DIET_FOOD", "fields": fields},
                "options": {"options_supported": False, "max_options": 1, "max_dimensions": 1},
                "required_templates": [],
            },
        }
    )
    return encode_content("smartstore", "taxonomy-1", "cat-1", body)


def test_a_text_rule_is_stored_exactly_as_before_and_a_typed_rule_round_trips() -> None:
    text = _rule("productName", required=True, max_length=200)
    typed = [
        text,
        _rule("returnCostReason", required=True, omitted_default=True),
        _rule("geneticallyModified", required=True, value_type="BOOLEAN"),
        _rule("consumptionDate", value_type="DATE"),
        _rule("consumptionDateText", required_without=["consumptionDate"]),
    ]
    document = _save(typed)
    stored = document["notice"]["fields"]
    # A rule saying nothing new is the v1 shape, key for key.
    assert stored[0] == text
    assert stored[1]["omitted_default"] is True and "value_type" not in stored[1]
    assert stored[2]["value_type"] == "BOOLEAN"
    assert stored[4]["required_without"] == ["consumptionDate"]
    notice = category_metadata_of("meta-1", True, document).notice
    assert notice is not None
    rules = {rule.key: rule for rule in notice.fields}
    assert rules["geneticallyModified"].value_type is FieldValueType.BOOLEAN
    assert rules["returnCostReason"].omitted_default
    assert rules["consumptionDateText"].required_without == ("consumptionDate",)


@pytest.mark.parametrize(
    "fields",
    [
        [_rule("a", required_without=["missing"])],
        [_rule("a", one_of=["a", "b"]), _rule("b")],
        [_rule("a", value_type="BOOLEAN", max_length=5)],
        [_rule("a", value_type="DATE", detail_page_reference_allowed=True)],
    ],
)
def test_an_inconsistent_typed_rule_is_refused_whole(fields: list[dict[str, Any]]) -> None:
    with pytest.raises(InputValidationError):
        _save(fields)


# ---------------------------------------------------------------- the wire, until S3


def test_the_create_projection_still_refuses_a_non_text_notice_value() -> None:
    # The CREATE table projects text members only; typed members are aligned in S3, never guessed.
    req = request(listing=replace(LISTING, notices=TYPED_NOTICES))
    payload = build_payload(final(req, resolved(metadata=TYPED))).payload
    with pytest.raises(WireContractError) as refused:
        product.project(payload)
    assert refused.value.code == "WIRE_VALUE_NOT_TEXT"
