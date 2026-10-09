"""What a Cafe24 product page states about itself (ADR-0010 §7–§8, ADR-0030 §2, §4).

Derived from KM통상's parser (ruling 5702780630 P1, ``kmretail-3``), with the words and regions a
site may vary held in a :class:`Vocabulary`. Its defaults are KM통상's. A site configuration may
extend a slot with plain words, or move a region (ADR-0030 §3).

The template is stricter than KM통상's parser wherever that parser reads a first value, a guess or
nothing at all. These are its stated differences (ADR-0030 §10), and each is fail closed:

- Several declarations of one fact that disagree are held for review, never the first one read.
- A money cell is read only when it states exactly one amount. Anything else in it — a condition,
  a range, a second amount or no amount — holds the field for review. A fee cell that says exactly
  무료 states free shipping.
- Stock is decided only by controls and marks inside the product's own action area. A hidden or
  disabled control is no offer. An active control decides ``ON_SALE`` (ADR-0010 §10), and a
  sold-out mark decides ``SOLD_OUT`` only when no active control exists.
- The description is read only from a description block the page closed. Its whole text is the
  value, and page text that carries URL material is held for review rather than stored.
- A notice the page shows is held for review. It is never reported absent without looking.
- Options are ``ABSENT`` only when the page writes its option container with no axis in it; a page
  without the container is held for review.

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
from integrations.suppliers.platforms.cafe24.collect.dom import Node, meta, read

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
    # ADR-0031 §3: the sales-channel row label and the phrases that read it. Empty by default: a
    # row the site has not worded stays under review, never allowed.
    sales_channel_row: tuple[str, ...] = ()
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


DEFAULT_VOCABULARY = Vocabulary()

_EVIDENCE_LIMIT = 200
# One amount as a page groups it: plain digits, or thousands separated by commas.
_NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)"
_PRICE_CELL = re.compile(_NUMBER + r"원?")
_WON = re.compile(r"(?<![\d,])" + _NUMBER + r"원")
_FEE_CELL = re.compile(_NUMBER + r"원")
_MINIMUM_CELL = re.compile(_NUMBER + r"원(?:이상)?")
# ADR-0032 §4 (the owner's rule, Issue #219 6086421199): a fee stated as a range, ``A원 ~ B원``,
# is read as its highest amount.
_FEE_RANGE = re.compile(_NUMBER + r"원?[~∼〜]" + _NUMBER + r"원")
_FREE_WORDS = ("무료", "무료배송")
# URL material: a scheme or a protocol-relative reference. It is never quoted and never stored.
_URL_MATERIAL = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*:)?//\S*")
_DIGIT = re.compile(r"\d")


def _squash(text: str) -> str:
    return "".join(text.split())


def _amount(match: re.Match[str]) -> int:
    return int(match.group(1).replace(",", ""))


def _quote(text: str) -> str:
    """Evidence words: URL material removed, then bounded, so a quote never carries a URL."""
    return _URL_MATERIAL.sub("[URL]", text)[:_EVIDENCE_LIMIT]


def _carries_url(text: str) -> bool:
    return _URL_MATERIAL.search(text) is not None


def _rows(nodes: Sequence[Node]) -> Iterator[tuple[str, str, Node]]:
    """Every (label, value) pair of the information tables, in source order.

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
            value = node.text
            if value:
                yield pending[0], value, node
            pending = None


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
        return _review(locator, text)
    return FieldFact(
        FieldStatus.CONFIRMED,
        TextValue(text=text),
        (Evidence(kind, locator, FieldStatus.CONFIRMED, observed=_quote(text)),),
    )


# ---------------------------------------------------------------- fields


def _name(
    nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    declared = meta(nodes).get("og:title", "").strip()
    stated = _labelled(rows, words.name)
    locator = _row_locator(words.name[0])
    if not declared and not stated:
        return _absent("meta[og:title]")
    if (
        declared
        and len(stated) == 1
        and declared != stated[0][1]
        and declared.startswith(stated[0][1] + " ")
    ):
        # A skin that appends the shop's name to the declared title (U-PICK: "<name> - U-PICK
        # B2B") states the product's own name in its 상품명 row; the suffix is not the name.
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
    """The one amount a price cell states: a bare number, or exactly one amount of won among other
    words (a discount rate, a tax note). None when it states none or several."""
    squashed = _squash(text)
    bare = _PRICE_CELL.fullmatch(squashed)
    if bare is not None:
        return _amount(bare)
    amounts = list(_WON.finditer(squashed))
    return _amount(amounts[0]) if len(amounts) == 1 else None


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
    amount = _MINIMUM_CELL.fullmatch(squashed)
    if amount is None:
        return _review(locator, value)
    return FieldFact(
        FieldStatus.CONFIRMED,
        MoneyValue(label=label, amount_krw=_amount(amount)),
        (
            Evidence(
                EvidenceKind.DOM_TEXT,
                locator,
                FieldStatus.CONFIRMED,
                observed=_quote(value),
                normalized=str(_amount(amount)),
            ),
        ),
    )


def _shipping(rows: Sequence[tuple[str, str, Node]], words: Vocabulary) -> FieldFact:
    method = _labelled(rows, words.shipping_method)
    fee = _labelled(rows, words.shipping_fee)
    fee_locator = _row_locator(words.shipping_fee[0])
    if not method and not fee:
        return _absent(fee_locator)
    if not fee:
        # A method without a fee states no amount; a guessed one would be an invention.
        return _review(fee_locator, *(v for _, v, _n in method))
    if any(_DIGIT.search(v) for _, v, _n in method):
        # A method row that names an amount states a condition beside the fee.
        return _review(_row_locator(words.shipping_method[0]), *(v for _, v, _n in method))
    disagreeing = _disagreement(fee_locator, [v for _, v, _n in fee])
    if disagreeing is not None:
        return disagreeing
    cell = _squash(fee[0][1])
    ranged = _FEE_RANGE.match(cell)
    if cell in _FREE_WORDS:
        amount = 0
    elif ranged is not None:
        # ADR-0032 §4: the highest amount of the range the cell opens with; its words are kept.
        amount = max(int(group.replace(",", "")) for group in ranged.groups())
    else:
        stated = _FEE_CELL.fullmatch(cell)
        if stated is None:
            # Not exactly one amount: a threshold, a range or a free-over amount is a condition,
            # never flattened into an amount it names (ADR-0010 §7).
            return _review(fee_locator, fee[0][1])
        amount = _amount(stated)
    policy = " / ".join(f"{label} {value}" for label, value, _ in (*method, *fee))
    if _carries_url(policy):
        return _review(fee_locator, policy)
    kind = ShippingKind.FREE if amount == 0 else ShippingKind.FIXED
    evidence = tuple(
        Evidence(
            EvidenceKind.DOM_TEXT,
            _row_locator(label),
            FieldStatus.CONFIRMED,
            observed=_quote(value),
        )
        for label, value, _cell in (*method, *fee)
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


def _sales_channels(
    nodes: Sequence[Node], rows: Sequence[tuple[str, str, Node]], words: Vocabulary
) -> FieldFact:
    """ADR-0031 §3: what the page says about where the product may be resold.

    Only the site's own phrases are read, in its sales-channel row and in the closed description
    block's text; nothing is read from an image. A row no phrase reads, or phrases that contradict
    each other, stay under review: an unread restriction is never an allowed one.
    """
    stated = _labelled(rows, words.sales_channel_row) if words.sales_channel_row else []
    row_locator = _row_locator((words.sales_channel_row or ("판매가능플랫폼",))[0])
    blocks = [node for node in nodes if node.marks_exactly(words.detail_container)]
    detail_locator = f"#{words.detail_container.lstrip('#.')}"
    sources: list[tuple[str, str]] = [(row_locator, value) for _label, value, _n in stated]
    if len(blocks) == 1 and blocks[0].closed:
        sources.append((detail_locator, blocks[0].text_outside(words.detail_menu)))
    elif blocks:
        # A description the page never closed proves nothing about what it says.
        return _review(detail_locator)

    def found(phrases: Sequence[str]) -> list[tuple[str, str]]:
        return [
            (locator, phrase)
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
    row_read = any(locator == row_locator for locator, _phrase in matched)
    if not matched:
        return _review(row_locator, *(v for _, v, _n in stated)) if stated else _absent(row_locator)
    if (allowed and restricting) or (stated and not row_read):
        # Phrases that contradict each other, or a row no phrase reads: never allowed.
        return _review(row_locator, *(phrase for _locator, phrase in matched))
    evidence = tuple(
        _evidence(locator, FieldStatus.CONFIRMED, phrase) for locator, phrase in matched
    )
    policy = _quote(" / ".join(dict.fromkeys(phrase for _locator, phrase in matched)))
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
        "shipping": _shipping(rows, words),
        "minimum_sale_price": _minimum_sale_price(rows, words),
        "quantity_tiers": _quantity_tiers(nodes),
        "brand": _text_row(rows, words.brand),
        "manufacturer": _text_row(rows, words.manufacturer),
        "origin": _text_row(rows, words.origin),
        "notice": _notice(nodes),
        "detail_description": _detail_description(nodes, words),
        "sales_channels": _sales_channels(nodes, rows, words),
    }
