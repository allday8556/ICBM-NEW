"""What a Cafe24 product page states about itself (ADR-0010 §7–§8, ADR-0030 §2, §4).

Derived from KM통상's parser (ruling 5702780630 P1, ``kmretail-3``), with the words and regions a
site may vary held in a :class:`Vocabulary`. Its defaults are KM통상's. A site configuration may
extend a slot with plain words, or move a region (ADR-0030 §3).

The template is stricter than KM통상's parser wherever that parser reads a first value, a guess or
nothing at all. These are its stated differences (ADR-0030 §10), and each is fail closed:

- Several declarations of one fact that disagree are held for review, never the first one read.
- A money cell is read only when it states exactly one amount. Anything else in it — a condition,
  a second amount or no amount — holds the field for review. A fee cell that says exactly 무료
  states free shipping, and one that is exactly a range (outside the platform's tier tooltip) is
  priced at its highest amount (ADR-0032 §4, the owner's rule).
- Stock is decided only by controls and marks inside the product's own action area. A hidden or
  disabled control is no offer. An active control decides ``ON_SALE`` (ADR-0010 §10), and a
  sold-out mark decides ``SOLD_OUT`` only when no active control exists.
- The description is read only from a description block the page closed. Its whole text is the
  value, and page text that carries URL material is held for review rather than stored.
- A notice the page shows is held for review. It is never reported absent without looking.
- Options are ``ABSENT`` only when the page writes its option container with no axis in it; a page
  without the container is held for review.
- ``cafe24-3`` (ADR-0035):
  - a region-surcharge row (``추가배송비``) is kept as words in the shipping policy text and never
    priced. One stated twice in different words, carrying URL material, longer than 200
    characters, or whose value is only an image, is held for review, and one without a fee row
    holds the field. An empty surcharge cell states no surcharge. KM통상's parser ignores the row;
  - text is cleaned of the byte-order mark, the zero-width space and the word joiner, and a
    no-break space is a space, in page text and in the declared title. A text made only of them is
    empty: a description is held for review, and a row's value cell is an empty cell, so the row
    is not stated (a minimum row of only U+FEFF is ``ABSENT``). KM통상's parser keeps them;
  - a minimum row that is exactly a per-quantity list with a one-unit amount above zero reads that
    amount, where KM통상's parser reads the cell's first number (the quantity 1);
  - the product's name is read for restricting channel phrases, and a channel row that is not
    wholly allowed is read only when every word of it is a configured phrase or a separator.
    KM통상 reports no channel fact.

It reads; it never fetches, stores, hashes, persists or retries. A field the page does not state is
``ABSENT``. A field whose evidence is present but cannot be read from the document alone is
``REVIEW_REQUIRED``. Nothing is inferred, averaged, converted or completed, and no value comes from
a script. Every locator is written here, never taken from the page.
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
from integrations.suppliers.platforms.cafe24.collect.dom import Node, clean, meta, read

# The word a page writes in the minimum-price row when the reseller sets the price freely
# (판매가 자율): the page states that there is no minimum. The user's rule of 2026-10-03.
NO_MINIMUM_WORD = "자율"
# The words a page would have to use to state a quantity tier. No accepted rule says how a Cafe24
# skin lays one out, so a tier is never read into a value.
TIER_LABELS = ("수량별 가격", "수량별 할인", "수량 할인", "수량별")
# A product information notice: its heading, or the labels of its own first rows. Spaces removed.
NOTICE_WORDS = ("상품정보제공고시", "상품정보고시", "품명및모델명", "제조국")
# The product's own action area: the only place a purchase control or a sold-out mark is read.
ACTION_AREA = "xans-product-action"
# The longest visible text a sold-out mark may be; anything longer is a sentence, not a mark.
MARK_LIMIT = 40


@dataclass(frozen=True)
class Vocabulary:
    """The words and regions one site uses. A label is site knowledge; a row whose label is not
    one of these is not read as that field. A region is ``#id``, ``.class`` or a bare token."""

    name: tuple[str, ...] = ("상품명",)
    price: tuple[str, ...] = ("판매가", "소비자가", "정가", "공급가")
    minimum_price: tuple[str, ...] = ("최저지도가", "최저판매가")
    shipping_method: tuple[str, ...] = ("배송방법",)
    shipping_fee: tuple[str, ...] = ("배송비",)
    # ADR-0035 §2: a region-surcharge row. Its words are kept beside the base fee, never priced.
    shipping_region_fee: tuple[str, ...] = ("추가배송비",)
    brand: tuple[str, ...] = ("브랜드",)
    manufacturer: tuple[str, ...] = ("제조사",)
    origin: tuple[str, ...] = ("원산지",)
    sold_out: tuple[str, ...] = ("품절", "SOLD OUT", "SOLDOUT", "일시품절")
    # Controls the reader can act on: the older skins' and the smart-design skins' own (cafe24-2).
    purchase_controls: tuple[str, ...] = (
        "btnBuy",
        "btnBasket",
        "btnCart",
        "actionBuy",
        "actionCart",
    )
    # ADR-0032 §2: the labels whose price is the purchase price, and those that are list prices.
    # A template declares none; only a site's words give a price a role.
    purchase_price: tuple[str, ...] = ()
    list_price: tuple[str, ...] = ()
    # ADR-0031 §3: the sales-channel row label, and the phrases that read it. The phrases are
    # empty by default, so a row a site has not worded stays under review, never allowed.
    sales_channel_row: tuple[str, ...] = ("판매가능플랫폼",)
    channel_all_allowed: tuple[str, ...] = ()
    channel_closed_only: tuple[str, ...] = ()
    channel_forbid_coupang: tuple[str, ...] = ()
    channel_forbid_smartstore: tuple[str, ...] = ()
    option_container: str = "xans-product-option"
    detail_container: str = "prdDetail"
    # The tab strip a skin repeats above every detail panel. Its words are navigation.
    detail_menu: str = "dMenu"
    # The representative image's container inside the product image module (cafe24-2).
    key_image: str = "keyImg"
    # The words a skin appends to the declared title after the product's name (cafe24-2).
    title_suffix: tuple[str, ...] = ()


DEFAULT_VOCABULARY = Vocabulary()

_EVIDENCE_LIMIT = 200
# One amount as a page groups it: plain digits, or thousands separated by commas.
_NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)"
_PRICE_CELL = re.compile(_NUMBER + r"원?")
# A price cell: one amount, then only a discount rate and a tax note, the words a price row adds
# without changing what it states. Any other word (이상, 부터, a second amount) is a condition.
_PRICE_WITH_NOTES = re.compile(
    _NUMBER + r"원(?:\d{1,3}%)?(?:\(?(?:부가세|VAT)(?:포함|별도)\)?)?", re.IGNORECASE
)
_FEE_CELL = re.compile(_NUMBER + r"원")
# A minimum is the least the reseller may charge, so "29,500원 이상" states exactly that minimum.
_MINIMUM_CELL = re.compile(_NUMBER + r"원(?:이상)?")
# ADR-0035 §3: one entry of a per-quantity minimum row, "<k>개 <amount>원 이상" with whitespace
# removed, and a cell that is exactly a list of them, separated by "/" or ",". Entries separated
# only by whitespace, or by nothing at all, are accepted too: a line break (``<br>``) leaves no
# mark in the collapsed text, so it cannot be told apart from them. An entry ends at 이상, so the
# next quantity starts unambiguously.
_QUANTITY_ENTRY = r"([1-9]\d*)개" + _NUMBER + r"원이상"
_QUANTITY_MINIMUM = re.compile(_QUANTITY_ENTRY)
_QUANTITY_LIST = re.compile(rf"{_QUANTITY_ENTRY}(?:[/,]?{_QUANTITY_ENTRY})*")
# ADR-0032 §4 (the owner's rule, Issue #219 6086421199): a fee stated as a range, ``A원 ~ B원``,
# is read as its highest amount.
_FEE_RANGE = re.compile(_NUMBER + r"원?[~∼〜～]" + _NUMBER + r"원")
# The platform's own quantity-tier tooltip inside a fee cell: it explains a stated range and states
# no fee of its own, so a range is read from the cell outside it.
FEE_TOOLTIP = ".ec-front-shop-delivery-defferent-shipping"
_FREE_WORDS = ("무료", "무료배송")
# URL material: a scheme or a protocol-relative reference. It is never quoted and never stored.
_URL_MATERIAL = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*:)?//\S*")
# Any URL shape in shipping words (a "//", a scheme followed by its address, or a domain-like
# name), the same detector the detail-guidance owner uses. Shipping words never name a domain, so
# a match is URL material; other texts keep the narrower ``_URL_MATERIAL`` test, because a maker's
# name such as "Co.KG" looks like a domain.
_URL_SHAPE = re.compile(
    r"//|\b(?:https?|ftp|file|data|javascript|mailto|tel|sms):(?!\s)"
    r"|[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b",
    re.IGNORECASE,
)
_DIGIT = re.compile(r"\d")
# ADR-0035 §2: the longest region-surcharge words kept as policy text; longer ones are held.
REGION_WORDS_LIMIT = 200
# ADR-0035 §1 NR-03: the separators a channel row puts between its phrases.
_CHANNEL_SEPARATORS = str.maketrans("", "", "/,·")
# Elements that only wrap words. A value cell holding any other element (an image, a link, a
# frame) but no words states something no rule reads.
_WRAPPERS = frozenset({"b", "br", "div", "em", "font", "i", "p", "span", "strong", "u"})


def _squash(text: str) -> str:
    return "".join(text.split())


def _amount(match: re.Match[str], group: int = 1) -> int:
    return int(match.group(group).replace(",", ""))


def _quote(text: str) -> str:
    """Evidence words: URL material removed, then bounded, so a quote never carries a URL."""
    return _URL_MATERIAL.sub("[URL]", text)[:_EVIDENCE_LIMIT]


def _carries_url(text: str) -> bool:
    return _URL_MATERIAL.search(text) is not None


def _carries_url_shape(text: str) -> bool:
    return _carries_url(text) or _URL_SHAPE.search(text) is not None


def _cells(nodes: Sequence[Node]) -> Iterator[tuple[str, Node]]:
    """Every (label, value cell) pair of the information tables, in source order, the cell
    whether or not it holds words.

    A row states a fact whether or not the storefront paints it, so a hidden row is read. What the
    row is *about* is its own heading cell, never a neighbour's: a heading waits for a value cell
    only within its own row.
    """
    pending: tuple[str, Node] | None = None
    for node in nodes:
        if node.tag == "tr":
            pending = None
        elif node.tag == "th":
            label = node.text
            pending = (label, node) if label else None
        elif node.tag == "td" and pending is not None:
            yield pending[0], node
            pending = None


def _rows(nodes: Sequence[Node]) -> Iterator[tuple[str, str, Node]]:
    """Every (label, value) pair of the information tables whose value cell holds words.

    A cell without words, one holding only invisible characters included (cafe24-3), states no
    value: the row is read as not stated, as an empty cell always was.
    """
    for label, cell in _cells(nodes):
        value = cell.text
        if value:
            yield label, value, cell


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


def _labelled(
    rows: Sequence[tuple[str, str, Node]], labels: Sequence[str]
) -> list[tuple[str, str, Node]]:
    return [row for row in rows if row[0] in labels]


def _row_locator(label: str) -> str:
    return f"th:{label} + td"


def _disagreement(locator: str, values: Sequence[str]) -> FieldFact | None:
    """Several declarations of one fact that do not agree: the page does not say which is true,
    so the field is held for review with each value quoted. None when they agree."""
    distinct = list(dict.fromkeys(" ".join(value.split()) for value in values))
    if len(distinct) <= 1:
        return None
    return _review(locator, *distinct)


def _text_fact(locator: str, text: str, kind: EvidenceKind = EvidenceKind.DOM_TEXT) -> FieldFact:
    """A text value, whole, or held for review when it carries URL material: such a value is
    never persisted, and the field never aborts the whole revision."""
    if _carries_url(text):
        # Held by its locator alone: the value, URL included, is never quoted into evidence.
        return _review(locator)
    return FieldFact(
        FieldStatus.CONFIRMED,
        TextValue(text=text),
        (Evidence(kind, locator, FieldStatus.CONFIRMED, observed=_quote(text)),),
    )


# ---------------------------------------------------------------- fields


def _declared_name(nodes: Sequence[Node]) -> str:
    return clean(meta(nodes).get("og:title", ""))


def _name(
    nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    # The declared title is cleaned as page text is (cafe24-3, ADR-0035): a no-break space or a
    # doubled space between its words is one space. cafe24-2 compared the raw title with the
    # cleaned row, so a title with two spaces in it never matched its row plus the suffix.
    declared = _declared_name(nodes)
    stated = _labelled(rows, words.name)
    locator = _row_locator(words.name[0])
    if not declared and not stated:
        return _absent("meta[og:title]")
    if (
        declared
        and len(stated) == 1
        and any(declared == clean(f"{stated[0][1]} {suffix}") for suffix in words.title_suffix)
    ):
        # A skin that appends the shop's own name to the declared title (U-PICK: "<name> -
        # U-PICK B2B") states the product's name in its 상품명 row. Only the exact suffix the site
        # names is set aside; any other difference is a disagreement.
        declared = ""
    disagreeing = _disagreement(
        locator, [*([declared] if declared else []), *(v for _, v, _n in stated)]
    )
    if disagreeing is not None:
        return disagreeing
    if declared:
        return _text_fact("meta[og:title]", declared, EvidenceKind.ATTRIBUTE)
    return _text_fact(locator, stated[0][1])


def _price_amount(text: str) -> int | None:
    """The one amount a price cell states: a bare number, or one amount of won followed only by a
    discount rate and a tax note. None for anything else, a condition such as 이상 included."""
    squashed = _squash(text)
    stated = _PRICE_CELL.fullmatch(squashed) or _PRICE_WITH_NOTES.fullmatch(squashed)
    return None if stated is None else _amount(stated)


def _prices(
    nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    prices: list[SourcePrice] = []
    evidence: list[Evidence] = []
    labels = tuple(dict.fromkeys((*words.price, *words.purchase_price, *words.list_price)))
    for label, value, _cell in _labelled(rows, labels):
        locator = _row_locator(label)
        amount = _price_amount(value)
        if amount is None:
            # A recognised price row that does not state exactly one amount is never skipped and
            # never read as its first amount.
            return _review(locator, value)
        repeated = [price for price in prices if price.label == label]
        if repeated and repeated[0].amount_krw != amount:
            return _review(locator, value)  # one label, two amounts
        if repeated:
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
                locator,
                FieldStatus.CONFIRMED,
                observed=_quote(value),
                normalized=str(amount),
            )
        )
    stated_amounts = {price.amount_krw for price in prices}
    declared = meta(nodes)
    for key in ("product:price:amount", "product:sale_price:amount"):
        stated = declared.get(key, "").strip()
        if not stated:
            continue
        number = _PRICE_CELL.fullmatch(_squash(stated).split(".")[0])
        if number is None or (prices and _amount(number) not in stated_amounts):
            # The page's own metadata names a price no row states: they disagree.
            return _review(f"meta[{key}]", stated)
        evidence.append(
            Evidence(EvidenceKind.ATTRIBUTE, f"meta[{key}]", FieldStatus.CONFIRMED, observed=stated)
        )
    locator = _row_locator(labels[0])
    if not prices:
        return _review(locator) if evidence else _absent(locator)
    return FieldFact(FieldStatus.CONFIRMED, PricesValue(prices=tuple(prices)), tuple(evidence))


def _minimum_sale_price(rows: Sequence[tuple[str, str, Node]], words: Vocabulary) -> FieldFact:
    stated = _labelled(rows, words.minimum_price)
    if not stated:
        # Never derived from a sale price: an unstated minimum is no minimum (CLAUDE.md §6.1).
        return _absent(_row_locator(words.minimum_price[0]))
    disagreeing = _disagreement(_row_locator(stated[0][0]), [v for _, v, _n in stated])
    if disagreeing is not None:
        return disagreeing
    label, value, _ = stated[0]
    locator = _row_locator(label)
    squashed = _squash(value)
    if NO_MINIMUM_WORD in squashed and not _DIGIT.search(squashed):
        # Stated, and what it states is that the price is free: no minimum, read from the page.
        return FieldFact(FieldStatus.ABSENT, None, (_evidence(locator, FieldStatus.ABSENT, value),))
    single = _MINIMUM_CELL.fullmatch(squashed)
    amount = _amount(single) if single is not None else _per_quantity_minimum(squashed)
    if amount is None:
        return _review(locator, value)
    return FieldFact(
        FieldStatus.CONFIRMED,
        MoneyValue(label=label, amount_krw=amount),
        (
            Evidence(
                EvidenceKind.DOM_TEXT,
                locator,
                FieldStatus.CONFIRMED,
                observed=_quote(value),
                normalized=str(amount),
            ),
        ),
    )


def _per_quantity_minimum(squashed: str) -> int | None:
    """ADR-0035 §3 (QM-01): the one-unit amount of a cell that is exactly a list of quantity
    minimums, ``1개 N원 이상`` with N > 0 first, and then ``k개 M원 이상`` with distinct
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


def _unread_cells(nodes: Sequence[Node], labels: Sequence[str]) -> list[Node]:
    """The value cells of rows headed by one of ``labels`` that hold no words but hold an element
    that is not a mere wrapper, an image above all: a statement no rule reads. A cell with nothing
    in it, or only invisible characters, states nothing and is not one of them."""
    return [
        cell
        for label, cell in _cells(nodes)
        if label in labels
        and not cell.text
        and any(node.tag not in _WRAPPERS for node in cell.descendants())
    ]


def _shipping(
    nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    method = _labelled(rows, words.shipping_method)
    fee = _labelled(rows, words.shipping_fee)
    region = _labelled(rows, words.shipping_region_fee)
    fee_locator = _row_locator(words.shipping_fee[0])
    region_locator = _row_locator(words.shipping_region_fee[0])
    if _unread_cells(nodes, words.shipping_region_fee):
        # ADR-0035 §2: a surcharge row whose value is only an image cannot be kept as words. An
        # empty surcharge cell states no surcharge and is read as no row (cafe24-3).
        return _review(region_locator, *(v for _, v, _n in region))
    if not method and not fee and not region:
        return _absent(fee_locator)
    if not fee:
        # A method or a region surcharge without a fee states no amount; a guessed one would be
        # an invention.
        return _review(fee_locator, *(v for _, v, _n in (*method, *region)))
    if region:
        # ADR-0035 §2 (RS-01): a region surcharge is kept as words beside the base fee and never
        # priced. One stated twice in different words, one carrying URL material, or one longer
        # than the words a policy text keeps, is held.
        region_locator = _row_locator(region[0][0])
        disagreeing = _disagreement(region_locator, [v for _, v, _n in region])
        if disagreeing is not None:
            return disagreeing
        if _carries_url_shape(region[0][1]):
            # Held by its locator alone: URL material is never quoted into evidence.
            return _review(region_locator)
        if len(region[0][1]) > REGION_WORDS_LIMIT:
            return _review(region_locator, region[0][1])
    if any(_DIGIT.search(v) for _, v, _n in method):
        # A method row that names an amount states a condition beside the fee.
        return _review(_row_locator(words.shipping_method[0]), *(v for _, v, _n in method))
    disagreeing = _disagreement(fee_locator, [v for _, v, _n in fee])
    if disagreeing is not None:
        return disagreeing
    cell = _squash(fee[0][1])
    ranged = _FEE_RANGE.fullmatch(_squash(fee[0][2].text_outside(FEE_TOOLTIP)))
    range_evidence: tuple[Evidence, ...] = ()
    if cell in _FREE_WORDS:
        amount = 0
    elif ranged is not None:
        # ADR-0032 §4: a cell that is exactly a range (outside the platform's tier tooltip) is
        # priced at its highest amount; its words are kept and the evidence names the reading.
        amount = max(int(group.replace(",", "")) for group in ranged.groups())
        range_evidence = (
            Evidence(
                EvidenceKind.DOM_TEXT,
                fee_locator,
                FieldStatus.CONFIRMED,
                observed=_quote(fee[0][1]),
                normalized=str(amount),
            ),
        )
    else:
        stated = _FEE_CELL.fullmatch(cell)
        if stated is None:
            # Not exactly one amount: a threshold, a range or a free-over amount is a condition,
            # never flattened into an amount it names (ADR-0010 §7).
            return _review(fee_locator, fee[0][1])
        amount = _amount(stated)
    # The surcharge's words, once: the same words stated twice are one statement.
    stated_rows = (*method, *fee, *region[:1])
    policy = " / ".join(f"{label} {value}" for label, value, _ in stated_rows)
    if _carries_url_shape(policy):
        # Held by its locator alone: URL material is never quoted into evidence.
        return _review(fee_locator)
    # The kind and the fee are the base fee's alone (RS-01).
    kind = ShippingKind.FREE if amount == 0 else ShippingKind.FIXED
    evidence = (
        tuple(
            Evidence(
                EvidenceKind.DOM_TEXT,
                _row_locator(label),
                FieldStatus.CONFIRMED,
                observed=_quote(value),
            )
            for label, value, _cell in stated_rows
        )
        + range_evidence
    )
    value = ShippingValue(
        kind=kind, policy_text=policy, fee_krw=None if kind is ShippingKind.FREE else amount
    )
    return FieldFact(FieldStatus.CONFIRMED, value, evidence)


def _stock(nodes: Sequence[Node], words: Vocabulary) -> FieldFact:
    """ADR-0010 §10, read only inside the product's own action area."""
    area = [node for node in nodes if node.within(ACTION_AREA) or node.marks_exactly(ACTION_AREA)]
    controls = [
        node
        for node in area
        if not node.hidden
        and not node.disabled
        and any(node.marks_exactly(name) for name in words.purchase_controls)
    ]
    sold_out = [
        node
        for node in area
        if not node.hidden
        and len(node.visible_text) <= MARK_LIMIT
        and any(word in node.visible_text for word in words.sold_out)
    ]
    if controls:
        return FieldFact(
            FieldStatus.CONFIRMED,
            StockValue(availability=Availability.ON_SALE),
            (
                Evidence(
                    EvidenceKind.CONTROL_STATE,
                    f"*.{ACTION_AREA} purchase control",
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
                    f"*.{ACTION_AREA} sold-out mark",
                    FieldStatus.CONFIRMED,
                    sold_out[0].visible_text,
                ),
            ),
        )
    # Neither: the page does not say plainly enough to record a state.
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        (
            Evidence(
                EvidenceKind.CONTROL_STATE,
                f"*.{ACTION_AREA} purchase control",
                FieldStatus.REVIEW_REQUIRED,
            ),
        ),
    )


def _options(nodes: Sequence[Node], words: Vocabulary) -> FieldFact:
    """The choices the page offers, or why they cannot be recorded as a value.

    A Cafe24 skin writes the option container on every product. An axis to choose is a ``select``
    inside it; a product without one writes none, and that stated absence is ``ABSENT`` — the
    Product DB's proof of "no options" (ADR-0013 ruling B), and KM통상's reading. Every container
    the page writes is looked at, matched exactly, and any axis in any of them is held for review:
    no accepted evidence says how a Cafe24 skin names an axis.
    """
    marker = words.option_container
    containers = [node for node in nodes if node.marks_exactly(marker)]
    locator = f"*.{marker.lstrip('#.')} select"
    axes = [
        node
        for container in containers
        for node in container.descendants()
        if node.tag == "select" and any(child.tag == "option" for child in node.descendants())
    ]
    if not containers:
        # No option container at all: a skin writes one on every product, so its absence means an
        # incomplete or foreign page, never proof of "no options". KM통상 reads it ``ABSENT``.
        return _review(locator)
    if not axes:
        return _absent(locator)
    return _review(
        locator,
        *(
            " / ".join(
                choice.text
                for choice in axis.descendants()
                if choice.tag == "option" and choice.text
            )
            for axis in axes
        ),
    )


def _quantity_tiers(nodes: Sequence[Node]) -> FieldFact:
    """A quantity tier is a source total, and this parser never produces one it cannot read. A
    page that states one of the tier wordings is held for review with its words kept."""
    stated = [
        node
        for node in nodes
        if node.tag in ("th", "td", "h3", "h4", "strong", "caption")
        and any(label in node.text for label in TIER_LABELS)
    ]
    locator = _row_locator(TIER_LABELS[0])
    if not stated:
        return _absent(locator)
    return _review(locator, *(node.text for node in stated))


def _detail_description(nodes: Sequence[Node], words: Vocabulary) -> FieldFact:
    detail = words.detail_container
    locator = f"#{detail.lstrip('#.')}"
    blocks = [node for node in nodes if node.marks_exactly(detail)]
    if not blocks:
        return _absent(locator)
    if len(blocks) > 1 or not blocks[0].closed:
        # Two blocks, or one the page never closed: the page does not say where the description
        # ends, and what follows it (boards, member notes) is never the product's description.
        return _review(locator)
    text = blocks[0].text_outside(words.detail_menu)
    if not text:
        # A description made only of images states no text of its own.
        return _review(locator)
    return _text_fact(locator, text, EvidenceKind.PRODUCT_HTML_FRAGMENT)


def _notice(nodes: Sequence[Node]) -> FieldFact:
    """The template reads no notice into a value. A page that shows one — its heading, or the
    labels a notice table begins with — is held for review; only a page that shows none states no
    notice. It is never reported absent without looking."""
    locator = _row_locator(NOTICE_WORDS[0])
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
    nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]], words: Vocabulary
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
    detail_locator = f"#{words.detail_container.lstrip('#.')}"
    headed = [node for node in nodes if node.tag == "th" and node.text in words.sales_channel_row]
    stated = _labelled(rows, words.sales_channel_row)
    if len(headed) > len(stated):
        # A channel row whose value is an image or nothing: a statement no rule can read.
        return _review(row_locator, *(v for _, v, _n in stated))
    blocks = [node for node in nodes if node.marks_exactly(words.detail_container)]
    if len(blocks) > 1 or (blocks and not blocks[0].closed):
        return _review(detail_locator)
    description = blocks[0].text_outside(words.detail_menu) if blocks else ""

    def contains(text: str, phrases: Sequence[str]) -> list[str]:
        return [phrase for phrase in phrases if _squash(phrase) in _squash(text)]

    def restrictions(text: str) -> dict[str, list[str]]:
        return {
            "closed": contains(text, words.channel_closed_only),
            "coupang": contains(text, words.channel_forbid_coupang),
            "smartstore": contains(text, words.channel_forbid_smartstore),
        }

    allowed_rows: list[str] = []
    found: dict[str, list[str]] = {"closed": [], "coupang": [], "smartstore": []}
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
        for key, hits in row_restrictions.items():
            found[key].extend(hits)
        quoted.append((row_locator, row_value))
    # ADR-0035 §1 (NR-01): the product's name is read for the restricting phrases only, from the
    # sources ``original_name`` reads. An allowed reading never comes from a name.
    names = [("meta[og:title]", _declared_name(nodes))] + [
        (_row_locator(label), value) for label, value, _n in _labelled(rows, words.name)
    ]
    sources = [*((locator, text) for locator, text in names if text), (detail_locator, description)]
    for locator, text in sources:
        for key, hits in restrictions(text).items():
            found[key].extend(hits)
            quoted.extend((locator, hit) for hit in hits)
    restricted = any(found.values())
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
    if found["closed"]:
        value = SalesChannelsValue(scope=SalesChannelScope.CLOSED_MALL_ONLY, policy_text=policy)
    elif restricted:
        forbidden = tuple(sorted(key for key in ("coupang", "smartstore") if found[key]))
        value = SalesChannelsValue(
            scope=SalesChannelScope.LISTED, forbidden=forbidden, policy_text=policy
        )
    else:
        value = SalesChannelsValue(scope=SalesChannelScope.ALL_ALLOWED, policy_text=policy)
    return FieldFact(FieldStatus.CONFIRMED, value, evidence)


def _text_row(rows: Sequence[tuple[str, str, Node]], labels: Sequence[str]) -> FieldFact:
    stated = _labelled(rows, labels)
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
    """Every M3 source-truth field this document states, and the evidence for each."""
    nodes = read(document.body)
    rows = tuple(_rows(nodes))
    return {
        "original_name": _name(nodes, rows, words),
        "prices": _prices(nodes, rows, words),
        "options": _options(nodes, words),
        "stock": _stock(nodes, words),
        "shipping": _shipping(nodes, rows, words),
        "minimum_sale_price": _minimum_sale_price(rows, words),
        "quantity_tiers": _quantity_tiers(nodes),
        "brand": _text_row(rows, words.brand),
        "manufacturer": _text_row(rows, words.manufacturer),
        "origin": _text_row(rows, words.origin),
        "notice": _notice(nodes),
        "detail_description": _detail_description(nodes, words),
        "sales_channels": _sales_channels(nodes, rows, words),
    }
