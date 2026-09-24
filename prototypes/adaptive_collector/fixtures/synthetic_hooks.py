"""Synthetic pure hooks of the invented supplier ``synhook`` (ADR-0017 §6.2 shape).

Each returns a candidate or ``CANNOT_PARSE``. None emits a status or evidence, and none performs
I/O. ``HOOK_REVISION`` is the semantic revision; the implementation fingerprint is computed from
this file's bytes.
"""

import re
from typing import Any

from prototypes.adaptive_collector.hooks import CANNOT_PARSE

HOOK_REVISION = "synhook-hooks-1"
_ITEM = re.compile(r"^Item No\. ([A-Z]{3})/(\d{4})$")
_CONDITIONAL = re.compile(r"^(\d+)만원 이상 무료 / 미만 (\d{1,3}(?:,\d{3})*)원$")


def decode_item_number(text: str) -> Any:
    match = _ITEM.fullmatch(text)
    return f"{match.group(1)}-{match.group(2)}" if match else CANNOT_PARSE


def parse_conditional_shipping(text: str) -> Any:
    match = _CONDITIONAL.fullmatch(text)
    if match is None:
        return CANNOT_PARSE
    return {
        "kind": "CONDITIONAL",
        "policy_text": text,
        "fee_krw": int(match.group(2).replace(",", "")),
        "free_over_krw": int(match.group(1)) * 10000,
    }


HOOKS = {
    "decode_item_number": decode_item_number,
    "parse_conditional_shipping": parse_conditional_shipping,
}
