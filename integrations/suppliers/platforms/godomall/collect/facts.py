"""What a Godomall product page states about itself (ADR-0010 §7–§8, ADR-0030 §2, §4, ADR-0034).

Written from 건강산's reconnaissance captures (Issue #219 `6086058056`). A Godomall skin lays the
product's own data out as ``dl`` rows of ``dt`` label and ``dd`` value inside
``.item_detail_list``, lists the lines to be ordered in ``.item_choice_list``, writes the purchase
controls in ``.btn_choice_box``, and puts the description in ``#detail .txt-manual``.

The browser capture keeps no ``name`` or ``value`` attribute (ADR-0019 §6.1), so nothing here reads
a form input's value: every fact is read from what the page shows.

Every reading fails closed:
- A value the page does not state is ``ABSENT``. Evidence the page holds but that cannot be read
  from the document alone is ``REVIEW_REQUIRED``.
- Declarations of one fact that disagree are held for review. A money cell is read only when it
  states exactly one amount.
- Stock is decided only inside the purchase-control box. An active control decides ``ON_SALE``
  (ADR-0010 §10); a sold-out control decides ``SOLD_OUT`` only when no active control exists.
  A stock row stating none beside an active control is a disagreement, held for review.
- The shipping fact is recorded as the page states it. A base fee with a free-over threshold is
  ``CONDITIONAL``, carrying both; pricing reads that at its base fee (ADR-0034 §2). A fee stated
  as a range is ``FIXED`` at its highest amount, its words kept (ADR-0032 §4). A region layer's
  words are kept beside the base fee and never priced (ADR-0035 §2); a layer stating its
  surcharge only as an image, or in words longer than 200 characters, is held for review.
- A minimum resale price written as a description sentence is read only from the site's phrases
  (ADR-0034 §1). A minimum row that is exactly a per-quantity list with a one-unit amount above
  zero reads that amount (ADR-0035 §3).
- A restricting channel phrase is read in the product's name too, never an allowed one, and a
  channel row that is not wholly allowed is read only when every word of it is a configured
  phrase or a separator (ADR-0035 §1, NR-03).
- A value made only of invisible characters is empty (ADR-0035): a ``dd`` row stays stated with
  an empty value, so a minimum row of only U+FEFF is held for review, as an empty one always was.
- Nothing is read from an image or a script, and every locator is written here.
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
from integrations.suppliers.platforms.godomall.collect.dom import Node, clean, meta, read

# The product's information rows and the box of its purchase controls.
INFO_LIST = "item_detail_list"
CHOICE_BOX = "btn_choice_box"
# The amount-tier layer of the shipping row (금액별배송비), and its region layer (지역별배송비).
FEE_TIERS = "lyDelivery"
REGION_FEES = "lyDeliveryZone"
# The region layer's own title (지역별배송비) and close control: neither is a surcharge the
# layer states.
REGION_TITLE = ".ly_tit"
REGION_CLOSE = "ly_close"
# ADR-0035 §2: the longest region-surcharge words kept as policy text; longer ones are held.
REGION_WORDS_LIMIT = 200
# ADR-0035 §1 NR-03: the separators a channel row puts between its phrases.
_CHANNEL_SEPARATORS = str.maketrans("", "", "/,·")
# The lines to be ordered, and one line of it. A product without options has its one line
# written in advance; a product with options starts with none.
CHOICE_LIST = "item_choice_list"
CHOICE_LINE = "option_display_item_"
# The stock row, and the word a minimum row uses for "no minimum" (ADR-0030 §2).
STOCK_ROW = "상품재고"
NO_MINIMUM_WORD = "자율"
# A product information notice: its heading words, spaces removed.
NOTICE_WORDS = ("상품정보제공고시", "상품정보고시", "상품필수정보")
MARK_LIMIT = 40
_EVIDENCE_LIMIT = 200


@dataclass(frozen=True)
class Vocabulary:
    """The words and regions one site uses. A label is site knowledge; a row whose label is not
    one of these is not read as that field."""

    name_heading: str = "item_detail_tit"
    price: tuple[str, ...] = ("판매가", "정가", "소비자가")
    # ADR-0032 §2: only a site's words give a price a role.
    purchase_price: tuple[str, ...] = ()
    list_price: tuple[str, ...] = ()
    minimum_price: tuple[str, ...] = ("최저판매가", "최저지도가")
    # ADR-0034 §1: the phrases a description sentence names its minimum with.
    minimum_price_phrase: tuple[str, ...] = ()
    shipping_fee: tuple[str, ...] = ("배송비",)
    product_code: tuple[str, ...] = ("상품코드",)
    brand: tuple[str, ...] = ("브랜드",)
    manufacturer: tuple[str, ...] = ("제조사",)
    origin: tuple[str, ...] = ("원산지",)
    sold_out: tuple[str, ...] = ("구매 불가", "구매불가", "품절", "SOLD OUT")
    purchase_controls: tuple[str, ...] = ("btn_add_cart", "btn_add_order")
    sold_out_controls: tuple[str, ...] = ("btn_add_soldout",)
    # ADR-0031 §3: the label of a row stating the channels, and the channel phrases, read from
    # such rows and the description. The phrases are empty by default.
    sales_channel_row: tuple[str, ...] = ("판매가능플랫폼",)
    channel_all_allowed: tuple[str, ...] = ()
    channel_closed_only: tuple[str, ...] = ()
    channel_forbid_coupang: tuple[str, ...] = ()
    channel_forbid_smartstore: tuple[str, ...] = ()
    detail_container: str = "detail"
    detail_text: str = "txt-manual"


DEFAULT_VOCABULARY = Vocabulary()

_NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)"
_ONE_AMOUNT = re.compile(_NUMBER + r"원")
_BARE_NUMBER = re.compile(_NUMBER)
_PRICE_WITH_NOTES = re.compile(
    _NUMBER + r"원(?:\d{1,3}%)?(?:\(?(?:부가세|VAT)(?:포함|별도)\)?)?", re.IGNORECASE
)
_MINIMUM_CELL = re.compile(_NUMBER + r"원(?:이상)?")
# ADR-0035 §3: one entry of a per-quantity minimum row, "<k>개 <amount>원 이상" with whitespace
# removed, and a cell that is exactly a list of them, separated by "/" or ",". Entries separated
# only by whitespace, or by nothing at all, are accepted too: a line break (``<br>``) leaves no
# mark in the collapsed text, so it cannot be told apart from them. An entry ends at 이상, so the
# next quantity starts unambiguously.
_QUANTITY_ENTRY = r"([1-9]\d*)개" + _NUMBER + r"원이상"
_QUANTITY_MINIMUM = re.compile(_QUANTITY_ENTRY)
_QUANTITY_LIST = re.compile(rf"{_QUANTITY_ENTRY}(?:[/,]?{_QUANTITY_ENTRY})*")
# ADR-0034 §1: the per-unit minimum a sentence states after its phrase: "1개 21,000원 이상" or
# "4,400원 이상". A bundle (묶음) or shipping-inclusive (배송비 포함) amount is never one.
# The amount must follow the phrase directly: anything between them (a quantity, a bundle, a
# shipping-inclusive note, another amount) is not a per-unit minimum the rule can read.
_SENTENCE_MINIMUM = re.compile(r"(?:1개)?" + _NUMBER + r"원이상")
# The amount-tier layer: "<from>원 이상 ~ <to>원 미만 <fee>원" and "<from>원 이상 <fee>원".
_TIER_BOUNDED = re.compile(_NUMBER + r"원이상~" + _NUMBER + r"원미만" + _NUMBER + r"원")
_TIER_OPEN = re.compile(_NUMBER + r"원이상" + _NUMBER + r"원")
# ADR-0032 §4 (the owner's rule, Issue #219 6086421199): a fee stated as a range, ``A원 ~ B원``,
# is read as its highest amount.
_FEE_RANGE = re.compile(_NUMBER + r"원?[~∼〜～]" + _NUMBER + r"원")
_URL_MATERIAL = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*:)?//\S*")
_DIGIT = re.compile(r"\d")


def _squash(text: str) -> str:
    return "".join(text.split())


def _amount(match: re.Match[str], group: int = 1) -> int:
    return int(match.group(group).replace(",", ""))


def _quote(text: str) -> str:
    return _URL_MATERIAL.sub("[URL]", text)[:_EVIDENCE_LIMIT]


def _carries_url(text: str) -> bool:
    return _URL_MATERIAL.search(text) is not None


def _evidence(locator: str, status: FieldStatus, observed: str | None = None) -> Evidence:
    return Evidence(
        EvidenceKind.DOM_TEXT,
        locator,
        status,
        observed=None if observed is None else _quote(observed),
    )


def _absent(locator: str) -> FieldFact:
    return FieldFact(FieldStatus.ABSENT, None, (_evidence(locator, FieldStatus.ABSENT),))


def _review(locator: str, *observed: str) -> FieldFact:
    entries = [_evidence(locator, FieldStatus.REVIEW_REQUIRED, text) for text in observed[:3]]
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        tuple(entries) or (_evidence(locator, FieldStatus.REVIEW_REQUIRED),),
    )


def _row_locator(label: str) -> str:
    return f"dt:{label} + dd"


def rows(nodes: Sequence[Node]) -> Iterator[tuple[str, str, Node]]:
    """Every (label, value, dd) row of the product's information list, in source order. A ``dt``
    pairs only with the ``dd`` of its own ``dl``."""
    for dl in nodes:
        if dl.tag != "dl" or not dl.within(INFO_LIST):
            continue
        children = [child for child in dl.content if isinstance(child, Node)]
        labels = [child for child in children if child.tag == "dt"]
        values = [child for child in children if child.tag == "dd"]
        if len(labels) == 1 and len(values) == 1 and labels[0].text:
            yield labels[0].text, values[0].text, values[0]


def _labelled(
    found: Sequence[tuple[str, str, Node]], labels: Sequence[str]
) -> list[tuple[str, str, Node]]:
    return [row for row in found if row[0] in labels]


def _disagreement(locator: str, values: Sequence[str]) -> FieldFact | None:
    distinct = list(dict.fromkeys(" ".join(value.split()) for value in values))
    return _review(locator, *distinct) if len(distinct) > 1 else None


def _text_fact(locator: str, text: str, kind: EvidenceKind = EvidenceKind.DOM_TEXT) -> FieldFact:
    if _carries_url(text):
        return _review(locator, text)
    return FieldFact(
        FieldStatus.CONFIRMED,
        TextValue(text=text),
        (Evidence(kind, locator, FieldStatus.CONFIRMED, observed=_quote(text)),),
    )


# ---------------------------------------------------------------- fields


def _declared_name(nodes: Sequence[Node]) -> str:
    # Cleaned as page text is (godomall-2, ADR-0035): a no-break space is a space.
    return clean(meta(nodes).get("og:title", ""))


def _name_headings(nodes: Sequence[Node], words: Vocabulary) -> list[str]:
    return [
        node.text
        for node in nodes
        if node.tag == "h3" and node.within(words.name_heading) and node.text
    ]


def _name(nodes: Sequence[Node], words: Vocabulary) -> FieldFact:
    declared = _declared_name(nodes)
    headings = _name_headings(nodes, words)
    if not declared and not headings:
        return _absent("meta[og:title]")
    disagreeing = _disagreement(
        f".{words.name_heading} h3", [*([declared] if declared else []), *headings]
    )
    if disagreeing is not None:
        return disagreeing
    if declared:
        return _text_fact("meta[og:title]", declared, EvidenceKind.ATTRIBUTE)
    return _text_fact(f".{words.name_heading} h3", headings[0])


def _price_amount(text: str) -> int | None:
    squashed = _squash(text)
    stated = _BARE_NUMBER.fullmatch(squashed) or _PRICE_WITH_NOTES.fullmatch(squashed)
    return None if stated is None else _amount(stated)


def _prices(found: Sequence[tuple[str, str, Node]], words: Vocabulary) -> FieldFact:
    labels = tuple(dict.fromkeys((*words.price, *words.purchase_price, *words.list_price)))
    prices: list[SourcePrice] = []
    evidence: list[Evidence] = []
    for label, value, _dd in _labelled(found, labels):
        locator = _row_locator(label)
        amount = _price_amount(value)
        if amount is None:
            return _review(locator, value)
        repeated = [price for price in prices if price.label == label]
        if repeated and repeated[0].amount_krw != amount:
            return _review(locator, value)
        if repeated:
            continue
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
                locator,
                FieldStatus.CONFIRMED,
                observed=_quote(value),
                normalized=str(amount),
            )
        )
    locator = _row_locator(labels[0])
    if not prices:
        return _absent(locator)
    return FieldFact(FieldStatus.CONFIRMED, PricesValue(prices=tuple(prices)), tuple(evidence))


def _sentences(block: Node | None) -> list[str]:
    """The description's sentence units (ADR-0034 §1): every paragraph and list item that holds no
    other one, so no unit's words are read twice."""
    if block is None:
        return []
    return [
        node.text
        for node in block.descendants()
        if node.tag in ("p", "li")
        and node.text
        and not any(inner.tag in ("p", "li") for inner in node.descendants())
    ]


def _description_block(nodes: Sequence[Node], words: Vocabulary) -> tuple[Node | None, bool]:
    """The closed description text block, and whether the page proves where it ends."""
    blocks = [
        node
        for node in nodes
        if node.marks_exactly(words.detail_text) and node.within(words.detail_container)
    ]
    if not blocks:
        return None, True
    if len(blocks) > 1 or not blocks[0].closed:
        return None, False
    return blocks[0], True


def _occurrences(text: str, phrase: str) -> list[int]:
    """Where ``phrase`` starts in ``text``, both squashed, every occurrence."""
    found: list[int] = []
    at = text.find(phrase)
    while at >= 0:
        found.append(at)
        at = text.find(phrase, at + 1)
    return found


def _per_quantity_minimum(squashed: str) -> int | None:
    """ADR-0035 §3 (QM-01): the one-unit amount of a minimum row that is exactly a list of
    quantity minimums, ``1개 N원 이상`` with N > 0 first, and then ``k개 M원 이상`` with distinct
    quantities k ≥ 2.

    The other quantities stay only in the evidence; they are never divided into a unit price. None
    for any other shape: a first entry that is not 1개, a zero one-unit amount (no minimum
    statement), other words, a repeated quantity, or an amount the list does not account for."""
    if _QUANTITY_LIST.fullmatch(squashed) is None:
        return None
    entries = [
        (int(found.group(1)), _amount(found, 2)) for found in _QUANTITY_MINIMUM.finditer(squashed)
    ]
    quantities = [quantity for quantity, _stated in entries]
    if quantities[0] != 1 or len(set(quantities)) != len(quantities) or entries[0][1] <= 0:
        return None
    return entries[0][1]


def _minimum_sale_price(
    nodes: Sequence[Node], found: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    stated = _labelled(found, words.minimum_price)
    disagreeing = _disagreement(
        _row_locator(stated[0][0]) if stated else "", [v for _, v, _n in stated]
    )
    if stated and disagreeing is not None:
        return disagreeing
    row_amount: int | None = None
    if stated:
        squashed_cell = _squash(stated[0][1])
        if NO_MINIMUM_WORD in squashed_cell and not _DIGIT.search(squashed_cell):
            # ADR-0030 §2: "자율" states that there is no minimum.
            stated = []
        else:
            cell = _MINIMUM_CELL.fullmatch(squashed_cell)
            row_amount = _amount(cell) if cell is not None else _per_quantity_minimum(squashed_cell)
            if row_amount is None:
                return _review(_row_locator(stated[0][0]), stated[0][1])
    block, closed = _description_block(nodes, words)
    sentence_locator = f"#{words.detail_container} .{words.detail_text} p"
    amounts: list[tuple[int, str, str]] = []
    if words.minimum_price_phrase:
        if not closed:
            return _review(sentence_locator)
        units = _sentences(block)
        whole = _squash(block.text) if block is not None else ""
        for phrase in words.minimum_price_phrase:
            key = _squash(phrase)
            in_units = 0
            for sentence in units:
                squashed = _squash(sentence)
                for at in _occurrences(squashed, key):
                    in_units += 1
                    # ADR-0034 §1: the per-unit amount directly after the phrase, and nothing else.
                    stated_amount = _SENTENCE_MINIMUM.match(squashed, at + len(key))
                    if stated_amount is None:
                        return _review(sentence_locator, sentence)
                    amounts.append((_amount(stated_amount), phrase, sentence))
            if len(_occurrences(whole, key)) != in_units:
                # The phrase is written outside every sentence unit: a statement no rule reads.
                return _review(sentence_locator)
    distinct = {amount for amount, _p, _s in amounts}
    if row_amount is not None:
        distinct.add(row_amount)
    if len(distinct) > 1:
        return _review(sentence_locator, *(s for _a, _p, s in amounts))
    if amounts:
        amount, phrase, sentence = amounts[0]
        return FieldFact(
            FieldStatus.CONFIRMED,
            MoneyValue(label=phrase, amount_krw=amount),
            (
                Evidence(
                    EvidenceKind.DOM_TEXT,
                    sentence_locator,
                    FieldStatus.CONFIRMED,
                    observed=_quote(sentence),
                    normalized=str(amount),
                ),
            ),
        )
    if row_amount is not None:
        label, value, _ = stated[0]
        return FieldFact(
            FieldStatus.CONFIRMED,
            MoneyValue(label=label, amount_krw=row_amount),
            (
                Evidence(
                    EvidenceKind.DOM_TEXT,
                    _row_locator(label),
                    FieldStatus.CONFIRMED,
                    observed=_quote(value),
                    normalized=str(row_amount),
                ),
            ),
        )
    return _absent(_row_locator(words.minimum_price[0]))


def _shipping(found: Sequence[tuple[str, str, Node]], words: Vocabulary) -> FieldFact:
    fee = _labelled(found, words.shipping_fee)
    locator = _row_locator(words.shipping_fee[0])
    if not fee:
        return _absent(locator)
    if len(fee) > 1:
        return _review(locator, *(v for _, v, _n in fee))
    _label, _value, dd = fee[0]
    # The base fee: the row's first strong statement, exactly one amount of won, or exactly a
    # range, which is read as its highest amount (ADR-0032 §4).
    strongs = [node for node in dd.content if isinstance(node, Node) and node.tag == "strong"]
    stated = _squash(strongs[0].text) if strongs else ""
    base = _ONE_AMOUNT.fullmatch(stated)
    ranged = _FEE_RANGE.fullmatch(stated)
    if base is not None:
        base_fee = _amount(base)
    elif ranged is not None:
        base_fee = max(_amount(ranged, 1), _amount(ranged, 2))
    else:
        return _review(locator, dd.text)
    tiers = [node for node in dd.descendants() if node.marks_exactly(f"#{FEE_TIERS}")]
    regions = [node for node in dd.descendants() if node.marks_exactly(f"#{REGION_FEES}")]
    if len(tiers) > 1 or len(regions) > 1:
        return _review(locator, dd.text)
    tier_items = [node.text for node in tiers[0].descendants() if node.tag == "li"] if tiers else []
    if tiers and len(_ONE_AMOUNT.findall(_squash(tiers[0].text))) != sum(
        len(_ONE_AMOUNT.findall(_squash(item))) for item in tier_items
    ):
        # The tier layer states an amount outside its tiers: a condition no rule reads.
        return _review(locator, tiers[0].text)
    # ADR-0035 §2 (RS-01), replacing ADR-0034 §2's hold: a region layer's words are a surcharge
    # kept beside the base fee and never priced. The layer's own title is a heading, not words it
    # states; a layer with nothing else in it states no surcharge.
    region_words = regions[0].words_outside(REGION_TITLE) if regions else ""
    if regions and not region_words:
        pictured = [
            node
            for node in regions[0].descendants()
            if node.tag == "img" and not node.within(REGION_CLOSE)
        ]
        if pictured:
            # ADR-0035 §2: a layer that states its surcharge only as an image cannot be kept as
            # words. The layer's own close control is no statement.
            return _review(locator, dd.text)
    if len(region_words) > REGION_WORDS_LIMIT:
        # ADR-0035 §2: words longer than a policy text keeps are held, never cut.
        return _review(locator, region_words)
    outside = _squash(dd.text_outside(f"#{FEE_TIERS}", f"#{REGION_FEES}"))
    # Outside the layers the row may hold only its base fee, the payment words and the
    # platform's button labels; any other amount is a condition no rule reads.
    if len(_ONE_AMOUNT.findall(outside)) != len(_ONE_AMOUNT.findall(stated)):
        return _review(locator, dd.text)
    if ranged is not None and tiers:
        # A range beside an amount-tier layer is two conditions at once: no rule reads it.
        return _review(locator, dd.text)
    policy = (
        f"배송비 {strongs[0].text}"
        + (f" · 금액별배송비 {' / '.join(tier_items)}" if tier_items else "")
        # The words stay visible beside the price, the region layer's included; an empty layer
        # is named as one.
        + (f" · 지역별추가배송비 {region_words or '(금액 표시 없음)'}" if regions else "")
    )
    if _carries_url(policy):
        # URL material in any of the words, the region layer's included, is never kept.
        return _review(locator, policy)
    evidence: tuple[Evidence, ...] = (_evidence(locator, FieldStatus.CONFIRMED, policy),)
    if ranged is not None:
        # The evidence names the reading: the highest amount of the range.
        evidence += (
            Evidence(
                EvidenceKind.DOM_TEXT,
                locator,
                FieldStatus.CONFIRMED,
                observed=_quote(strongs[0].text),
                normalized=str(base_fee),
            ),
        )
    if not tier_items:
        kind = ShippingKind.FREE if base_fee == 0 else ShippingKind.FIXED
        return FieldFact(
            FieldStatus.CONFIRMED,
            ShippingValue(
                kind=kind,
                policy_text=policy,
                fee_krw=None if kind is ShippingKind.FREE else base_fee,
            ),
            evidence,
        )
    # ADR-0034 §2: exactly "[0, T) base fee" and "[T, ∞) free" is a free-over policy.
    if len(tier_items) == 2:
        bounded = _TIER_BOUNDED.fullmatch(_squash(tier_items[0]))
        opened = _TIER_OPEN.fullmatch(_squash(tier_items[1]))
        if (
            bounded is not None
            and opened is not None
            and _amount(bounded, 1) == 0
            and _amount(bounded, 3) == base_fee
            and _amount(bounded, 2) == _amount(opened, 1)
            and _amount(opened, 2) == 0
            and base_fee > 0
        ):
            return FieldFact(
                FieldStatus.CONFIRMED,
                ShippingValue(
                    kind=ShippingKind.CONDITIONAL,
                    policy_text=policy,
                    fee_krw=base_fee,
                    free_over_krw=_amount(opened, 1),
                ),
                evidence,
            )
    return _review(locator, policy)


def _stock(
    nodes: Sequence[Node], found: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    """ADR-0010 §10, read only inside the purchase-control box."""
    box = [node for node in nodes if node.within(CHOICE_BOX) or node.marks_exactly(CHOICE_BOX)]
    active = [
        node
        for node in box
        if not node.hidden
        and not node.disabled
        and any(node.marks_exactly(name) for name in words.purchase_controls)
    ]
    sold_out = [
        node
        for node in box
        if not node.hidden
        and (
            any(node.marks_exactly(name) for name in words.sold_out_controls)
            or (
                len(node.visible_text) <= MARK_LIMIT
                and any(word in node.visible_text for word in words.sold_out)
            )
        )
    ]
    locator = f".{CHOICE_BOX} button"
    stock_rows = _labelled(found, (STOCK_ROW,))
    none_left = any(_squash(value) == "0개" for _label, value, _dd in stock_rows)
    if active and none_left:
        # The page says both "buy" and "none left": a disagreement, never an offer.
        return _review(_row_locator(STOCK_ROW), *(v for _, v, _n in stock_rows))
    if active:
        return FieldFact(
            FieldStatus.CONFIRMED,
            StockValue(availability=Availability.ON_SALE),
            (
                Evidence(
                    EvidenceKind.CONTROL_STATE,
                    locator,
                    FieldStatus.CONFIRMED,
                    observed="purchase control",
                ),
            ),
        )
    if sold_out:
        return FieldFact(
            FieldStatus.CONFIRMED,
            StockValue(availability=Availability.SOLD_OUT),
            (
                _evidence(
                    locator, FieldStatus.CONFIRMED, sold_out[0].visible_text or "sold-out control"
                ),
            ),
        )
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        (Evidence(EvidenceKind.CONTROL_STATE, locator, FieldStatus.REVIEW_REQUIRED),),
    )


def _options(nodes: Sequence[Node]) -> FieldFact:
    """``ABSENT`` — the Product DB's proof of "no options" (ADR-0013 ruling B) — only when the page
    shows it: the order list exists with exactly one line written in advance, and the product has
    no option control of any kind, hidden or not (a skin may hide the real ``select``). A product
    with options starts its order list empty; every other shape, a sold-out page without an order
    list included, is held for review."""
    locator = f".{CHOICE_LIST}"
    lists = [node for node in nodes if node.marks_exactly(f".{CHOICE_LIST}")]
    if len(lists) != 1 or not lists[0].closed:
        return _review(locator)
    lines = [
        node
        for node in lists[0].descendants()
        if node.tag == "tbody" and node.element_id.startswith(CHOICE_LINE)
    ]
    controls = [
        node
        for node in nodes
        if node.tag == "select"
        or any(name.startswith("option_select") for name in node.classes)
        or node.element_id.startswith("option_select")
    ]
    if len(lines) == 1 and not controls:
        return _absent(locator)
    return _review(locator)


def _quantity_tiers(found: Sequence[tuple[str, str, Node]]) -> FieldFact:
    """No accepted rule says how a Godomall skin lays out a quantity tier. A row that names one is
    held for review; otherwise the page states none."""
    named = [(label, value) for label, value, _n in found if "수량" in label and "할인" in label]
    locator = _row_locator("수량별 할인")
    return (
        _review(locator, *(f"{label} {value}" for label, value in named))
        if named
        else _absent(locator)
    )


def _detail_description(nodes: Sequence[Node], words: Vocabulary) -> FieldFact:
    block, closed = _description_block(nodes, words)
    locator = f"#{words.detail_container} .{words.detail_text}"
    if not closed:
        return _review(locator)
    if block is None:
        return _absent(locator)
    text = block.text
    if not text:
        return _review(locator)
    return _text_fact(locator, text, EvidenceKind.PRODUCT_HTML_FRAGMENT)


def _notice(nodes: Sequence[Node]) -> FieldFact:
    locator = f"dt:{NOTICE_WORDS[0]} + dd"
    shown = [
        node
        for node in nodes
        if len(node.text) <= MARK_LIMIT and any(word in _squash(node.text) for word in NOTICE_WORDS)
    ]
    return _review(locator) if shown else _absent(locator)


def _unread_channel_words(value: str, phrases: Sequence[str]) -> str:
    """What a channel row says beyond the configured phrases it matches and the separators between
    them (``/``, ``,``, ``·``), whitespace ignored. Longer phrases are set aside first, so a phrase
    inside a longer one never leaves the longer one's remainder behind."""
    left = _squash(value)
    for phrase in sorted({_squash(p) for p in phrases if _squash(p)}, key=len, reverse=True):
        left = left.replace(phrase, "")
    return left.translate(_CHANNEL_SEPARATORS)


def _sales_channels(
    nodes: Sequence[Node], found: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    """ADR-0031 §3: what the page says about where the product may be resold.

    Only the site's own phrases are read, from its sales-channel rows, the product's name (its
    restricting phrases only, ADR-0035 §1) and the closed description block's text; nothing is
    read from an image. Fail closed throughout:
    - every channel row must be read; a row no phrase reads, or one that holds only an image or
      nothing, is held for review;
    - "all allowed" is read only from a row whose whole value is an allowed phrase, never from a
      phrase inside other words, and never from a name;
    - a row that is not wholly allowed reads as its restrictions (ADR-0035 NR-03, so one row
      stating an allowed phrase and restrictions is ``LISTED``) only when every word of it is a
      matched configured phrase or a separator; anything left over is held for review;
    - an allowed row beside a restriction stated elsewhere (another row, the name or the
      description) is a contradiction, held for review.
    A restricting phrase is read wherever it appears.
    """
    row_locator = _row_locator(words.sales_channel_row[0])
    detail_locator = f"#{words.detail_container} .{words.detail_text}"
    headed = [node for node in nodes if node.tag == "dt" and node.text in words.sales_channel_row]
    stated = _labelled(found, words.sales_channel_row)
    if len(headed) > len(stated):
        # A channel row whose value is an image or nothing: a statement no rule can read.
        return _review(row_locator, *(v for _, v, _n in stated))
    block, closed = _description_block(nodes, words)
    if not closed:
        return _review(detail_locator)
    description = "" if block is None else block.text

    def contains(text: str, phrases: Sequence[str]) -> list[str]:
        return [phrase for phrase in phrases if _squash(phrase) in _squash(text)]

    def restrictions(text: str) -> dict[str, list[str]]:
        return {
            "closed": contains(text, words.channel_closed_only),
            "coupang": contains(text, words.channel_forbid_coupang),
            "smartstore": contains(text, words.channel_forbid_smartstore),
        }

    allowed_rows: list[str] = []
    hits: dict[str, list[str]] = {"closed": [], "coupang": [], "smartstore": []}
    # What was read, and where: (locator, the words quoted).
    quoted: list[tuple[str, str]] = []
    configured = (
        *words.channel_all_allowed,
        *words.channel_closed_only,
        *words.channel_forbid_coupang,
        *words.channel_forbid_smartstore,
    )
    for _label, row_value, _node in stated:
        row_restrictions = restrictions(row_value)
        whole_allowed = any(_squash(row_value) == _squash(p) for p in words.channel_all_allowed)
        if not whole_allowed and not any(row_restrictions.values()):
            # A row no phrase reads, or an allowed phrase inside other words: never allowed.
            return _review(row_locator, row_value)
        if not whole_allowed and _unread_channel_words(row_value, configured):
            # ADR-0035 §1 NR-03: a row that is not wholly allowed reads as its restrictions only
            # when every word of it is read. Words left over (an unconfigured ban such as
            # "스마트스토어 등록 불가") are a statement no rule reads (ADR-0031 SC-02).
            return _review(row_locator, row_value)
        if whole_allowed:
            allowed_rows.append(row_value)
        for key, phrases in row_restrictions.items():
            hits[key].extend(phrases)
        quoted.append((row_locator, row_value))
    # ADR-0035 §1 (NR-01): the product's name is read for the restricting phrases only, from the
    # sources ``original_name`` reads. An allowed reading never comes from a name.
    names = [("meta[og:title]", _declared_name(nodes))] + [
        (f".{words.name_heading} h3", heading) for heading in _name_headings(nodes, words)
    ]
    sources = [*((locator, text) for locator, text in names if text), (detail_locator, description)]
    for locator, text in sources:
        for key, phrases in restrictions(text).items():
            hits[key].extend(phrases)
            quoted.extend((locator, phrase) for phrase in phrases)
    restricted = any(hits.values())
    if allowed_rows and restricted:
        # NR-02: a restriction in the name or the description beside an all-allowed row.
        return _review(row_locator, *dict.fromkeys(text for _l, text in quoted))
    if not allowed_rows and not restricted:
        return _absent(row_locator)
    assert {"coupang", "smartstore"} <= SALES_CHANNEL_MARKETPLACES
    policy = _quote(" / ".join(dict.fromkeys(text for _l, text in quoted)))
    evidence = tuple(
        _evidence(locator, FieldStatus.CONFIRMED, text) for locator, text in dict.fromkeys(quoted)
    )
    if hits["closed"]:
        value = SalesChannelsValue(scope=SalesChannelScope.CLOSED_MALL_ONLY, policy_text=policy)
    elif restricted:
        forbidden = tuple(sorted(key for key in ("coupang", "smartstore") if hits[key]))
        value = SalesChannelsValue(
            scope=SalesChannelScope.LISTED, forbidden=forbidden, policy_text=policy
        )
    else:
        value = SalesChannelsValue(scope=SalesChannelScope.ALL_ALLOWED, policy_text=policy)
    return FieldFact(FieldStatus.CONFIRMED, value, evidence)


def _text_row(found: Sequence[tuple[str, str, Node]], labels: Sequence[str]) -> FieldFact:
    stated = _labelled(found, labels)
    if not stated:
        return _absent(_row_locator(labels[0]))
    disagreeing = _disagreement(_row_locator(stated[0][0]), [v for _, v, _n in stated])
    if disagreeing is not None:
        return disagreeing
    label, value, _ = stated[0]
    return _text_fact(_row_locator(label), value)


def parse_fields(
    document: DocumentView, words: Vocabulary = DEFAULT_VOCABULARY
) -> dict[str, FieldFact]:
    """Every source-truth field this document states, and the evidence for each."""
    nodes = read(document.body)
    found = tuple(rows(nodes))
    return {
        "original_name": _name(nodes, words),
        "prices": _prices(found, words),
        "options": _options(nodes),
        "stock": _stock(nodes, found, words),
        "shipping": _shipping(found, words),
        "minimum_sale_price": _minimum_sale_price(nodes, found, words),
        "quantity_tiers": _quantity_tiers(found),
        "brand": _text_row(found, words.brand),
        "manufacturer": _text_row(found, words.manufacturer),
        "origin": _text_row(found, words.origin),
        "notice": _notice(nodes),
        "detail_description": _detail_description(nodes, words),
        "sales_channels": _sales_channels(nodes, found, words),
    }
