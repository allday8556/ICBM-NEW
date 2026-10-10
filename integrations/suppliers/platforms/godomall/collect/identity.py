"""What identifies a Godomall product at its source (ADR-0030 §4; 건강산's reconnaissance).

**The rule.** The identity is the ``goodsNo`` the page declares in its ``og:url``, which must
address the product page itself (``/goods/goods_view.php``). The page must agree with itself
everywhere it states the number:
- the product URL's own ``goodsNo`` query value;
- the ``상품코드`` row of its information list.

All are digit strings and all must be equal. Each was observed on every reconnaissance capture.
The order form's ``goodsNo[]`` inputs are not read: the browser capture keeps no input value
(ADR-0019 §6.1).

**When it cannot be read.** A missing or malformed number, a corroboration the page does not make,
or two declarations that disagree all leave the identity *unresolved*. The caller then records no
revision at all. A site cannot replace this rule (ADR-0030 PT-04).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from integrations.suppliers.collection import DocumentView
from integrations.suppliers.platforms.godomall.collect.dom import Node, meta, read
from integrations.suppliers.platforms.godomall.collect.facts import rows

IDENTITY_KEY = "goodsNo"
PRODUCT_PATH = "/goods/goods_view.php"
CODE_LABEL = "상품코드"

UNRESOLVED_ABSENT = "the page declares no product number"
UNRESOLVED_SHAPE = "the declared product address is not a product page with a digit number"
UNRESOLVED_INCOMPLETE = "the page does not corroborate the product number everywhere it must"
UNRESOLVED_DISAGREE = "the page's declarations of the product number disagree"


@dataclass(frozen=True)
class SourceIdentity:
    source_product_id: str
    agreed: tuple[str, ...]


@dataclass(frozen=True)
class UnresolvedIdentity:
    reason: str
    seen: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


IdentityResult = SourceIdentity | UnresolvedIdentity


def goods_number(url: str) -> str | None:
    """The ``goodsNo`` a product URL states, or None when it states none or not exactly one."""
    try:
        values = parse_qs(urlsplit(url).query).get(IDENTITY_KEY, [])
    except ValueError:
        return None
    if len(values) != 1:
        return None
    number = values[0].strip()
    return number if number.isdigit() else None


def _code_rows(nodes: Sequence[Node]) -> list[str]:
    return [value.strip() for label, value, _dd in rows(nodes) if label == CODE_LABEL]


def resolve(document: DocumentView, source_url: str) -> IdentityResult:
    """The product's source identity, or why it could not be read."""
    nodes = read(document.body)
    declared_url = meta(nodes).get("og:url", "").strip()
    if not declared_url:
        return UnresolvedIdentity(UNRESOLVED_ABSENT, missing=("meta[og:url]",))
    number = goods_number(declared_url)
    if number is None or urlsplit(declared_url).path != PRODUCT_PATH:
        return UnresolvedIdentity(UNRESOLVED_SHAPE)
    codes = _code_rows(nodes)
    corroborations: dict[str, str | None] = {
        "url": goods_number(source_url),
        f"dt:{CODE_LABEL} + dd": codes[0] if len(codes) == 1 and codes[0].isdigit() else None,
    }
    missing = tuple(where for where, value in corroborations.items() if value is None)
    if missing:
        return UnresolvedIdentity(UNRESOLVED_INCOMPLETE, missing=missing)
    stated = [value for value in corroborations.values() if value is not None]
    disagreeing = {value for value in stated if value != number}
    if disagreeing:
        return UnresolvedIdentity(UNRESOLVED_DISAGREE, seen=tuple(sorted(disagreeing | {number})))
    agreed = ("meta[og:url]", *corroborations)
    return SourceIdentity(source_product_id=number, agreed=agreed)
