"""Japanese consumption tax as ICBM applies it to a Super Delivery price (owner, Issue #219
``6164685916``).

Super Delivery states its wholesale prices 税抜 (the set table's column reads ``価格（税抜）``),
while the cost of an order is what the member pays with the consumption tax added. The owner's
decision: the purchase cost of a set is its 税抜 price plus the consumption tax, rounded up to the
whole yen, at

- the reduced rate when the page marks the product as 軽減税率対象 — its product block states
  「（軽減税率対象商品）」, or the product name starts with 「※」 and the company's description
  explains that mark (「【※】マークは軽減税率対象品目です」);
- the standard rate otherwise.

The rates and the reading of the mark are ICBM's own versioned constant, patched by ICBM and never a
user setting (like ADR-0038 §4's USD 150 threshold). ``TAX_REVISION`` names the version that made a
price, and it is written into that price's evidence, so every computed amount says how it was made.
The 税抜 price stays recorded beside it exactly as stated.
"""

import re

TAX_REVISION = "jp-consumption-tax-1"
STANDARD_RATE_PERCENT = 10
REDUCED_RATE_PERCENT = 8
# The product block's own statement that the product takes the reduced rate.
REDUCED_MARK = "軽減税率対象"
# A product name that starts with this mark, on a page whose company explains the mark.
NAME_MARK = "※"
NAME_MARK_EXPLAINED = re.compile(r"【※】\s*マーク[はが]\s*軽減税率対象")


def tax_included(amount: int, rate_percent: int) -> int:
    """``amount`` yen 税抜 with ``rate_percent`` consumption tax added, rounded up to the yen."""
    return -(-amount * (100 + rate_percent) // 100)
