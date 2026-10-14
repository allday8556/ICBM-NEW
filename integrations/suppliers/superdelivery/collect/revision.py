"""The identity of Super Delivery's whole collection contract (ADR-0010 §12, ADR-0040 C1).

One revision covers everything this package decides — the source identity rule, the facts parser
and the image-role rules — because they are read from one captured page and accepted together. A
semantic change to any of them advances this string, and the repository pin refuses a package
whose files changed without it.

``superdelivery-1`` (2026-10-14, ADR-0040 slice C1): the first reading, made on three captures the
owner's signed-in browser showed (two of one company with a price disclosed, one of another company
with the price undisclosed and two sets). A 税抜 price was held for review: the page states no 税込
price.

``superdelivery-2`` (2026-10-14, the owner's decision, Issue #219 ``6164685916``): the purchase
price of a set is its regular price — the struck price beside a time-limited campaign price, which
is recorded only — 税抜 plus the Japanese consumption tax (8 % for a product the page marks
軽減税率対象, else 10 %; ``tax.py``, ``jp-consumption-tax-1``), rounded up to the whole yen; each
set's configuration carries its own. Shipping amounts stay as stated, and their policy text
says 税抜.
"""

EXTRACTION_REVISION = "superdelivery-2"
