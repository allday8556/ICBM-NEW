"""ADR-0041 §6, I4: the deterministic checks of a recognised region and its Korean text."""

import pytest

from app.capabilities.image_studio.checks import (
    CONTACT_DETAIL,
    LOW_CONFIDENCE,
    LOW_CONFIDENCE_THRESHOLD,
    NUMBER_UNIT_DROPPED,
    looks_like_contact,
    numeric_tokens_missing,
    region_flags,
)
from app.capabilities.image_studio.contracts import OcrRegion, sent_regions

# ---------------------------------------------------------------- numbers, units, models, sizes


@pytest.mark.parametrize(
    ("source", "korean"),
    [
        # Chinese: a quantity with its unit, in either script.
        ("净含量350ml", "용량 350ml"),
        ("净含量350毫升", "용량 350밀리리터"),
        ("重量5公斤", "무게 5kg"),
        ("重量5公斤", "무게 5킬로그램"),
        ("一百五十克", "150g"),
        ("三个装", "3개입"),
        ("两瓶", "2병"),
        ("100%纯棉", "면 100%"),
        ("32寸", "32인치"),
        ("2024年新款", "2024년 신상"),
        ("1,000个", "1000개"),
        # Japanese, and fullwidth digits and units.
        ("サイズ30センチ", "사이즈 30cm"),
        ("内容量500ミリリットル", "내용량 500ml"),
        ("３５０ｍｌ", "350mL"),
        ("１．５Ｌ", "1.5리터"),
        ("温度45℃", "온도 45°C"),
        # Model numbers and sizes, however they are separated.
        ("型号:AB-1234", "모델 AB-1234"),
        ("型号:AB-1234", "모델 ab1234"),
        ("5000mAh大容量", "5000mAh 대용량"),
        ("4K高清", "4K 고화질"),
        ("尺寸:30*40cm", "사이즈 30×40cm"),
        ("尺寸30x40", "30 × 40 cm"),
        ("30×40×50mm", "30x40x50mm"),
        # Nothing numeric, or an article.
        ("纯天然芝麻油", "순수 천연 참기름"),
        ("一个好产品", "좋은 제품"),
    ],
)
def test_a_carried_number_unit_model_or_size_is_not_missing(source: str, korean: str) -> None:
    assert numeric_tokens_missing(source, korean) == ()


@pytest.mark.parametrize(
    ("source", "korean", "missing"),
    [
        ("净含量350ml", "순 함량", ("350ml",)),
        ("净含量350ml", "순 함량 350", ("350ml",)),
        ("重量5公斤", "무게 5g", ("5公斤",)),
        ("三个装", "세 개", ("三个",)),
        ("两瓶", "병", ("两瓶",)),
        ("サイズ30センチ", "사이즈 30", ("30センチ",)),
        ("３５０ｍｌ", "350리터", ("350ml",)),
        ("１．５Ｌ", "1500ml", ("1.5L",)),
        ("型号:AB-1234", "모델명", ("AB-1234",)),
        ("型号:AB-1234", "모델 AB-1243", ("AB-1234",)),
        ("尺寸:30*40cm", "사이즈 30x40", ("30*40cm",)),
        ("尺寸:30*40cm", "사이즈 30x45cm", ("30*40cm",)),
        ("2024年新款", "신상", ("2024",)),
        ("型号X5 容量2L 共3个", "모델 X5 용량 2L", ("3个",)),
        ("7天无理由 7天", "무료 반품", ("7",)),
    ],
)
def test_a_dropped_number_unit_model_or_size_is_named(
    source: str, korean: str, missing: tuple[str, ...]
) -> None:
    assert numeric_tokens_missing(source, korean) == missing


# ---------------------------------------------------------------- contact details


@pytest.mark.parametrize(
    "text",
    [
        "微信:shop8888",
        "加微 信 abc123",
        "VX同号",
        "wx:shop_01",
        "QQ:123456",
        "QQ群 99887766",
        "LINE ID：shop_jp",
        "@shop_cn 关注",
        "电话:021-12345678",
        "手机 13812345678",
        "13812345678",
        "Tel 03-1234-5678",
        "090-1234-5678",
        "010-1234-5678",
        "+86 138 1234 5678",
        "shop@example.com",
        "https://shop.example.cn/item",
        "www.shop.com",
        "店铺taobao.com",
        "旺旺:shop",
        "ＱＱ：１２３４５６",
    ],
)
def test_a_contact_detail_is_recognised(text: str) -> None:
    assert looks_like_contact(text)


@pytest.mark.parametrize(
    "text",
    [
        "纯天然芝麻油",
        "净含量350ml",
        "型号AB-1234",
        "尺寸30*40cm",
        "6901234567890",
        "2024年新款",
        "厂家直销",
        "包邮 7天无理由退货",
        "高品质 line art",
    ],
)
def test_product_text_is_not_a_contact_detail(text: str) -> None:
    assert not looks_like_contact(text)


def test_a_contact_detail_is_kept_unless_included() -> None:
    regions = (
        OcrRegion("r0", (0, 0, 9, 9), "纯天然芝麻油", "zh", 0.9, 0.9),
        OcrRegion("r1", (0, 10, 9, 19), "微信:shop8888", "zh", 0.9, 0.9),
    )
    assert [r.region_id for r in sent_regions(regions)] == ["r0"]
    assert [r.region_id for r in sent_regions(regions, included=frozenset({"r1"}))] == ["r0", "r1"]
    # An exclusion still wins.
    kept = sent_regions(regions, frozenset({"r1"}), frozenset({"r1"}))
    assert [r.region_id for r in kept] == ["r0"]


# ---------------------------------------------------------------- a region's flags


def test_a_region_names_each_of_its_flags_in_order() -> None:
    assert region_flags("纯天然芝麻油", 0.95, "순수 천연 참기름") == ()
    assert region_flags("纯天然芝麻油", LOW_CONFIDENCE_THRESHOLD, None) == ()
    assert region_flags("纯天然芝麻油", LOW_CONFIDENCE_THRESHOLD - 0.01, None) == (LOW_CONFIDENCE,)
    assert region_flags("净含量350ml", 0.95, "순 함량") == (NUMBER_UNIT_DROPPED,)
    # With no Korean text, nothing can have been dropped.
    assert region_flags("净含量350ml", 0.95, None) == ()
    assert region_flags("电话 13812345678", 0.5, "전화") == (
        LOW_CONFIDENCE,
        NUMBER_UNIT_DROPPED,
        CONTACT_DETAIL,
    )
