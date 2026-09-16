"""What a KM통상 product page states about itself (ADR-0010 §7–§8, ruling 5702780630 P1).

This turns one immutable product document into supplier-neutral facts and the evidence for each.
It reads; it never fetches, stores, hashes, persists or retries. Every value is quoted from the
page: a field the page does not state is ``ABSENT``, and a field whose evidence is present but
cannot be read from the document alone is ``REVIEW_REQUIRED``. Nothing is inferred, averaged,
converted or completed, and no value comes from a script — what only a script mentions was never
stated to the reader.

**Where this storefront states each fact.** Cafe24 lays the product's own data out as a table of
label and value rows in the information area, some of them hidden but still the page's own
statement, and repeats the money amounts as ``product:*`` metadata:

===================  ====================================================================
field                read from
===================  ====================================================================
original_name        ``og:title``, agreeing with the 상품명 row
prices               every price row, each with the exact label the page used
minimum_sale_price   the 최저지도가 row, only when the page states it
shipping             the 배송방법 and 배송비 rows, kept as the page's own words
stock                the purchase controls the reader can see, and a visible sold-out mark
options              the option control, when the document itself carries its values
quantity_tiers       a tier table, when the document itself carries one
detail_description   the presence of the description block
brand, manufacturer, origin, notice   their own labelled rows
===================  ====================================================================

A price is an integer number of won. A source total is never divided into a unit price, and a
conditional shipping policy is never flattened into a fixed fee.
"""

import re
from collections.abc import Iterator, Sequence

from app.collect.facts import (
    Availability,
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    MoneyValue,
    PricesValue,
    ShippingKind,
    ShippingValue,
    SourcePrice,
    StockValue,
    TextValue,
)
from integrations.suppliers.collection import DocumentView
from integrations.suppliers.kmretail.collect.dom import Node, meta, read

# The exact labels this storefront uses. A label is site knowledge, so it lives here and nowhere
# else; a row whose label is not one of these is not read as that field.
NAME_LABELS = ("상품명",)
PRICE_LABELS = ("판매가", "소비자가", "정가", "공급가")
MINIMUM_PRICE_LABELS = ("최저지도가", "최저판매가")
SHIPPING_METHOD_LABELS = ("배송방법",)
SHIPPING_FEE_LABELS = ("배송비",)
BRAND_LABELS = ("브랜드",)
MANUFACTURER_LABELS = ("제조사",)
ORIGIN_LABELS = ("원산지",)
# Controls the reader can act on, and the words this storefront uses when it cannot be bought.
PURCHASE_CONTROLS = ("btnBuy", "btnBasket", "btnCart")
SOLD_OUT_WORDS = ("품절", "SOLD OUT", "SOLDOUT", "일시품절")
OPTION_CONTAINER = "xans-product-option"
DETAIL_CONTAINER = "prdDetail"
# The tab strip this storefront repeats above every detail panel. Its words are navigation, so a
# description block that holds nothing but images must not be read as if it stated them.
DETAIL_MENU = "dMenu"
# The words a page would have to use to state a quantity tier. The retained evidence carries none,
# and no accepted rule says how this storefront lays one out, so a tier is never read into a value.
TIER_LABELS = ("수량별 가격", "수량별 할인", "수량 할인", "수량별")

_AMOUNT = re.compile(r"(\d[\d,]*)\s*원?")
_EVIDENCE_LIMIT = 200


def _won(text: str) -> int | None:
    """The integer number of won a cell states, or None when it states no amount."""
    match = _AMOUNT.search(text.replace(" ", ""))
    if match is None:
        return None
    digits = match.group(1).replace(",", "")
    return int(digits) if digits.isdigit() else None


def _quote(text: str) -> str:
    return text[:_EVIDENCE_LIMIT]


def _rows(nodes: Sequence[Node]) -> Iterator[tuple[str, str, Node]]:
    """Every (label, value) pair of the information tables, in source order.

    A row states a fact whether or not the storefront paints it, so a hidden row is read; what the
    row is *about* is its own heading cell, never a neighbour's.
    """
    pending: tuple[str, Node] | None = None
    for node in nodes:
        if node.tag == "th":
            label = node.text
            pending = (label, node) if label else None
        elif node.tag == "td" and pending is not None:
            value = node.text
            if value:
                yield pending[0], value, node
            pending = None


def _absent(locator: str) -> FieldFact:
    return FieldFact(
        status=FieldStatus.ABSENT,
        value=None,
        evidence=(Evidence(EvidenceKind.DOM_TEXT, locator, FieldStatus.ABSENT),),
    )


def _review(locator: str, kind: EvidenceKind = EvidenceKind.DOM_TEXT) -> FieldFact:
    return FieldFact(
        status=FieldStatus.REVIEW_REQUIRED,
        value=None,
        evidence=(Evidence(kind, locator, FieldStatus.REVIEW_REQUIRED),),
    )


def _labelled(
    rows: Sequence[tuple[str, str, Node]], labels: Sequence[str]
) -> list[tuple[str, str, Node]]:
    return [row for row in rows if any(label == row[0] for label in labels)]


def _name(nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]]) -> FieldFact:
    declared = meta(nodes).get("og:title", "").strip()
    stated = _labelled(rows, NAME_LABELS)
    if not declared and not stated:
        return _absent("meta[og:title]")
    text = declared or stated[0][1]
    evidence = [
        Evidence(
            EvidenceKind.ATTRIBUTE,
            "meta[og:title]",
            FieldStatus.CONFIRMED,
            observed=_quote(declared),
        )
        if declared
        else Evidence(
            EvidenceKind.DOM_TEXT,
            "th:상품명 + td",
            FieldStatus.CONFIRMED,
            observed=_quote(stated[0][1]),
        )
    ]
    if declared and stated:
        evidence.append(
            Evidence(
                EvidenceKind.DOM_TEXT,
                "th:상품명 + td",
                FieldStatus.CONFIRMED,
                observed=_quote(stated[0][1]),
            )
        )
    return FieldFact(FieldStatus.CONFIRMED, TextValue(text=text), tuple(evidence))


def _prices(nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]]) -> FieldFact:
    prices: list[SourcePrice] = []
    evidence: list[Evidence] = []
    for label, value, _cell in _labelled(rows, PRICE_LABELS):
        amount = _won(value)
        if amount is None:
            continue
        if any(price.label == label for price in prices):
            continue  # the same labelled price repeated in another table is one source price
        prices.append(SourcePrice(label=label, amount_krw=amount))
        evidence.append(
            Evidence(
                EvidenceKind.DOM_TEXT,
                f"th:{label} + td",
                FieldStatus.CONFIRMED,
                observed=_quote(value),
                normalized=str(amount),
            )
        )
    declared = meta(nodes)
    for key in ("product:price:amount", "product:sale_price:amount"):
        stated = declared.get(key, "").strip()
        if stated:
            evidence.append(
                Evidence(
                    EvidenceKind.ATTRIBUTE, f"meta[{key}]", FieldStatus.CONFIRMED, observed=stated
                )
            )
    if not prices:
        return _review("th:판매가 + td") if evidence else _absent("th:판매가 + td")
    return FieldFact(FieldStatus.CONFIRMED, PricesValue(prices=tuple(prices)), tuple(evidence))


def _minimum_sale_price(rows: Sequence[tuple[str, str, Node]]) -> FieldFact:
    stated = _labelled(rows, MINIMUM_PRICE_LABELS)
    if not stated:
        # Never derived from a sale price: an unstated minimum is no minimum (CLAUDE.md §6.1).
        return _absent("th:최저지도가 + td")
    label, value, _ = stated[0]
    amount = _won(value)
    if amount is None:
        return _review(f"th:{label} + td")
    return FieldFact(
        FieldStatus.CONFIRMED,
        MoneyValue(label=label, amount_krw=amount),
        (
            Evidence(
                EvidenceKind.DOM_TEXT,
                f"th:{label} + td",
                FieldStatus.CONFIRMED,
                observed=_quote(value),
                normalized=str(amount),
            ),
        ),
    )


def _shipping(rows: Sequence[tuple[str, str, Node]]) -> FieldFact:
    method = _labelled(rows, SHIPPING_METHOD_LABELS)
    fee = _labelled(rows, SHIPPING_FEE_LABELS)
    if not method and not fee:
        return _absent("th:배송비 + td")
    policy = " / ".join(f"{label} {value}" for label, value, _ in (*method, *fee))
    evidence = tuple(
        Evidence(
            EvidenceKind.DOM_TEXT,
            f"th:{label} + td",
            FieldStatus.CONFIRMED,
            observed=_quote(row_value),
        )
        for label, row_value, _cell in (*method, *fee)
    )
    if not fee:
        # A method without a fee states no amount; a guessed one would be an invention.
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            None,
            (*evidence, Evidence(EvidenceKind.DOM_TEXT, "th:배송비 + td", FieldStatus.ABSENT)),
        )
    amount = _won(fee[0][1])
    if amount is None:
        return FieldFact(FieldStatus.REVIEW_REQUIRED, None, evidence)
    kind = ShippingKind.FREE if amount == 0 else ShippingKind.FIXED
    value = ShippingValue(
        kind=kind,
        policy_text=policy,
        fee_krw=None if kind is ShippingKind.FREE else amount,
    )
    return FieldFact(FieldStatus.CONFIRMED, value, evidence)


def _stock(nodes: Sequence[Node]) -> FieldFact:
    controls = [
        node
        for node in nodes
        if not node.hidden and any(node.marks(name) for name in PURCHASE_CONTROLS)
    ]
    sold_out = [
        node
        for node in nodes
        if not node.hidden
        and node.tag not in ("html", "body")
        and any(word in node.visible_text for word in SOLD_OUT_WORDS)
        and len(node.visible_text) <= 40
    ]
    evidence = [
        Evidence(
            EvidenceKind.CONTROL_STATE,
            node.selector,
            FieldStatus.CONFIRMED,
            observed="purchase control",
        )
        for node in controls[:3]
    ] + [
        Evidence(
            EvidenceKind.DOM_TEXT,
            node.selector,
            FieldStatus.CONFIRMED,
            observed=_quote(node.visible_text),
        )
        for node in sold_out[:3]
    ]
    if controls and not sold_out:
        availability = Availability.ON_SALE
    elif sold_out and not controls:
        availability = Availability.SOLD_OUT
    else:
        # Both, or neither: the page does not say plainly enough to record a state.
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            None,
            tuple(evidence)
            or (Evidence(EvidenceKind.CONTROL_STATE, "span#btnBuy", FieldStatus.ABSENT),),
        )
    return FieldFact(FieldStatus.CONFIRMED, StockValue(availability=availability), tuple(evidence))


def _options(nodes: Sequence[Node]) -> FieldFact:
    """The choices the page offers, or why they cannot be recorded as a value.

    This storefront writes the option container on every product, so the container alone says
    nothing. A product that has an axis to choose states it as a ``select``; a product without one
    writes none, and that is a stated absence, not a doubt.

    When axes *are* stated, the evidence keeps each axis and its values separately and in the
    order the page wrote them — a different count, grade or weight stays a different value — but
    the field stays ``REVIEW_REQUIRED``: no accepted evidence shows how this storefront names an
    axis, and a name is not something a parser may invent.
    """
    container = [node for node in nodes if node.marks(OPTION_CONTAINER)]
    if not container:
        return _absent(f"*.{OPTION_CONTAINER}")
    axes = [
        node
        for node in container[0].descendants()
        if node.tag == "select" and any(child.tag == "option" for child in node.descendants())
    ]
    if not axes:
        return _absent(f"*.{OPTION_CONTAINER} select")
    evidence = tuple(
        Evidence(
            EvidenceKind.DOM_TEXT,
            f"*.{OPTION_CONTAINER} {axis.selector}",
            FieldStatus.REVIEW_REQUIRED,
            observed=_quote(
                " / ".join(
                    choice.text
                    for choice in axis.descendants()
                    if choice.tag == "option" and choice.text
                )
            )
            or None,
        )
        for axis in axes
    )
    return FieldFact(FieldStatus.REVIEW_REQUIRED, None, evidence)


def _quantity_tiers(nodes: Sequence[Node]) -> FieldFact:
    """A quantity tier is a source total, and this parser never produces one it cannot read.

    The retained captures carry no tier table: the ``quantity_price`` span beside the order form
    is the cart line's own total for a quantity the reader has not chosen yet, not a supplier
    tier. So a page that states none of the tier wordings states no tiers, and a page that does
    state one is held for review with its own words kept — never divided into a unit price.
    """
    stated = [
        node
        for node in nodes
        if node.tag in ("th", "td", "h3", "h4", "strong", "caption")
        and any(label in node.text for label in TIER_LABELS)
    ]
    if not stated:
        return _absent(f"th:{TIER_LABELS[0]}")
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        tuple(
            Evidence(
                EvidenceKind.DOM_TEXT,
                node.selector,
                FieldStatus.REVIEW_REQUIRED,
                observed=_quote(node.text),
            )
            for node in stated[:3]
        ),
    )


def _detail_description(nodes: Sequence[Node]) -> FieldFact:
    block = [node for node in nodes if node.marks(DETAIL_CONTAINER)]
    if not block:
        return _absent(f"#{DETAIL_CONTAINER}")
    text = block[0].text_outside(DETAIL_MENU)
    if not text:
        # A description made only of images states no text of its own; the images are their own
        # field, and inventing a description from them would be fabrication.
        return _review(f"#{DETAIL_CONTAINER}")
    return FieldFact(
        FieldStatus.CONFIRMED,
        TextValue(text=_quote(text)),
        (
            Evidence(
                EvidenceKind.PRODUCT_HTML_FRAGMENT,
                f"#{DETAIL_CONTAINER}",
                FieldStatus.CONFIRMED,
                observed=_quote(text),
            ),
        ),
    )


def _text_row(rows: Sequence[tuple[str, str, Node]], labels: Sequence[str]) -> FieldFact:
    stated = _labelled(rows, labels)
    if not stated:
        return _absent(f"th:{labels[0]} + td")
    label, value, _ = stated[0]
    return FieldFact(
        FieldStatus.CONFIRMED,
        TextValue(text=_quote(value)),
        (
            Evidence(
                EvidenceKind.DOM_TEXT,
                f"th:{label} + td",
                FieldStatus.CONFIRMED,
                observed=_quote(value),
            ),
        ),
    )


def parse_fields(document: DocumentView) -> dict[str, FieldFact]:
    """Every M3 source-truth field this document states, and the evidence for each."""
    nodes = read(document.body)
    rows = tuple(_rows(nodes))
    return {
        "original_name": _name(nodes, rows),
        "prices": _prices(nodes, rows),
        "options": _options(nodes),
        "stock": _stock(nodes),
        "shipping": _shipping(rows),
        "minimum_sale_price": _minimum_sale_price(rows),
        "quantity_tiers": _quantity_tiers(nodes),
        "brand": _text_row(rows, BRAND_LABELS),
        "manufacturer": _text_row(rows, MANUFACTURER_LABELS),
        "origin": _text_row(rows, ORIGIN_LABELS),
        "notice": _absent("th:상품정보제공고시 + td"),
        "detail_description": _detail_description(nodes),
    }
