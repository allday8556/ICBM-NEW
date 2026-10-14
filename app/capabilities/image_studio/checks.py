"""The deterministic checks of one recognised region and its Korean text (ADR-0041 §6; IT-05).

Pure functions, no I/O. A region is flagged, and needs its own confirmation, when:
- ``LOW_CONFIDENCE``: the OCR engine's confidence is below ``LOW_CONFIDENCE_THRESHOLD``;
- ``NUMBER_UNIT_DROPPED``: a digit, a decimal number, a number with its unit, a model number or
  a size specification of the source is not carried by the Korean text
  (``numeric_tokens_missing``);
- ``CONTACT_DETAIL``: the region holds a supplier's contact details (``looks_like_contact``): a
  phone number, a messenger id, an email address or a URL. Such a region defaults to
  keep-original and is never sent for translation unless the operator includes it.

Both texts are compared after NFKC normalisation, so fullwidth digits, letters, units and
separators (``３５０ｍｌ``, ``＊``, ``㎝``) read as their ASCII forms. A unit is compared by its
canonical name, whatever the script it is written in (``毫升``, ``ミリリットル``, ``밀리리터`` and
``ml`` are one unit). Chinese numerals count only directly before a unit (``三个``, ``两瓶``); a
lone ``一`` before a counter is an article and is not required. The comparison is conservative:
a Korean text that spells a number out (``세 개``) is flagged, and the operator confirms it.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

LOW_CONFIDENCE: Final = "LOW_CONFIDENCE"
NUMBER_UNIT_DROPPED: Final = "NUMBER_UNIT_DROPPED"
CONTACT_DETAIL: Final = "CONTACT_DETAIL"
# The flags of this module, in the order a region names them.
FLAGS: Final = (LOW_CONFIDENCE, NUMBER_UNIT_DROPPED, CONTACT_DETAIL)
# Below this OCR confidence a region is flagged (ADR-0041 §6).
LOW_CONFIDENCE_THRESHOLD: Final = 0.80

COUNT: Final = "count"
# Canonical unit → its spellings in Latin, Chinese, Japanese and Korean. Matching is longest first
# and case-insensitive, so ``ミリリットル`` is ml and never mm.
_UNITS: Final[dict[str, tuple[str, ...]]] = {
    "mm": ("mm", "毫米", "ミリメートル", "ミリ", "밀리미터", "밀리", "미리"),
    "cm": ("cm", "厘米", "公分", "センチメートル", "センチ", "센티미터", "센티", "센치"),
    "m": ("m", "米", "メートル", "미터"),
    "km": ("km", "公里", "キロメートル", "킬로미터"),
    "inch": ("inch", "英寸", "寸", "インチ", "인치"),
    "mg": ("mg", "毫克", "ミリグラム", "밀리그램"),
    "g": ("g", "克", "グラム", "그램"),
    "kg": ("kg", "公斤", "千克", "キログラム", "キロ", "킬로그램", "킬로"),
    "jin": ("斤", "근"),
    "ml": ("ml", "cc", "毫升", "ミリリットル", "밀리리터"),
    "l": ("l", "公升", "升", "リットル", "리터"),
    "%": ("%", "パーセント", "퍼센트", "프로"),
    "deg": ("°c", "°", "度", "도"),
    "w": ("w", "瓦", "ワット", "와트"),
    "kw": ("kw", "千瓦", "킬로와트"),
    "v": ("v", "伏", "ボルト", "볼트"),
    "mah": ("mah", "毫安时", "毫安", "밀리암페어"),
    "hz": ("hz", "赫兹", "ヘルツ", "헤르츠"),
    "gb": ("gb", "ギガ", "기가"),
    "tb": ("tb", "테라"),
    "mb": ("mb", "메가"),
    COUNT: (
        "pcs",
        "pc",
        "个",
        "個",
        "件",
        "只",
        "枚",
        "片",
        "包",
        "袋",
        "瓶",
        "盒",
        "箱",
        "本",
        "入",
        "粒",
        "支",
        "条",
        "双",
        "对",
        "套",
        "张",
        "張",
        "卷",
        "根",
        "セット",
        "ペア",
        "パック",
        "ピース",
        "개입",
        "개",
        "매",
        "장",
        "팩",
        "봉",
        "병",
        "박스",
        "입",
        "세트",
        "켤레",
        "쌍",
        "알",
        "정",
        "롤",
        "피스",
    ),
}
_CANONICAL: Final = {
    spelling.casefold(): unit for unit, spellings in _UNITS.items() for spelling in spellings
}


def _alternation(spellings: Sequence[str]) -> str:
    # A Latin unit must end the word: ``5 more`` is not 5 m, ``5mmx`` is not 5 mm.
    return "|".join(
        re.escape(s) + ("(?![a-z])" if s.isascii() and s[-1].isalpha() else "")
        for s in sorted(spellings, key=len, reverse=True)
    )


_UNIT: Final = _alternation(list(_CANONICAL))
_ASCII_UNIT: Final = _alternation([s for s in _CANONICAL if s.isascii()])
_NUMBER: Final = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_SIZE: Final = re.compile(
    rf"(?<![a-z0-9.])({_NUMBER})\s*[x×*]\s*({_NUMBER})(?:\s*[x×*]\s*({_NUMBER}))?"
    rf"(?:\s?({_UNIT}))?",
    re.IGNORECASE,
)
# A run of Latin letters and digits, with inner separators: a model number when it has both.
_RUN: Final = re.compile(r"(?<![a-z0-9])[a-z0-9]+(?:[-_/.][a-z0-9]+)*(?![a-z0-9])", re.IGNORECASE)
_MEASURE_RUN: Final = re.compile(rf"(?:{_NUMBER})\s?(?:{_ASCII_UNIT})", re.IGNORECASE)
_MEASURE: Final = re.compile(rf"(?<![\d.,])({_NUMBER})(?:\s?({_UNIT}))?", re.IGNORECASE)
_CJK_NUMERAL: Final = re.compile(rf"([零〇一二两兩三四五六七八九十百千万萬]+)\s?({_UNIT})")
_ANY_NUMBER: Final = re.compile(rf"(?<![\d.,])({_NUMBER})")
_SEPARATORS: Final = re.compile(r"[\s\-_/.]")

_CJK_DIGITS: Final = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CJK_SCALES: Final = {"十": 10, "百": 100, "千": 1000, "万": 10000, "萬": 10000}


@dataclass(frozen=True)
class _Token:
    """One thing the Korean text must carry: ``kind`` is size, model, measure or number."""

    kind: str
    key: tuple[str | None, ...]
    display: str


def _normalised(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _value(number: str) -> str:
    """A number by value: ``1,000`` and ``1000.0`` are ``1000``."""
    try:
        value = Decimal(number.replace(",", "")).normalize()
    except InvalidOperation:
        return number
    return format(value, "f")


def _unit(spelling: str | None) -> str | None:
    return None if spelling is None else _CANONICAL[spelling.casefold()]


def _cjk_value(numeral: str) -> int | None:
    total = section = number = 0
    seen = False
    for char in numeral:
        if char in _CJK_DIGITS:
            if seen and number:
                return None  # ``一二`` is not a number
            number, seen = _CJK_DIGITS[char], True
        elif _CJK_SCALES[char] < 10000:
            section += (number or 1) * _CJK_SCALES[char]
            number, seen = 0, False
        else:
            total += (section + number) * _CJK_SCALES[char]
            section = number = 0
            seen = False
    return total + section + number


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _tokens(text: str) -> list[_Token]:
    """What a text carries, in order of appearance within each kind: size specifications first,
    then model numbers, numbers with units and bare numbers, each removed once read."""
    rest = _normalised(text)
    tokens: list[_Token] = []
    for match in _SIZE.finditer(rest):
        dims = tuple(_value(d) for d in match.groups()[:3] if d is not None)
        tokens.append(_Token("size", (*dims, _unit(match.group(4))), match.group(0).strip()))
        rest = _blank(rest, *match.span())
    for match in _RUN.finditer(rest):
        run = match.group(0)
        if not (re.search(r"[a-z]", run, re.I) and re.search(r"\d", run)):
            continue
        if _MEASURE_RUN.fullmatch(run):
            continue
        tokens.append(_Token("model", (_SEPARATORS.sub("", run).casefold(),), run))
        rest = _blank(rest, *match.span())
    for match in _CJK_NUMERAL.finditer(rest):
        value, unit = _cjk_value(match.group(1)), _unit(match.group(2))
        if value is None or (value == 1 and unit == COUNT):
            continue
        tokens.append(_Token("measure", (str(value), unit), match.group(0)))
        rest = _blank(rest, *match.span())
    for match in _MEASURE.finditer(rest):
        unit = _unit(match.group(2))
        kind = "number" if unit is None else "measure"
        tokens.append(_Token(kind, (_value(match.group(1)), unit), match.group(0).strip()))
    return tokens


def numeric_tokens_missing(source: str, korean: str) -> tuple[str, ...]:
    """The digits, numbers with units, model numbers and size specifications of ``source`` that
    ``korean`` does not carry, as written in the source (NFKC), in order and without repeats. An
    empty answer means nothing was dropped."""
    carried = _tokens(korean)
    sizes = {t.key for t in carried if t.kind == "size"}
    measures = {t.key for t in carried if t.kind == "measure"}
    numbers = {_value(n) for n in _ANY_NUMBER.findall(_normalised(korean))}
    flat = _SEPARATORS.sub("", _normalised(korean)).casefold()
    missing: list[str] = []
    for token in _tokens(source):
        if token.kind == "size":
            found = token.key in sizes or (
                token.key[-1] is None and any(key[:-1] == token.key[:-1] for key in sizes)
            )
        elif token.kind == "model":
            found = str(token.key[0]) in flat
        elif token.kind == "measure":
            found = token.key in measures
        else:
            found = token.key[0] in numbers
        if not found and token.display not in missing:
            missing.append(token.display)
    return tuple(missing)


_CONTACT: Final = (
    # Email addresses and URLs.
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    re.compile(r"https?://|www\.", re.IGNORECASE),
    re.compile(
        r"(?<![a-z0-9.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|cn|net|jp|kr|co|org|shop|top|xyz)\b",
        re.IGNORECASE,
    ),
    # Messenger ids: WeChat, QQ, LINE, KakaoTalk, WhatsApp, Telegram, Aliwangwang, and an @id.
    re.compile(
        r"微\s*信|威信|薇信|v信|weixin|wechat|(?<![a-z])(?:vx|qq)(?![a-z])"
        r"|(?<![a-z])wx\s*[:号]|旺旺"
        r"|whatsapp|telegram|kakao|카카오톡|카톡|(?<![a-z])line\s*(?:id)?\s*[:@]|ライン\s*id",
        re.IGNORECASE,
    ),
    re.compile(r"(?<![a-z0-9_.@])@[a-z0-9_.-]{3,}", re.IGNORECASE),
    # Phone numbers: a phone word before digits, a CN or KR mobile, a separated or international
    # number (CN, JP, KR landlines and mobiles).
    re.compile(
        r"(?:电话|電話|手机|手機|热线|熱線|(?<![a-z])tel|(?<![a-z])phone|☎|📞|フリーダイヤル"
        r"|전화|연락처)"
        r"\s*[:：]?\s*\+?[\d][\d\s-]{5,}",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)01[016789]-?\d{3,4}-?\d{4}(?!\d)"),
    re.compile(r"(?<!\d)0\d{1,4}[-\s]\d{1,4}[-\s]\d{3,4}(?!\d)"),
    re.compile(r"\+(?:86|81|82)[\s-]?\d[\d\s-]{6,}"),
)


def looks_like_contact(text: str) -> bool:
    """Whether a region holds a supplier's contact details (ADR-0041 §6): such a region defaults
    to keep-original and is not sent for translation unless the operator includes it."""
    normalised = _normalised(text)
    return any(pattern.search(normalised) for pattern in _CONTACT)


def region_flags(text: str, confidence: float, korean: str | None) -> tuple[str, ...]:
    """The flags of one region (``FLAGS`` order). ``korean`` is the text proposed for it, or
    ``None`` when it has none (kept, or not translated): only a proposed text can drop a number."""
    flags = []
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        flags.append(LOW_CONFIDENCE)
    if korean is not None and numeric_tokens_missing(text, korean):
        flags.append(NUMBER_UNIT_DROPPED)
    if looks_like_contact(text):
        flags.append(CONTACT_DETAIL)
    return tuple(flags)
