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

``superdelivery-3`` (2026-10-14, the PR #351 audit): a text value is never a cut of the statement.
The description is stored whole (its evidence still quotes the first 200 characters), or held for
review when it carries URL material; the shipping policy text is whole. Every set is checked for
its regular price: a campaign set of several units without its own holds the options for review
(``REGULAR_PRICE_NOT_STATED``), whichever set it is.

``superdelivery-4`` (2026-10-14, the PR #351 re-audit): one price shown is the regular price only
when the page marks no discount. Under the page's discount mark (``.campaign-info``) with no struck
regular price beside it, the price shown is the discounted one: ``prices`` and the options are
``REVIEW_REQUIRED`` (``REGULAR_PRICE_NOT_STATED``), never priced on it.
"""

EXTRACTION_REVISION = "superdelivery-4"
