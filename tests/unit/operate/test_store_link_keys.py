"""The keys and signals of ADR-0042 §7 (S2a): pure values, synthetic inputs only.

- a seller code is a strong key after trim and case, from four characters;
- a barcode is a strong key only as a valid GTIN, and a UPC-A equals its EAN-13;
- a similar name is a share of common words, never a link;
- a capacity, count or pack conflict is two different amounts of one dimension.
"""

from decimal import Decimal

import pytest

from app.stages.operate.catalog.keys import (
    GTIN,
    SELLER_CODE,
    amounts,
    conflicts,
    gtin_key,
    name_similarity,
    name_tokens,
    seller_code_key,
    strong_keys,
)


@pytest.mark.parametrize(
    ("raw", "key"),
    [(" KM287 ", "km287"), ("km287", "km287"), ("AB1", None), ("   ", None), (None, None)],
)
def test_a_seller_code_is_compared_after_trim_and_case(raw: str | None, key: str | None) -> None:
    assert seller_code_key(raw) == key


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("8801234567893", "08801234567893"),  # EAN-13
        ("036000291452", "00036000291452"),  # UPC-A
        ("0036000291452", "00036000291452"),  # the same UPC-A as an EAN-13
        ("96385074", "00000096385074"),  # EAN-8
        ("8801234567890", None),  # a wrong check digit
        ("88012345678", None),  # no GTIN length
        ("88O1234567893", None),  # not digits only
        ("８８０１２３４５６７８９３", None),  # full-width digits are not a barcode
        (None, None),
    ],
)
def test_a_barcode_is_a_key_only_as_a_valid_gtin(raw: str | None, key: str | None) -> None:
    assert gtin_key(raw) == key


def test_strong_keys_name_their_kind() -> None:
    keys = strong_keys(("KM287", None, "x"), ("8801234567893", "bad"))
    assert keys == frozenset({(SELLER_CODE, "km287"), (GTIN, "08801234567893")})


def test_a_similar_name_is_a_share_of_common_words() -> None:
    left = name_tokens("[국산] 참기름 500ml 2병 선물세트")
    right = name_tokens("국산 참기름 500ml 2병 선물")
    assert left == frozenset({"국산", "참기름", "500ml", "2병", "선물세트"})
    assert name_similarity(left, right) == pytest.approx(4 / 6)
    assert name_similarity(left, frozenset()) == 0.0


def test_a_conflict_is_two_different_amounts_of_one_dimension() -> None:
    assert amounts(["생들기름 1L", None]) == {"volume": frozenset({Decimal(1000)})}
    assert conflicts(amounts(["생들기름 1L"]), amounts(["생들기름 350ml"])) == ("volume",)
    assert conflicts(amounts(["참기름 1000ml"]), amounts(["참기름 1L"])) == ()
    assert conflicts(amounts(["깨 500g 2개입"]), amounts(["깨 0.5kg 3개"])) == ("count",)
    # A dimension only one side states is no conflict.
    assert conflicts(amounts(["참기름 2병"]), amounts(["참기름 300ml"])) == ()
