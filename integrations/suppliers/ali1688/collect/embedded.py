"""Reading the embedded-data member of a 1688 capture by its allowlisted paths (ADR-0040 §3, §4).

The member arrives already checked by the server against the capture policy's allowlist: every key
is an allowlisted name or a key of an allowlisted map, and every leaf has its declared type. This
module only walks it. A path that is not there is ``None`` — the page did not state it — and a fact
that needs it is ``REVIEW_REQUIRED``, never filled in.

Evidence for a fact read here is ``EMBEDDED_JSON`` (ADR-0010 §8; ADR-0040 §3 as amended) with the
locator ``embedded:<JSON path>``, where a map's own key is ``*``.
"""

from collections.abc import Mapping
from typing import Any

from integrations.suppliers.collection import DocumentView

ROOT = ("result", "data")
PRICE_MODEL = ("mainPrice", "fields", "priceModel")
UNIT = ("mainPrice", "fields", "unit")
SKU_MODEL = ("Root", "fields", "dataJson", "skuModel")
FREIGHT = ("shippingServices", "fields", "freightInfo")
OFFER_ID = ("gallery", "fields", "offerId")


def locator(*path: str) -> str:
    """The evidence locator of one allowlisted path under ``result.data``."""
    return "embedded:" + ".".join((*ROOT, *path))


def member(document: DocumentView) -> tuple[Mapping[str, Any] | None, str | None]:
    """The accepted member's ``result.data``, or why there is none.

    ``EMBEDDED_DATA_NOT_DECLARED`` is the answer for a document that came with no member at all (a
    capture policy that declares none, or a direct read, which 1688 never has).
    """
    embedded = document.embedded
    if embedded is None:
        return None, "EMBEDDED_DATA_NOT_DECLARED"
    if embedded.data is None:
        return None, embedded.refusal or "EMBEDDED_DATA_ABSENT"
    data = at(embedded.data, *ROOT)
    if not isinstance(data, Mapping):
        return None, "EMBEDDED_DATA_EMPTY"
    return data, None


def at(value: object, *path: str) -> Any:
    """The value at ``path`` below ``value``, or ``None`` where the member states nothing."""
    for step in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(step)
    return value
