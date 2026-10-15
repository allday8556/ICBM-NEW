"""The Korean names the REGISTER screens show for SmartStore notice fields (2026-10-15).

They are the packaged provider notice schema's own labels, exactly: every field of every
documented type has one, written in Korean, and nothing is named for another marketplace or for
a type the schema does not document.
"""

import re

from integrations.marketplaces.smartstore.notice_schema import load_notice_schema, notice_labels

HANGUL = re.compile(r"[가-힣]")


def test_every_documented_field_has_the_schemas_own_korean_label() -> None:
    schema = load_notice_schema()
    assert schema.types
    for notice_type, documented in schema.types.items():
        name, labels = notice_labels("smartstore", notice_type)
        assert name == documented.name and HANGUL.search(name), notice_type
        assert dict(labels) == {field.name: field.label for field in documented.fields}
        for key, label in labels.items():
            assert HANGUL.search(label), (notice_type, key)
            assert label != key


def test_the_diet_food_fields_the_usability_check_named_read_in_korean() -> None:
    name, labels = notice_labels("smartstore", "DIET_FOOD")
    assert name is not None
    assert labels["productName"] == "제품명"
    assert (
        labels["nonMedicinalUsesMessage"]
        == "질병의 예방 및 치료를 위한 의약품이 아니라는 내용의 문구"
    )
    assert labels["producer"] == "제조업소"


def test_nothing_is_named_outside_the_schema() -> None:
    assert notice_labels("coupang", "DIET_FOOD") == (None, {})
    assert notice_labels("smartstore", "NOT_A_TYPE") == (None, {})
