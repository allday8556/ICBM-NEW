"""What a Super Delivery product page states about itself (ADR-0040 §2, ADR-0038 §1 and §9).

This turns one captured product page into supplier-neutral facts and the evidence for each. It
reads; it never fetches, stores, hashes, persists or retries. Every value is quoted from the page in
its own currency, JPY, and nothing is converted, computed or completed (ADR-0010 §7, FX-01): a field
the page does not state is ``ABSENT``, and a field whose words are present but do not say what the
field needs is ``REVIEW_REQUIRED``.

**Where the page states each fact.** The capture holds the product block (``#product-detail-wrap``)
and the company's terms and shipping blocks (ADR-0040 §2):

===================  ===========================================================================
field                read from
===================  ===========================================================================
original_name        ``h1.product-name``, as stated (a leading ``※`` marks a reduced-tax item)
prices               the set table (``table.set-list``): its price column header names the tax
                     basis, each set's row its prices — see *Prices* below
options              the set table's rows, one configuration per set (S1, S2 …) — see *Options*
stock                the set table's 在庫状況 cells, or, where the company keeps no stock column,
                     the purchase control the reader can see
shipping             the company's 送料 table and words (``#dealer-carriage``) — see *Shipping*
brand                the ブランド名 entry of the product block
manufacturer         a 販売元 / 発売元 / 製造元 line of the detail information
origin               a 生産地 / 原産国 / 原産地 line of the detail information
detail_description   the 商品説明 block (``#product_comment``), its first words
sales_channels       ``ABSENT``: the company's terms name no marketplace (ADR-0040 §2, ADR-0031)
minimum_sale_price,  ``ABSENT``: the site states neither
notice,
quantity_tiers
===================  ===========================================================================

**Prices** (ADR-0040 §2, OC-07; ADR-0038 §9 answer 3). The purchase price is the 税込 price of one
set — what an order of one set pays now. The set table states its amounts under one price column
whose header names the tax basis, ``価格（税抜）`` or ``価格（税込）``.

- A page that shows no wholesale price — the company has not approved the member, and the page
  offers 「卸価格を見る（申請）」 instead — is ``REVIEW_REQUIRED`` with the reason
  ``PRICE_NOT_DISCLOSED``. Nothing is guessed.
- A page whose prices are stated 税抜 states no 税込 price. Adding the consumption tax would be a
  computation (and its rate, 8 % or 10 %, a judgement), so ``prices`` is ``REVIEW_REQUIRED`` with
  the reason ``TAX_INCLUDED_PRICE_NOT_STATED``; every price the page states is kept in the
  evidence, as stated. Every capture of 2026-10-14 is of this kind.
- A page whose prices are stated 税込 is ``CONFIRMED``: the first set's price is the ``PURCHASE``
  price, labelled with the set's own words (``1セット（1点）``); the wholesale unit price keeps its
  stated ratio in its label (``卸単価（66%）``) and no role; the maker's suggested retail price is
  a ``LIST`` price. A price struck through beside a campaign price is the evidence's, not the
  value's: the page names no label for it.

**Options** (ADR-0040 §2, ADR-0013 ruling B). A page with one set states no option: ``ABSENT``. A
page with several sets states one axis, the table's 内訳 column, whose values are each set's 内訳
words, and one configuration per set whose supplier SKU is its SD item number (``12064384S2``). A
configuration carries its set's price only when the price is a ``CONFIRMED`` 税込 one.

**Stock.** 「在庫有り」 is on sale. A company that keeps no stock count writes no 在庫状況 column;
its page is on sale while the reader is shown the purchase control 「カートに入れる」, and is
held for review when it shows none. Words this parser does not know are held for review.

**Shipping** (ADR-0040 §2). A company's 送料 table states, per region, the fee under and over
one order threshold (``20,000円（未満）`` / ``20,000円（以上）``). When every region that ships
free over the threshold states one fee under it — or the company states that fee as
「送料全国一律」 — that fee and threshold are a ``CONDITIONAL`` policy in JPY. The regions that
deviate (北海道, 沖縄・離島) are kept as words in its policy text (ADR-0035 RS-01). Anything else
is ``REVIEW_REQUIRED``.

**Company terms** (ADR-0040 §2, OC-08; ADR-0038 §9 answer 2). The company's 販売規制 table
(消費者への直送, 仕入れ前の販売, 画像転載, ネット販売, 代金引換 with ○ / × / △) and its free-text
取引条件 are kept as its quoted words, in the evidence of ``sales_channels``. They name no
marketplace, so they are no sales-channel restriction (ADR-0031), and they are no gate: the field is
``ABSENT``, and its evidence shows the words with the product.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from app.stages.collect.facts import (
    Availability,
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    ForeignPrice,
    ForeignPricesValue,
    ForeignShippingValue,
    OptionAxis,
    OptionConfiguration,
    OptionsValue,
    PriceRole,
    ShippingKind,
    SourceCurrency,
    StockValue,
    TextValue,
)
from integrations.suppliers.collection import DocumentView
from integrations.suppliers.superdelivery.collect.dom import Node, find, first, read

# ADR-0038 §1, ADR-0040 §5: every amount this site states is in Japanese yen.
CURRENCY: SourceCurrency = "JPY"
# ADR-0040 §2: the reasons a price field states no purchase price.
PRICE_NOT_DISCLOSED = "PRICE_NOT_DISCLOSED"
TAX_INCLUDED_PRICE_NOT_STATED = "TAX_INCLUDED_PRICE_NOT_STATED"
PRICE_BASIS_NOT_STATED = "PRICE_BASIS_NOT_STATED"
PRICE_NOT_STATED = "PRICE_NOT_STATED"

SET_TABLE = ".set-list"
SET_CODE = re.compile(r"^S(\d+)$")
SET_NUMBER = re.compile(r"SD品番[：:]\s*(\d+)S(\d+)")
YEN = re.compile(r"[¥￥]\s*(\d{1,3}(?:,\d{3})+|\d+)")
JAN = re.compile(r"JAN[：:]\s*(\d+)")
PRICE_HEADER = "価格（"
TAX_EXCLUDED = "税抜"
TAX_INCLUDED = "税込"
DETAIL_HEADER = "内訳"
# The control a company that has not approved the member shows instead of a price.
APPLY_CONTROL = ".pdtl-set-apply"
PURCHASE_CONTROLS = (".addCartItem_dummy", ".add-cart-item")
IN_STOCK_WORDS = frozenset({"在庫有り", "在庫あり"})
SOLD_OUT_WORDS = frozenset({"在庫なし", "在庫無し", "品切れ", "売り切れ", "売切れ"})
BRAND_LABEL = "ブランド名"
MANUFACTURER = re.compile(r"(販売元|発売元|製造元)[：:]\s*([^【】]+?)\s*(?:】|$)")
ORIGIN = re.compile(r"^■?\s*(生産地|原産国|原産地)[：:]\s*(.+)$")
DETAIL_BLOCK = "#product_comment"
DETAIL_INFORMATION = ".product-detail-information"
SHIPPING_BLOCK = "#dealer-carriage"
SHIPPING_TABLE = ".carriage-shipping"
UNDER = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+)円（未満）$")
OVER = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+)円（以上）$")
FEE = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+)円$")
FREE_WORD = "無料"
NATIONWIDE = re.compile(r"送料全国一律\s*(\d{1,3}(?:,\d{3})+|\d+)円")
TRADE_TABLE = ".dl-trade-tbl"
TERMS_BLOCK = "#dealing_consideration_box"
_EVIDENCE_LIMIT = 200
# ADR-0010 §8 bounds a stored observed value at 4096 bytes; the terms are quoted up to this many.
_TERMS_BYTES = 4000
_POLICY_LIMIT = 1000


def _quote(text: str, limit: int = _EVIDENCE_LIMIT) -> str:
    return text[:limit]


def _bytes_quote(text: str, limit: int = _TERMS_BYTES) -> str:
    """``text`` cut to at most ``limit`` UTF-8 bytes, at a character boundary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _amount(text: str) -> str | None:
    match = YEN.search(text)
    return None if match is None else match.group(1).replace(",", "")


def _evidence(
    locator: str,
    status: FieldStatus,
    observed: str | None = None,
    normalized: str | None = None,
    kind: EvidenceKind = EvidenceKind.DOM_TEXT,
) -> Evidence:
    return Evidence(
        kind, locator, status, None if observed is None else _quote(observed), normalized
    )


def _absent(locator: str, observed: str | None = None) -> FieldFact:
    return FieldFact(FieldStatus.ABSENT, None, (_evidence(locator, FieldStatus.ABSENT, observed),))


def _review(*evidence: Evidence) -> FieldFact:
    return FieldFact(FieldStatus.REVIEW_REQUIRED, None, evidence)


# ---------------------------------------------------------------- the set table


@dataclass(frozen=True)
class Stated:
    """One amount the page states, with the words that name it and where it sits."""

    label: str
    amount: str
    locator: str
    observed: str


@dataclass
class SetRow:
    """One set of the set table, read from its own rows only (never the smartphone duplicates
    nested inside them)."""

    code: str
    detail: str = ""
    jan: str | None = None
    sku: tuple[str, str] | None = None
    stock: str | None = None
    maker_price: Stated | None = None
    maker_words: str | None = None
    unit_price: Stated | None = None
    struck: Stated | None = None
    set_price: Stated | None = None

    @property
    def number(self) -> str:
        return f"S{self.code}"

    @property
    def words(self) -> str:
        """The set as the page states it: its number, its 内訳 and its JAN."""
        jan = f" JAN：{self.jan}" if self.jan else ""
        return f"{self.number} {self.detail}{jan}"


def _rows(table: Node) -> Iterator[Node]:
    for part in table.children:
        if part.tag == "tr":
            yield part
        elif part.tag in ("tbody", "thead"):
            yield from (row for row in part.children if row.tag == "tr")


def _cells(row: Node) -> tuple[Node, ...]:
    return tuple(cell for cell in row.children if cell.tag in ("td", "th"))


def _price_cell(cell: Node, label: str, locator: str) -> tuple[Stated | None, Stated | None]:
    """The amount a price cell charges now, and an amount struck through beside it (a campaign
    price is written as ``.cmp-price`` beside the struck ``.list-price``)."""
    current = next(cell.find(".cmp-price"), None)
    struck = next(cell.find(".list-price"), None)
    words = cell.text
    now = _amount(current.text) if current is not None else _amount(words)
    before = _amount(struck.text) if struck is not None else None
    where = f"{locator} .cmp-price" if current is not None else locator
    return (
        None if now is None else Stated(label, now, where, words),
        None if before is None else Stated(label, before, f"{locator} .list-price", words),
    )


def _set_rows(table: Node) -> list[SetRow]:
    sets: list[SetRow] = []
    for row in _rows(table):
        cells = _cells(row)
        if not cells:
            continue
        start = SET_CODE.fullmatch(cells[0].text)
        if start is not None and cells[0].tag == "td":
            sets.append(SetRow(code=start.group(1)))
        if not sets:
            continue
        current = sets[-1]
        locator = f"table.set-list {current.number}"
        for index, cell in enumerate(cells):
            following = cells[index + 1] if index + 1 < len(cells) else None
            if cell.marks(".td-set-detail"):
                holder = first(tuple(cell.descendants()), tag="div")
                current.detail = (holder or cell).own_text
                jan = JAN.search(cell.text)
                current.jan = None if jan is None else jan.group(1)
            elif cell.marks(".tr-last"):
                sku = SET_NUMBER.search(cell.text)
                current.sku = None if sku is None else (sku.group(1), sku.group(2))
            elif cell.marks(".stock-info"):
                current.stock = cell.text
            elif cell.marks(".maker-product-price"):
                label = cell.find(".float-left")
                named = next(label, None)
                words = named.text if named is not None else cell.text
                amount_cell = (
                    following if following is not None and following.marks(".td-price01") else None
                )
                amount = None if amount_cell is None else _amount(amount_cell.text)
                if amount is not None and amount_cell is not None:
                    current.maker_price = Stated(
                        words, amount, f"{locator} td.td-price01", amount_cell.text
                    )
                else:
                    current.maker_words = cell.text
            elif cell.marks(".maker-wholesale-price") and following is not None:
                now, before = _price_cell(following, cell.text, f"{locator} td.td-price02")
                current.unit_price, current.struck = now, before
            elif cell.marks(".set-num") and following is not None:
                now, _ = _price_cell(
                    following, cell.text, f"{locator} td.maker-wholesale-set-price"
                )
                current.set_price = now
    return sets


def _header(table: Node) -> tuple[str, ...]:
    return tuple(cell.text for row in _rows(table) for cell in _cells(row) if cell.tag == "th")


# ---------------------------------------------------------------- fields


def _name(nodes: Sequence[Node]) -> FieldFact:
    heading = first(nodes, ".product-name", tag="h1")
    text = "" if heading is None else heading.text
    if not text:
        return _absent("h1.product-name")
    return FieldFact(
        FieldStatus.CONFIRMED,
        TextValue(text=text),
        (_evidence("h1.product-name", FieldStatus.CONFIRMED, text),),
    )


def _basis(header: Sequence[str]) -> tuple[str | None, str | None]:
    """The price column's header, and the tax basis it names (税抜 or 税込), if it names one."""
    for words in header:
        if words.startswith(PRICE_HEADER):
            if TAX_INCLUDED in words and TAX_EXCLUDED not in words:
                return words, TAX_INCLUDED
            if TAX_EXCLUDED in words and TAX_INCLUDED not in words:
                return words, TAX_EXCLUDED
            return words, None
    return None, None


def _stated_evidence(stated: Stated, status: FieldStatus) -> Evidence:
    return _evidence(stated.locator, status, stated.observed, f"{CURRENCY} {stated.amount}")


def _prices(nodes: Sequence[Node], sets: Sequence[SetRow], header: Sequence[str]) -> FieldFact:
    if not sets:
        return _absent("table.set-list")
    words, basis = _basis(header)
    first_set = sets[0]
    observed: list[Evidence] = []
    if words is not None:
        observed.append(_evidence("th:価格", FieldStatus.CONFIRMED, words, basis))
    for stated in (
        first_set.set_price,
        first_set.unit_price,
        first_set.struck,
        first_set.maker_price,
    ):
        if stated is not None:
            observed.append(_stated_evidence(stated, FieldStatus.CONFIRMED))
    campaign = first(nodes, ".campaign-info")
    if campaign is not None and campaign.text:
        # The company's price campaign: why a struck price stands beside the price charged now.
        observed.append(_evidence(".campaign-info", FieldStatus.CONFIRMED, campaign.text))
    if first_set.maker_words:
        observed.append(
            _evidence(
                f"table.set-list {first_set.number} td.maker-product-price",
                FieldStatus.CONFIRMED,
                first_set.maker_words,
            )
        )
    if any(row.set_price is None for row in sets):
        apply = first(nodes, APPLY_CONTROL)
        if apply is not None and apply.text:
            return _review(
                *observed,
                _evidence(
                    APPLY_CONTROL,
                    FieldStatus.REVIEW_REQUIRED,
                    apply.text,
                    PRICE_NOT_DISCLOSED,
                    EvidenceKind.CONTROL_STATE,
                ),
            )
        return _review(
            *observed,
            _evidence("table.set-list", FieldStatus.REVIEW_REQUIRED, None, PRICE_NOT_STATED),
        )
    if basis != TAX_INCLUDED:
        reason = TAX_INCLUDED_PRICE_NOT_STATED if basis == TAX_EXCLUDED else PRICE_BASIS_NOT_STATED
        return _review(
            *observed,
            _evidence("th:価格", FieldStatus.REVIEW_REQUIRED, words, reason),
        )
    roled = (
        (first_set.set_price, PriceRole.PURCHASE),
        (first_set.unit_price, None),
        (first_set.maker_price, PriceRole.LIST),
    )
    prices = tuple(_price(stated, role) for stated, role in roled if stated is not None)
    return FieldFact(FieldStatus.CONFIRMED, ForeignPricesValue(prices=prices), tuple(observed))


def _price(stated: Stated, role: PriceRole | None) -> ForeignPrice:
    return ForeignPrice(label=stated.label, currency=CURRENCY, amount=stated.amount, role=role)


def _options(
    sets: Sequence[SetRow], header: Sequence[str], number: str | None, prices: FieldFact
) -> FieldFact:
    if not sets:
        return _absent("table.set-list")
    if len(sets) == 1:
        # ADR-0013 ruling B: one set is no choice; the page states no option. The set's own words
        # (its 内訳 and JAN) are kept as the evidence of that.
        return _absent(f"table.set-list {sets[0].number}", sets[0].words)
    evidence = tuple(
        _evidence(
            f"table.set-list {row.number}",
            FieldStatus.CONFIRMED,
            row.words,
            None if row.sku is None else f"{row.sku[0]}S{row.sku[1]}",
        )
        for row in sets
    )
    details = [row.detail for row in sets]
    coherent = (
        all(details)
        and len(set(details)) == len(details)
        and all(row.sku is not None and row.sku == (number, row.code) for row in sets)
    )
    if not coherent:
        # A set without its own words, two sets the page words alike, or an SD item number that
        # is not this product's and this set's: the choices cannot be recorded as stated.
        return _review(
            *evidence,
            _evidence("table.set-list", FieldStatus.REVIEW_REQUIRED, " / ".join(details)),
        )
    axis = next(
        (words.split("（", 1)[0].strip() for words in header if words.startswith(DETAIL_HEADER)),
        DETAIL_HEADER,
    )
    priced = prices.status is FieldStatus.CONFIRMED
    configurations = tuple(
        OptionConfiguration(
            selections=(row.detail,),
            supplier_sku_id=f"{number}S{row.code}",
            sold_out=_sold_out(row.stock),
            unit_price=row.set_price.amount if priced and row.set_price is not None else None,
            currency=CURRENCY if priced and row.set_price is not None else None,
        )
        for row in sets
    )
    return FieldFact(
        FieldStatus.CONFIRMED,
        OptionsValue(
            axes=(OptionAxis(name=axis, values=tuple(details)),), configurations=configurations
        ),
        evidence,
    )


def _sold_out(words: str | None) -> bool | None:
    if words in IN_STOCK_WORDS:
        return False
    if words in SOLD_OUT_WORDS:
        return True
    return None


def _stock(nodes: Sequence[Node], sets: Sequence[SetRow]) -> FieldFact:
    stated = [row for row in sets if row.stock is not None]
    if sets and len(stated) == len(sets):
        evidence = tuple(
            _evidence(
                f"table.set-list {row.number} td.stock-info", FieldStatus.CONFIRMED, row.stock
            )
            for row in sets
        )
        states = [_sold_out(row.stock) for row in sets]
        if None in states:
            return _review(
                *(
                    _evidence(e.locator, FieldStatus.REVIEW_REQUIRED, e.observed)
                    if state is None
                    else e
                    for e, state in zip(evidence, states, strict=True)
                )
            )
        availability = Availability.SOLD_OUT if all(states) else Availability.ON_SALE
        return FieldFact(FieldStatus.CONFIRMED, StockValue(availability=availability), evidence)
    if stated:
        # Some sets state their stock and some do not: the page does not say plainly enough.
        return _review(_evidence("table.set-list td.stock-info", FieldStatus.REVIEW_REQUIRED, None))
    controls = [
        (token, node)
        for node in nodes
        for token in PURCHASE_CONTROLS
        if node.marks(token) and node.visible_text
    ]
    if controls:
        # The company keeps no stock count (no 在庫状況 column): the page offers the product.
        token, control = controls[0]
        return FieldFact(
            FieldStatus.CONFIRMED,
            StockValue(availability=Availability.ON_SALE),
            (
                _evidence(
                    token,
                    FieldStatus.CONFIRMED,
                    control.visible_text,
                    kind=EvidenceKind.CONTROL_STATE,
                ),
            ),
        )
    return _review(
        _evidence("table.set-list td.stock-info", FieldStatus.REVIEW_REQUIRED, None),
    )


def _yen_cell(words: str) -> int | None:
    if words == FREE_WORD:
        return 0
    match = FEE.fullmatch(words.replace(" ", ""))
    return None if match is None else int(match.group(1).replace(",", ""))


def _shipping(nodes: Sequence[Node]) -> FieldFact:
    block = first(nodes, SHIPPING_BLOCK)
    if block is None:
        return _review(_evidence(SHIPPING_BLOCK, FieldStatus.REVIEW_REQUIRED, None))
    table = first(tuple(block.descendants()), SHIPPING_TABLE, tag="table")
    lines = block.lines("table")
    nationwide = next((m for line in lines if (m := NATIONWIDE.search(line))), None)
    statement = next((line for line in lines if NATIONWIDE.search(line)), None)
    if table is None:
        return _review(
            _evidence(f"{SHIPPING_BLOCK} table.carriage-shipping", FieldStatus.REVIEW_REQUIRED)
        )
    rows = [_cells(row) for row in _rows(table)]
    tiers = next(
        (
            [cell.text for cell in cells]
            for cells in rows
            if len(cells) == 2
            and all(cell.tag == "th" for cell in cells)
            and UNDER.fullmatch(cells[0].text)
        ),
        None,
    )
    header = next(
        (cell.text for cells in rows for cell in cells if cell.text.startswith("ご購入金額")),
        "",
    )
    regions = [
        (cells[0].text, cells[1].text, cells[2].text)
        for cells in rows
        if len(cells) == 3 and cells[0].tag == "th" and cells[1].tag == "td"
    ]
    summary = " / ".join(f"{region} {under}・{over}" for region, under, over in regions)
    evidence = [
        _evidence(
            f"{SHIPPING_BLOCK} table.carriage-shipping",
            FieldStatus.CONFIRMED,
            _quote(f"{header} {' ・ '.join(tiers or [])} / {summary}"),
        )
    ]
    if statement is not None:
        evidence.append(
            _evidence(f"{SHIPPING_BLOCK} .dl-info-mdl-txt", FieldStatus.CONFIRMED, statement)
        )
    under_match = None if tiers is None else UNDER.fullmatch(tiers[0])
    over_match = None if tiers is None else OVER.fullmatch(tiers[1])
    fees: list[tuple[int, int]] = []
    for _, under, over in regions:
        fee_under, fee_over = _yen_cell(under), _yen_cell(over)
        if fee_under is not None and fee_over is not None:
            fees.append((fee_under, fee_over))
    if (
        tiers is None
        or under_match is None
        or over_match is None
        or under_match.group(1) != over_match.group(1)
        or not regions
        or len(fees) != len(regions)
    ):
        # A threshold the two columns do not state alike, or a fee cell that is no amount (別途):
        # the policy is the page's words, and no reading of them is made.
        return _review(*evidence, _guard(evidence[0]))
    threshold = int(under_match.group(1).replace(",", ""))
    free_over = {fee_under for fee_under, fee_over in fees if fee_over == 0}
    stated = None if nationwide is None else int(nationwide.group(1).replace(",", ""))
    if len(free_over) == 1:
        (flat,) = free_over
    elif stated is not None and stated in free_over:
        flat = stated
    else:
        return _review(*evidence, _guard(evidence[0]))
    if flat <= 0:
        return _review(*evidence, _guard(evidence[0]))
    deviating = [
        f"{region} {under}・{over}"
        for (region, under, over), fee in zip(regions, fees, strict=True)
        if fee != (flat, 0)
    ]
    parts = [part for part in (statement,) if part]
    parts.append(f"{header} {tiers[0]} {flat}円・{tiers[1]} {FREE_WORD}")
    parts.extend(deviating)
    value = ForeignShippingValue(
        kind=ShippingKind.CONDITIONAL,
        policy_text=_quote(" / ".join(parts), _POLICY_LIMIT),
        currency=CURRENCY,
        fee=str(flat),
        free_over=str(threshold),
    )
    return FieldFact(FieldStatus.CONFIRMED, value, tuple(evidence))


def _guard(evidence: Evidence) -> Evidence:
    return Evidence(evidence.kind, evidence.locator, FieldStatus.REVIEW_REQUIRED, evidence.observed)


def _brand(nodes: Sequence[Node]) -> FieldFact:
    """The ブランド名 entry: the ``dd`` written right after its ``dt`` in the same ``dl``."""
    locator = ".brand-genre dt:ブランド名 + dd"
    for entry in find(nodes, ".brand-genre"):
        for holder in entry.find(tag="dl"):
            children = holder.children
            for index in range(len(children) - 1):
                term, definition = children[index], children[index + 1]
                if term.tag != "dt" or definition.tag != "dd":
                    continue
                if not term.text.startswith(BRAND_LABEL) or not definition.text:
                    continue
                words = definition.text
                return FieldFact(
                    FieldStatus.CONFIRMED,
                    TextValue(text=words),
                    (_evidence(locator, FieldStatus.CONFIRMED, words),),
                )
    return _absent(locator)


def _labelled_line(nodes: Sequence[Node], pattern: re.Pattern[str], locator: str) -> FieldFact:
    block = first(nodes, DETAIL_INFORMATION)
    if block is None:
        return _absent(locator)
    found: list[tuple[str, str]] = []
    for line in block.lines():
        for match in pattern.finditer(line):
            pair = (match.group(1), match.group(2).strip())
            if pair[1] and pair not in found:
                found.append(pair)
    if not found:
        return _absent(locator)
    evidence = tuple(
        _evidence(f"{locator}:{label}", FieldStatus.CONFIRMED, f"{label}：{words}")
        for label, words in found
    )
    if len({words for _, words in found}) > 1:
        # Two different statements: which one names the product's maker is not the page's to say.
        return _review(*(_guard(e) for e in evidence))
    return FieldFact(FieldStatus.CONFIRMED, TextValue(text=found[0][1]), evidence)


def _detail_description(nodes: Sequence[Node]) -> FieldFact:
    block = first(nodes, DETAIL_BLOCK)
    if block is None:
        return _absent(DETAIL_BLOCK)
    # The company's own link to its product list is navigation, not description.
    text = block.text_outside("a", "label")
    if not text:
        return _review(
            _evidence(
                DETAIL_BLOCK, FieldStatus.REVIEW_REQUIRED, kind=EvidenceKind.PRODUCT_HTML_FRAGMENT
            )
        )
    return FieldFact(
        FieldStatus.CONFIRMED,
        TextValue(text=_quote(text)),
        (
            _evidence(
                DETAIL_BLOCK,
                FieldStatus.CONFIRMED,
                text,
                kind=EvidenceKind.PRODUCT_HTML_FRAGMENT,
            ),
        ),
    )


def _sales_channels(nodes: Sequence[Node]) -> FieldFact:
    """ADR-0040 §2, OC-08: the company's terms, kept as its words and never a gate."""
    evidence: list[Evidence] = []
    table = first(nodes, TRADE_TABLE, tag="table")
    if table is not None:
        cells = [" ".join(cell.text for cell in _cells(row)) for row in _rows(table) if _cells(row)]
        evidence.append(
            Evidence(
                EvidenceKind.DOM_TEXT,
                "table.dl-trade-tbl",
                FieldStatus.ABSENT,
                _bytes_quote(" / ".join(cells)),
            )
        )
    terms = first(nodes, TERMS_BLOCK)
    if terms is not None and terms.lines():
        evidence.append(
            Evidence(
                EvidenceKind.DOM_TEXT,
                TERMS_BLOCK,
                FieldStatus.ABSENT,
                _bytes_quote("\n".join(terms.lines())),
            )
        )
    if not evidence:
        return _absent("table.dl-trade-tbl")
    return FieldFact(FieldStatus.ABSENT, None, tuple(evidence))


def parse_fields(document: DocumentView) -> dict[str, FieldFact]:
    """Every source-truth field this captured page states, and the evidence for each."""
    nodes = read(document.body)
    table = first(nodes, SET_TABLE, tag="table")
    sets = [] if table is None else _set_rows(table)
    header = () if table is None else _header(table)
    numbers = {row.sku[0] for row in sets if row.sku is not None}
    number = next(iter(numbers)) if len(numbers) == 1 else None
    prices = _prices(nodes, sets, header)
    return {
        "original_name": _name(nodes),
        "prices": prices,
        "options": _options(sets, header, number, prices),
        "stock": _stock(nodes, sets),
        "shipping": _shipping(nodes),
        "minimum_sale_price": _absent("table.set-list"),
        "quantity_tiers": _absent("table.set-list"),
        "brand": _brand(nodes),
        "manufacturer": _labelled_line(nodes, MANUFACTURER, ".product-detail-information"),
        "origin": _labelled_line(nodes, ORIGIN, ".product-detail-information"),
        "notice": _absent(".product-detail-information"),
        "detail_description": _detail_description(nodes),
        "sales_channels": _sales_channels(nodes),
    }
