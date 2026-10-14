"""What a captured 1688 page states about its product (ADR-0040 §4, ADR-0010 §7–§8).

A 1688 capture is two things: the product-scope DOM the capture policy cuts (the title block, the
gallery and the attribute table) and the embedded-data member, the projection of the page's own
data onto the reviewed allowlist (§3). This module reads both. It never fetches, stores, hashes or
retries, and nothing is inferred, averaged or completed: a fact the page does not state is
``ABSENT``, and one whose evidence is present but does not settle it is ``REVIEW_REQUIRED``.

=====================  =========================================================================
field                  read from
=====================  =========================================================================
original_name          the title block's ``h1`` (Chinese, as stated)
prices                 CNY. ``skuPrice``: the lowest SKU original price (``skuInfoMap.*.price``),
                       labelled, is the ``PURCHASE`` price. ``rangePrice``: the first tier of
                       ``originalPrices``, or of ``currentPrices`` when no original is stated
options                ``skuProps`` (axes and values) and ``skuInfoMap`` (one configuration per
                       SKU): the SKU id, its original price and its sold-out state
stock                  every SKU's ``canBookCount``: any above zero is ON_SALE, all zero SOLD_OUT
quantity_tiers         ``rangePrice``: held for review, every tier quoted in its evidence (a tier
                       states a unit price, never the total a quantity tier holds)
shipping               ``freightInfo.totalCost`` as a ``FIXED`` CNY fee (``FREE`` at zero), from
                       the stated ship-from ``location``; never the member's receive address
brand, origin          the attribute table's 品牌 and 产地
detail_description     not captured: the description is a separate document the page names,
                       and no reviewed transport reads it yet (``REVIEW_REQUIRED``)
manufacturer, notice   not stated on 1688: ``ABSENT``
minimum_sale_price,    not stated on 1688: ``ABSENT``
sales_channels
=====================  =========================================================================

**The price basis** (the owner's rule, Issue #219 ``6164685916``; ADR-0038 §9.3 as amended). The
purchase price is the original price, the non-discounted ``price`` of a SKU. A promotion or member
``discountPrice`` is recorded as evidence and never sets the cost. Where no original is stated, the
stated price is used. A minimum order above one (``beginAmount`` > 1) is ``REVIEW_REQUIRED``:
구매대행 sells one unit.

**Embedded data refused** (OC-04). When the member is absent — the browser or the server refused
it — every field it would have supplied is ``REVIEW_REQUIRED``, with the refusal's code as its
evidence. Nothing is read instead from the DOM's selected option.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

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
    StockValue,
    TextValue,
)
from integrations.suppliers.ali1688.collect.dom import Node, read
from integrations.suppliers.ali1688.collect.embedded import (
    FREIGHT,
    PRICE_MODEL,
    SKU_MODEL,
    UNIT,
    at,
    locator,
    member,
)
from integrations.suppliers.collection import DocumentView

CURRENCY: Final = "CNY"
SKU_PRICE = "skuPrice"
RANGE_PRICE = "rangePrice"
# The title block and the attribute table the capture policy keeps.
TITLE_BLOCK = "productTitle"
ATTRIBUTES = "productAttributes"
BRAND_LABELS = ("品牌",)
ORIGIN_LABELS = ("产地",)
# The words this site writes in an attribute cell to state that there is none.
NONE_WORDS = frozenset({"无", "无品牌"})
# The labels of the prices this parser writes. They name where on the page each price is stated,
# because the embedded data carries amounts without words.
LOWEST_SKU_LABEL = "SKU最低原价"
ORIGINAL_TIER_LABEL = "原价"
CURRENT_TIER_LABEL = "现价"
# A SKU key joins its axis values with this, written HTML-escaped by the page itself.
SKU_KEY_SEPARATOR = "&gt;"

_CNY = re.compile(r"^(0|[1-9][0-9]{0,11})(?:\.([0-9]{1,2}))?$")
_QUOTE = 200

# Why a field that needs the embedded data is under review.
PRICE_DISPLAY_UNKNOWN = "PRICE_DISPLAY_TYPE_UNKNOWN"
PRICE_NOT_STATED = "PRICE_NOT_STATED"
MINIMUM_ORDER_ABOVE_ONE = "MINIMUM_ORDER_ABOVE_ONE"
SKU_KEY_UNREAD = "SKU_KEY_NOT_ITS_AXES"
STOCK_NOT_STATED = "STOCK_NOT_STATED"
FREIGHT_NOT_STATED = "FREIGHT_NOT_STATED"
# A 1688 tier states a unit price from a quantity on; a quantity tier is a stated (quantity, total)
# pair. Nothing is multiplied into a total the page never stated.
TIER_UNIT_PRICE_NOT_TOTAL = "TIER_UNIT_PRICE_NOT_TOTAL"
# The description is a separate document the page names; nothing captures it yet (ADR-0040 C2).
DESCRIPTION_NOT_CAPTURED = "DESCRIPTION_NOT_CAPTURED"


# ---------------------------------------------------------------- small readers


def _cny(text: object) -> str | None:
    """A CNY amount as decimal text at two places (``9.5`` → ``9.50``), or None."""
    if not isinstance(text, str):
        return None
    match = _CNY.fullmatch(text)
    if match is None:
        return None
    return f"{match.group(1)}.{(match.group(2) or '').ljust(2, '0')}"


def _cny_number(value: object) -> str | None:
    """A JSON number stated as a CNY amount (freight), as decimal text, or None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return _cny(str(value) if isinstance(value, int) else repr(value))


def _fen(amount: str) -> int:
    whole, _, fraction = amount.partition(".")
    return int(whole) * 100 + int(fraction.ljust(2, "0"))


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _evidence(path: Sequence[str], status: FieldStatus, observed: str | None = None) -> Evidence:
    return Evidence(
        EvidenceKind.EMBEDDED_JSON,
        locator(*path),
        status,
        observed=None if observed is None else observed[:_QUOTE],
    )


def _review(path: Sequence[str], reason: str) -> FieldFact:
    return FieldFact(
        status=FieldStatus.REVIEW_REQUIRED,
        value=None,
        evidence=(
            Evidence(
                EvidenceKind.EMBEDDED_JSON,
                locator(*path),
                FieldStatus.REVIEW_REQUIRED,
                normalized=reason,
            ),
        ),
    )


def _absent(kind: EvidenceKind, where: str, normalized: str | None = None) -> FieldFact:
    return FieldFact(
        status=FieldStatus.ABSENT,
        value=None,
        evidence=(Evidence(kind, where, FieldStatus.ABSENT, normalized=normalized),),
    )


def _unavailable(reason: str) -> FieldFact:
    """A field the embedded data would have supplied, when the member is absent (OC-04)."""
    return FieldFact(
        status=FieldStatus.REVIEW_REQUIRED,
        value=None,
        evidence=(
            Evidence(
                EvidenceKind.EMBEDDED_JSON,
                "embedded",
                FieldStatus.REVIEW_REQUIRED,
                normalized=reason,
            ),
        ),
    )


# ---------------------------------------------------------------- the DOM


def _name(nodes: Sequence[Node]) -> FieldFact:
    headings = [
        node
        for node in nodes
        if node.tag == "h1" and node.inside(element_id=TITLE_BLOCK) and node.text
    ]
    where = f"div#{TITLE_BLOCK} h1"
    if len(headings) != 1:
        return FieldFact(
            status=FieldStatus.REVIEW_REQUIRED if headings else FieldStatus.ABSENT,
            value=None,
            evidence=(
                Evidence(
                    EvidenceKind.DOM_TEXT,
                    where,
                    FieldStatus.REVIEW_REQUIRED if headings else FieldStatus.ABSENT,
                ),
            ),
        )
    text = headings[0].text
    return FieldFact(
        status=FieldStatus.CONFIRMED,
        value=TextValue(text=text),
        evidence=(
            Evidence(EvidenceKind.DOM_TEXT, where, FieldStatus.CONFIRMED, observed=text[:_QUOTE]),
        ),
    )


def _attributes(nodes: Sequence[Node]) -> list[tuple[str, str, str]]:
    """Every (label, value, locator) the attribute table states, in page order.

    The table writes a pair two ways: a decision row (``cpv-attr-label`` beside ``cpv-attr-value``)
    and a description grid (a ``th`` label cell followed by its ``td`` value cell).
    """
    found: list[tuple[str, str, str]] = []
    for node in nodes:
        if not node.inside(element_id=ATTRIBUTES):
            continue
        if node.has_class("cpv-attr-item"):
            labels = [n.text for n in node.descendants() if n.has_class("cpv-attr-label")]
            values = [n.text for n in node.descendants() if n.has_class("cpv-attr-value")]
            if len(labels) == 1 and len(values) == 1 and labels[0] and values[0]:
                found.append((labels[0], values[0], "div.cpv-attr-item"))
        elif node.tag == "tr":
            cells = [item for item in node.content if isinstance(item, Node)]
            for label, value in zip(cells[::2], cells[1::2], strict=False):
                if label.tag == "th" and value.tag == "td" and label.text and value.text:
                    found.append((label.text, value.text, "th + td"))
    return found


def _attribute(rows: Sequence[tuple[str, str, str]], labels: Sequence[str]) -> FieldFact:
    stated = [(label, value, where) for label, value, where in rows if label in labels]
    where = f"div#{ATTRIBUTES} " + "|".join(labels)
    if not stated:
        return _absent(EvidenceKind.DOM_TEXT, where)
    values = {value for _, value, _ in stated}
    evidence = tuple(
        Evidence(
            EvidenceKind.DOM_TEXT,
            f"div#{ATTRIBUTES} {found}:{label}",
            FieldStatus.CONFIRMED if len(values) == 1 else FieldStatus.REVIEW_REQUIRED,
            observed=value[:_QUOTE],
        )
        for label, value, found in stated
    )
    if len(values) != 1:
        # Two rows of the table state it differently: the page does not settle it.
        return FieldFact(FieldStatus.REVIEW_REQUIRED, None, evidence)
    (value,) = values
    if value in NONE_WORDS:
        # The page states that there is none.
        return FieldFact(
            FieldStatus.ABSENT,
            None,
            tuple(
                Evidence(e.kind, e.locator, FieldStatus.ABSENT, observed=e.observed)
                for e in evidence
            ),
        )
    return FieldFact(FieldStatus.CONFIRMED, TextValue(text=value), evidence)


# ---------------------------------------------------------------- the embedded data


class _Sku:
    """One ``skuInfoMap`` entry as stated."""

    def __init__(self, key: str, entry: Mapping[str, Any]) -> None:
        self.key = key
        self.sku_id = _count(entry.get("skuId"))
        self.price = _cny(entry.get("price"))
        self.discount = _cny(entry.get("discountPrice"))
        self.stock = _count(entry.get("canBookCount"))
        self.promotion = entry.get("isPromotionSku") is True

    @property
    def original(self) -> str | None:
        """The SKU's purchase price: its original price, else the one price it states."""
        return self.price or self.discount


def _skus(data: Mapping[str, Any]) -> list[_Sku]:
    entries = at(data, *SKU_MODEL, "skuInfoMap")
    if not isinstance(entries, Mapping):
        return []
    return [_Sku(key, entry) for key, entry in entries.items() if isinstance(entry, Mapping)]


def _axes(data: Mapping[str, Any]) -> list[tuple[str, list[str]]] | None:
    props = at(data, *SKU_MODEL, "skuProps")
    if props is None:
        return []
    axes: list[tuple[str, list[str]]] = []
    for prop in props if isinstance(props, list) else []:
        name = prop.get("prop") if isinstance(prop, Mapping) else None
        values = prop.get("value") if isinstance(prop, Mapping) else None
        if not isinstance(name, str) or not name or not isinstance(values, list):
            return None
        names = [v.get("name") for v in values if isinstance(v, Mapping)]
        if not names or not all(isinstance(n, str) and n for n in names):
            return None
        axes.append((name, [str(n) for n in names]))
    return axes


def _tiers(data: Mapping[str, Any], name: str) -> list[tuple[int, str]] | None:
    """A price list of the price model as (beginAmount, amount), or None when one is unreadable."""
    listed = at(data, *PRICE_MODEL, name)
    if listed is None:
        return []
    tiers: list[tuple[int, str]] = []
    for entry in listed if isinstance(listed, list) else [None]:
        begin = _count(at(entry, "beginAmount"))
        amount = _cny(at(entry, "price"))
        if begin is None or begin < 1 or amount is None:
            return None
        tiers.append((begin, amount))
    return tiers


def _prices(data: Mapping[str, Any], skus: Sequence[_Sku], unit: str) -> FieldFact:
    display = at(data, *PRICE_MODEL, "priceDisplayType")
    display_path = (*PRICE_MODEL, "priceDisplayType")
    if display == SKU_PRICE:
        stated = [sku.original for sku in skus]
        if not skus or any(amount is None for amount in stated):
            return _review((*SKU_MODEL, "skuInfoMap", "*", "price"), PRICE_NOT_STATED)
        current = _tiers(data, "currentPrices")
        if current is None:
            return _review((*PRICE_MODEL, "currentPrices"), PRICE_NOT_STATED)
        if any(begin > 1 for begin, _ in current):
            return _review((*PRICE_MODEL, "currentPrices", "beginAmount"), MINIMUM_ORDER_ABOVE_ONE)
        lowest = min((amount for amount in stated if amount is not None), key=_fen)
        return FieldFact(
            FieldStatus.CONFIRMED,
            ForeignPricesValue(
                prices=(
                    ForeignPrice(
                        label=LOWEST_SKU_LABEL,
                        currency=CURRENCY,
                        amount=lowest,
                        role=PriceRole.PURCHASE,
                    ),
                )
            ),
            (
                _evidence(display_path, FieldStatus.CONFIRMED, SKU_PRICE),
                _evidence(
                    (*SKU_MODEL, "skuInfoMap", "*", "price"),
                    FieldStatus.CONFIRMED,
                    f"{len(skus)} SKU, lowest {lowest} {CURRENCY}",
                ),
            ),
        )
    if display == RANGE_PRICE:
        original = _tiers(data, "originalPrices")
        current = _tiers(data, "currentPrices")
        if original is None or current is None:
            return _review((*PRICE_MODEL, "currentPrices"), PRICE_NOT_STATED)
        basis, name, label = (
            (original, "originalPrices", ORIGINAL_TIER_LABEL)
            if original
            else (current, "currentPrices", CURRENT_TIER_LABEL)
        )
        if not basis:
            return _review((*PRICE_MODEL, "currentPrices"), PRICE_NOT_STATED)
        begin, amount = basis[0]
        if begin != 1:
            return _review((*PRICE_MODEL, name, "beginAmount"), MINIMUM_ORDER_ABOVE_ONE)
        prices = [
            ForeignPrice(
                label=f"{label} ≥{begin}{unit}",
                currency=CURRENCY,
                amount=amount,
                role=PriceRole.PURCHASE,
            )
        ]
        evidence = [
            _evidence(display_path, FieldStatus.CONFIRMED, RANGE_PRICE),
            _evidence((*PRICE_MODEL, name, "price"), FieldStatus.CONFIRMED, f"≥{begin} {amount}"),
        ]
        if original and current and current[0][0] == 1:
            # A current (promotion) first tier beside the original: kept as a fact, never a cost.
            prices.append(
                ForeignPrice(
                    label=f"{CURRENT_TIER_LABEL} ≥1{unit}",
                    currency=CURRENCY,
                    amount=current[0][1],
                )
            )
            evidence.append(
                _evidence(
                    (*PRICE_MODEL, "currentPrices", "price"),
                    FieldStatus.CONFIRMED,
                    f"≥1 {current[0][1]}",
                )
            )
        return FieldFact(
            FieldStatus.CONFIRMED, ForeignPricesValue(prices=tuple(prices)), tuple(evidence)
        )
    return _review(display_path, PRICE_DISPLAY_UNKNOWN)


def _configurations(
    axes: Sequence[tuple[str, list[str]]], skus: Sequence[_Sku], priced: bool
) -> list[OptionConfiguration] | None:
    configurations: list[tuple[tuple[int, ...], OptionConfiguration]] = []
    for sku in skus:
        selections = tuple(sku.key.split(SKU_KEY_SEPARATOR))
        if len(selections) != len(axes) or any(
            value not in values for value, (_, values) in zip(selections, axes, strict=True)
        ):
            return None
        if priced and sku.original is None:
            return None
        position = tuple(
            values.index(value) for value, (_, values) in zip(selections, axes, strict=True)
        )
        configurations.append(
            (
                position,
                OptionConfiguration(
                    selections=selections,
                    supplier_sku_id=None if sku.sku_id is None else str(sku.sku_id),
                    sold_out=None if sku.stock is None else sku.stock == 0,
                    unit_price=sku.original if priced else None,
                    currency=CURRENCY if priced else None,
                ),
            )
        )
    return [configuration for _, configuration in sorted(configurations, key=lambda c: c[0])]


def _options(data: Mapping[str, Any], skus: Sequence[_Sku]) -> FieldFact:
    axes = _axes(data)
    props_path = (*SKU_MODEL, "skuProps")
    if axes is None:
        return _review(props_path, SKU_KEY_UNREAD)
    if not axes and not skus:
        # The page's data states no option axis and no SKU: a product with no option.
        return _absent(EvidenceKind.EMBEDDED_JSON, locator(*props_path))
    priced = at(data, *PRICE_MODEL, "priceDisplayType") == SKU_PRICE
    configurations = _configurations(axes, skus, priced)
    if not axes or configurations is None:
        return _review((*SKU_MODEL, "skuInfoMap", "*"), SKU_KEY_UNREAD)
    try:
        value = OptionsValue(
            axes=tuple(OptionAxis(name=name, values=tuple(values)) for name, values in axes),
            configurations=tuple(configurations),
        )
    except ValueError:
        return _review(props_path, SKU_KEY_UNREAD)
    evidence = [
        _evidence(props_path, FieldStatus.CONFIRMED, " / ".join(name for name, _ in axes)),
        _evidence((*SKU_MODEL, "skuInfoMap", "*"), FieldStatus.CONFIRMED, f"{len(skus)} SKU"),
    ]
    # A promotion or member price beside a SKU's original price is kept as a fact (the owner's
    # price basis): one entry per SKU whose stated discount differs from its original price.
    for sku in skus:
        if sku.price and sku.discount and sku.discount != sku.price:
            evidence.append(
                _evidence(
                    (*SKU_MODEL, "skuInfoMap", "*", "discountPrice"),
                    FieldStatus.CONFIRMED,
                    f"{sku.key.replace(SKU_KEY_SEPARATOR, ' / ')}: price {sku.price}, "
                    f"discountPrice {sku.discount}{' (promotion)' if sku.promotion else ''}",
                )
            )
    return FieldFact(FieldStatus.CONFIRMED, value, tuple(evidence))


def _stock(skus: Sequence[_Sku]) -> FieldFact:
    path = (*SKU_MODEL, "skuInfoMap", "*", "canBookCount")
    counts = [sku.stock for sku in skus]
    if not skus or any(count is None for count in counts):
        return FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            StockValue(availability=Availability.REVIEW_REQUIRED),
            (
                Evidence(
                    EvidenceKind.EMBEDDED_JSON,
                    locator(*path),
                    FieldStatus.REVIEW_REQUIRED,
                    normalized=STOCK_NOT_STATED,
                ),
            ),
        )
    on_sale = sum(1 for count in counts if count)
    availability = Availability.ON_SALE if on_sale else Availability.SOLD_OUT
    return FieldFact(
        FieldStatus.CONFIRMED,
        StockValue(availability=availability),
        (_evidence(path, FieldStatus.CONFIRMED, f"{on_sale} of {len(skus)} SKU can be booked"),),
    )


def _quantity_tiers(data: Mapping[str, Any], unit: str) -> FieldFact:
    display_path = (*PRICE_MODEL, "priceDisplayType")
    display = at(data, *display_path)
    if display == SKU_PRICE:
        return _absent(EvidenceKind.EMBEDDED_JSON, locator(*display_path), SKU_PRICE)
    if display != RANGE_PRICE:
        return _review(display_path, PRICE_DISPLAY_UNKNOWN)
    original = _tiers(data, "originalPrices")
    current = _tiers(data, "currentPrices")
    if original is None or current is None:
        return _review((*PRICE_MODEL, "currentPrices"), PRICE_NOT_STATED)
    basis, name = (original, "originalPrices") if original else (current, "currentPrices")
    if not basis or len({begin for begin, _ in basis}) != len(basis):
        return _review((*PRICE_MODEL, name), PRICE_NOT_STATED)
    # A tier states a unit price from a quantity on, and a quantity tier is a stated (quantity,
    # total) pair: no total is ever computed. Every tier is kept verbatim in the evidence, and the
    # field waits for review.
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        tuple(
            Evidence(
                EvidenceKind.EMBEDDED_JSON,
                locator(*PRICE_MODEL, name),
                FieldStatus.REVIEW_REQUIRED,
                observed=f"beginAmount {begin}{unit} price {amount} {CURRENCY}",
                normalized=TIER_UNIT_PRICE_NOT_TOTAL,
            )
            for begin, amount in basis
        ),
    )


def _shipping(data: Mapping[str, Any]) -> FieldFact:
    cost_path = (*FREIGHT, "totalCost")
    fee = _cny_number(at(data, *cost_path))
    location = at(data, *FREIGHT, "location")
    if fee is None or not isinstance(location, str) or not location:
        return _review(cost_path, FREIGHT_NOT_STATED)
    # The freight runs to the account's own receive address (the 배대지), which never leaves the
    # browser: the policy text names where it ships from and the fee, and nothing of the member.
    text = f"{location} 发货, 运费 {fee} {CURRENCY}"
    free = _fen(fee) == 0
    return FieldFact(
        FieldStatus.CONFIRMED,
        ForeignShippingValue(
            kind=ShippingKind.FREE if free else ShippingKind.FIXED,
            policy_text=text,
            currency=CURRENCY,
            fee=None if free else fee,
        ),
        (
            _evidence(cost_path, FieldStatus.CONFIRMED, fee),
            _evidence((*FREIGHT, "location"), FieldStatus.CONFIRMED, location),
        ),
    )


def _detail_description() -> FieldFact:
    """The description is a separate document the page names, which this capture does not hold.

    The page has a description; ICBM did not capture it. That is never ``ABSENT``: it waits for
    review, and 1688's detail images wait for an owner-approved amendment of ADR-0040.
    """
    return FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        (
            Evidence(
                EvidenceKind.DOM_TEXT,
                "description",
                FieldStatus.REVIEW_REQUIRED,
                normalized=DESCRIPTION_NOT_CAPTURED,
            ),
        ),
    )


# ---------------------------------------------------------------- the whole page


def parse_fields(document: DocumentView) -> dict[str, FieldFact]:
    """Every field of the registry, as a captured 1688 page states it."""
    nodes = read(document.body)
    rows = _attributes(nodes)
    fields: dict[str, FieldFact] = {
        "original_name": _name(nodes),
        "brand": _attribute(rows, BRAND_LABELS),
        "origin": _attribute(rows, ORIGIN_LABELS),
        "manufacturer": _absent(EvidenceKind.DOM_TEXT, f"div#{ATTRIBUTES} 生产企业"),
        "notice": _absent(EvidenceKind.DOM_TEXT, f"div#{ATTRIBUTES}"),
        "detail_description": _detail_description(),
        # ADR-0040 §4: not stated on 1688.
        "minimum_sale_price": _absent(EvidenceKind.DOM_TEXT, "page"),
        "sales_channels": _absent(EvidenceKind.DOM_TEXT, "page"),
    }
    data, refusal = member(document)
    if data is None:
        reason = refusal or "EMBEDDED_DATA_ABSENT"
        for key in ("prices", "options", "stock", "quantity_tiers", "shipping"):
            fields[key] = _unavailable(reason)
        return fields
    unit_value = at(data, *UNIT)
    unit = unit_value if isinstance(unit_value, str) else ""
    skus = _skus(data)
    fields["prices"] = _prices(data, skus, unit)
    fields["options"] = _options(data, skus)
    fields["stock"] = _stock(skus)
    fields["quantity_tiers"] = _quantity_tiers(data, unit)
    fields["shipping"] = _shipping(data)
    return fields
