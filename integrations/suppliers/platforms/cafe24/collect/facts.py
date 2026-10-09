"""What a Cafe24 product page states about itself (ADR-0010 §7–§8, ADR-0030 §2, §4).

Derived from KM통상's parser (ruling 5702780630 P1, ``kmretail-3``). The rules are unchanged; only
the words and regions a site may vary are now a :class:`Vocabulary`, whose defaults are KM통상's
and which a site configuration may only extend with plain words or replace a region of
(ADR-0030 §3).

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
minimum_sale_price   the 최저지도가 row, only when the page states an amount; a row that says
                     the price is free (자율) states that there is no minimum
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
from dataclasses import dataclass

from app.stages.collect.facts import (
    SALES_CHANNEL_MARKETPLACES,
    Availability,
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    MoneyValue,
    PriceRole,
    PricesValue,
    SalesChannelScope,
    SalesChannelsValue,
    ShippingKind,
    ShippingValue,
    SourcePrice,
    StockValue,
    TextValue,
)
from integrations.suppliers.collection import DocumentView
from integrations.suppliers.platforms.cafe24.collect.dom import Node, meta, read

# The word a page writes in the minimum-price row when the reseller sets the price freely
# (판매가 자율): the page states that there is no minimum. The user's rule of 2026-10-03.
NO_MINIMUM_WORD = "자율"
# The words a page would have to use to state a quantity tier. No accepted rule says how a Cafe24
# skin lays one out, so a tier is never read into a value.
TIER_LABELS = ("수량별 가격", "수량별 할인", "수량 할인", "수량별")


@dataclass(frozen=True)
class Vocabulary:
    """The words and regions one site uses. A label is site knowledge; a row whose label is not
    one of these is not read as that field."""

    name: tuple[str, ...] = ("상품명",)
    price: tuple[str, ...] = ("판매가", "소비자가", "정가", "공급가")
    minimum_price: tuple[str, ...] = ("최저지도가", "최저판매가")
    shipping_method: tuple[str, ...] = ("배송방법",)
    shipping_fee: tuple[str, ...] = ("배송비",)
    brand: tuple[str, ...] = ("브랜드",)
    manufacturer: tuple[str, ...] = ("제조사",)
    origin: tuple[str, ...] = ("원산지",)
    sold_out: tuple[str, ...] = ("품절", "SOLD OUT", "SOLDOUT", "일시품절")
    # ADR-0032 §2: the labels whose price is the purchase price, and those that are list prices.
    # A template declares none; only a site's words give a price a role.
    purchase_price: tuple[str, ...] = ()
    list_price: tuple[str, ...] = ()
    # ADR-0031 §3: the sales-channel row label and the phrases that read it. Empty by default: a
    # row the site has not worded stays under review, never allowed.
    sales_channel_row: tuple[str, ...] = ()
    channel_all_allowed: tuple[str, ...] = ()
    channel_closed_only: tuple[str, ...] = ()
    channel_forbid_coupang: tuple[str, ...] = ()
    channel_forbid_smartstore: tuple[str, ...] = ()
    # Controls the reader can act on: the older skins' ids and the smart-design skins' own.
    purchase_controls: tuple[str, ...] = (
        "btnBuy",
        "btnBasket",
        "btnCart",
        "actionBuy",
        "actionCart",
    )
    option_container: str = "xans-product-option"
    detail_container: str = "prdDetail"
    # The tab strip a skin repeats above every detail panel. Its words are navigation, so a
    # description block that holds nothing but images must not be read as if it stated them.
    detail_menu: str = "dMenu"
    # The representative image's container inside the product image module.
    key_image: str = "keyImg"


DEFAULT_VOCABULARY = Vocabulary()

_AMOUNT = re.compile(r"(\d[\d,]*)\s*원?")
# ADR-0032 §4 (the owner's rule, Issue #219 6086421199): a fee stated as a range, ``A원 ~ B원``,
# is read as its highest amount.
_RANGE = re.compile(r"(\d[\d,]*)\s*원?\s*[~∼〜]\s*(\d[\d,]*)\s*원")
# A fee cell that states one fee and nothing else: an amount of won, and no condition.
_ONLY_FEE = re.compile(r"\d[\d,]*원")
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


def _name(
    nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    declared = meta(nodes).get("og:title", "").strip()
    stated = _labelled(rows, words.name)
    if not declared and not stated:
        return _absent("meta[og:title]")
    if declared and stated and declared != stated[0][1] and declared.startswith(stated[0][1]):
        # A skin that appends the shop's name to the declared title (U-PICK: "<name> - U-PICK
        # B2B") states the product's own name in its 상품명 row; the suffix is not the name.
        declared = ""
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
            f"th:{words.name[0]} + td",
            FieldStatus.CONFIRMED,
            observed=_quote(stated[0][1]),
        )
    ]
    if declared and stated:
        evidence.append(
            Evidence(
                EvidenceKind.DOM_TEXT,
                f"th:{words.name[0]} + td",
                FieldStatus.CONFIRMED,
                observed=_quote(stated[0][1]),
            )
        )
    return FieldFact(FieldStatus.CONFIRMED, TextValue(text=text), tuple(evidence))


def _prices(
    nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    prices: list[SourcePrice] = []
    evidence: list[Evidence] = []
    labels = tuple(dict.fromkeys((*words.price, *words.purchase_price, *words.list_price)))
    for label, value, _cell in _labelled(rows, labels):
        amount = _won(value)
        if amount is None:
            continue
        if any(price.label == label for price in prices):
            continue  # the same labelled price repeated in another table is one source price
        role = (
            PriceRole.PURCHASE
            if label in words.purchase_price
            else PriceRole.LIST
            if label in words.list_price
            else None
        )
        prices.append(SourcePrice(label=label, amount_krw=amount, role=role))
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
        locator = f"th:{words.price[0]} + td"
        return _review(locator) if evidence else _absent(locator)
    return FieldFact(FieldStatus.CONFIRMED, PricesValue(prices=tuple(prices)), tuple(evidence))


def _minimum_sale_price(rows: Sequence[tuple[str, str, Node]], words: Vocabulary) -> FieldFact:
    stated = _labelled(rows, words.minimum_price)
    if not stated:
        # Never derived from a sale price: an unstated minimum is no minimum (CLAUDE.md §6.1).
        return _absent(f"th:{words.minimum_price[0]} + td")
    label, value, _ = stated[0]
    amount = _won(value)
    if amount is None and NO_MINIMUM_WORD in value.replace(" ", ""):
        # Stated, and what it states is that the price is free: no minimum, read from the page.
        return FieldFact(
            FieldStatus.ABSENT,
            None,
            (
                Evidence(
                    EvidenceKind.DOM_TEXT,
                    f"th:{label} + td",
                    FieldStatus.ABSENT,
                    observed=_quote(value),
                ),
            ),
        )
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


def _shipping(rows: Sequence[tuple[str, str, Node]], words: Vocabulary) -> FieldFact:
    method = _labelled(rows, words.shipping_method)
    fee = _labelled(rows, words.shipping_fee)
    fee_locator = f"th:{words.shipping_fee[0]} + td"
    if not method and not fee:
        return _absent(fee_locator)
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
            (*evidence, Evidence(EvidenceKind.DOM_TEXT, fee_locator, FieldStatus.ABSENT)),
        )
    amount = _won(fee[0][1])
    ranged = _RANGE.search(fee[0][1].replace(" ", ""))
    if ranged is not None:
        # ADR-0032 §4, the owner's rule: the highest amount of a stated range, its words kept.
        bounds = [int(group.replace(",", "")) for group in ranged.groups()]
        amount = max(bounds)
        evidence = (
            *evidence,
            Evidence(
                EvidenceKind.DOM_TEXT,
                fee_locator,
                FieldStatus.CONFIRMED,
                observed=_quote(fee[0][1]),
                normalized=str(amount),
            ),
        )
    elif amount is None or not _ONLY_FEE.fullmatch(fee[0][1].replace(" ", "")):
        # A cell that is neither a range nor one amount and nothing else states a condition or a
        # choice: a threshold or a free-over amount. It is never flattened into an amount it names
        # (ADR-0010 §7). KM통상's parser reads the first amount; the template states this
        # difference (ADR-0030 §10).
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            None,
            (
                *evidence,
                Evidence(
                    EvidenceKind.DOM_TEXT,
                    fee_locator,
                    FieldStatus.REVIEW_REQUIRED,
                    observed=_quote(fee[0][1]),
                ),
            ),
        )
    kind = ShippingKind.FREE if amount == 0 else ShippingKind.FIXED
    value = ShippingValue(
        kind=kind,
        policy_text=policy,
        fee_krw=None if kind is ShippingKind.FREE else amount,
    )
    return FieldFact(FieldStatus.CONFIRMED, value, evidence)


def _stock(nodes: Sequence[Node], words: Vocabulary) -> FieldFact:
    controls = [
        node
        for node in nodes
        if not node.hidden and any(node.marks(name) for name in words.purchase_controls)
    ]
    sold_out = [
        node
        for node in nodes
        if not node.hidden
        and node.tag not in ("html", "body")
        and any(word in node.visible_text for word in words.sold_out)
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
    # ADR-0010 §10: an active BUY/CART control decides ON_SALE, and sold-out words beside it are
    # not authoritative; SOLD OUT decides only with no active purchase path. KM통상's parser holds
    # both together under review; the template states this difference (ADR-0030 §10).
    if controls:
        return FieldFact(
            FieldStatus.CONFIRMED,
            StockValue(availability=Availability.ON_SALE),
            tuple(evidence[: len(controls[:3])]),
        )
    if sold_out:
        return FieldFact(
            FieldStatus.CONFIRMED, StockValue(availability=Availability.SOLD_OUT), tuple(evidence)
        )
    # Neither: the page does not say plainly enough to record a state.
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        (Evidence(EvidenceKind.CONTROL_STATE, "purchase control", FieldStatus.REVIEW_REQUIRED),),
    )


def _options(nodes: Sequence[Node], words: Vocabulary) -> FieldFact:
    """The choices the page offers, or why they cannot be recorded as a value.

    A Cafe24 skin writes the option container on every product, so the container alone says
    nothing. A product that has an axis to choose states it as a ``select``; a product without one
    writes none, and that is a stated absence, not a doubt. ``ABSENT`` is the reading the canonical
    Product DB takes as proof of "no options" (ADR-0013 ruling B, the default single-unit
    composition), and KM통상's parser gives the same reading (ADR-0030 §2).

    When axes *are* stated, the evidence keeps each axis and its values separately and in the
    order the page wrote them — a different count, grade or weight stays a different value — but
    the field stays ``REVIEW_REQUIRED``: no accepted evidence shows how this storefront names an
    axis, and a name is not something a parser may invent.
    """
    marker = words.option_container
    container = [node for node in nodes if node.marks(marker)]
    if not container:
        return _absent(f"*.{marker}")
    axes = [
        node
        for node in container[0].descendants()
        if node.tag == "select" and any(child.tag == "option" for child in node.descendants())
    ]
    if not axes:
        return _absent(f"*.{marker} select")
    evidence = tuple(
        Evidence(
            EvidenceKind.DOM_TEXT,
            f"*.{marker} {axis.selector}",
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


def _detail_description(nodes: Sequence[Node], words: Vocabulary) -> FieldFact:
    detail = words.detail_container
    block = [node for node in nodes if node.marks(detail)]
    if not block:
        return _absent(f"#{detail}")
    text = block[0].text_outside(words.detail_menu)
    if not text:
        # A description made only of images states no text of its own; the images are their own
        # field, and inventing a description from them would be fabrication.
        return _review(f"#{detail}")
    return FieldFact(
        FieldStatus.CONFIRMED,
        TextValue(text=_quote(text)),
        (
            Evidence(
                EvidenceKind.PRODUCT_HTML_FRAGMENT,
                f"#{detail}",
                FieldStatus.CONFIRMED,
                observed=_quote(text),
            ),
        ),
    )


def _squash(text: str) -> str:
    return "".join(text.split())


def _sales_channels(
    nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    """ADR-0031 §3: what the page says about where the product may be resold.

    Only the site's own phrases are read, in its sales-channel row and in the description text;
    nothing is read from an image. A row no phrase reads, or phrases that contradict each other,
    stay under review: an unread restriction is never an allowed one.
    """
    stated = _labelled(rows, words.sales_channel_row) if words.sales_channel_row else []
    detail = [node for node in nodes if node.marks(words.detail_container)]
    sources: list[tuple[str, str]] = [(f"th:{label} + td", value) for label, value, _ in stated]
    if detail:
        sources.append((f"#{words.detail_container}", detail[0].text_outside(words.detail_menu)))

    def found(phrases: Sequence[str]) -> list[tuple[str, str, str]]:
        return [
            (locator, phrase, text)
            for locator, text in sources
            for phrase in phrases
            if _squash(phrase) in _squash(text)
        ]

    allowed = found(words.channel_all_allowed)
    closed = found(words.channel_closed_only)
    forbids = {
        "coupang": found(words.channel_forbid_coupang),
        "smartstore": found(words.channel_forbid_smartstore),
    }
    assert set(forbids) <= SALES_CHANNEL_MARKETPLACES
    restricting = closed + [hit for hits in forbids.values() for hit in hits]
    matched = allowed + restricting
    if not matched:
        if stated:
            return _review(f"th:{stated[0][0]} + td")
        return _absent(f"th:{(words.sales_channel_row or ('판매가능플랫폼',))[0]} + td")
    evidence_status = FieldStatus.REVIEW_REQUIRED if allowed and restricting else None
    if evidence_status is None and stated and not any(hit[0].startswith("th:") for hit in matched):
        # The row says something no phrase reads, even if the description says something else.
        evidence_status = FieldStatus.REVIEW_REQUIRED
    evidence = tuple(
        Evidence(
            EvidenceKind.DOM_TEXT,
            locator,
            evidence_status or FieldStatus.CONFIRMED,
            observed=_quote(phrase),
        )
        for locator, phrase, _text in matched
    )
    if evidence_status is not None:
        return FieldFact(FieldStatus.REVIEW_REQUIRED, None, evidence)
    policy = _quote(" / ".join(dict.fromkeys(phrase for _locator, phrase, _text in matched)))
    if closed:
        value = SalesChannelsValue(scope=SalesChannelScope.CLOSED_MALL_ONLY, policy_text=policy)
    elif restricting:
        forbidden = tuple(sorted(key for key, hits in forbids.items() if hits))
        value = SalesChannelsValue(
            scope=SalesChannelScope.LISTED, forbidden=forbidden, policy_text=policy
        )
    else:
        value = SalesChannelsValue(scope=SalesChannelScope.ALL_ALLOWED, policy_text=policy)
    return FieldFact(FieldStatus.CONFIRMED, value, evidence)


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


def parse_fields(
    document: DocumentView, words: Vocabulary = DEFAULT_VOCABULARY
) -> dict[str, FieldFact]:
    """Every M3 source-truth field this document states, and the evidence for each."""
    nodes = read(document.body)
    rows = tuple(_rows(nodes))
    return {
        "original_name": _name(nodes, rows, words),
        "prices": _prices(nodes, rows, words),
        "options": _options(nodes, words),
        "stock": _stock(nodes, words),
        "shipping": _shipping(rows, words),
        "minimum_sale_price": _minimum_sale_price(rows, words),
        "quantity_tiers": _quantity_tiers(nodes),
        "brand": _text_row(rows, words.brand),
        "manufacturer": _text_row(rows, words.manufacturer),
        "origin": _text_row(rows, words.origin),
        "notice": _absent("th:상품정보제공고시 + td"),
        "detail_description": _detail_description(nodes, words),
        "sales_channels": _sales_channels(nodes, rows, words),
    }
