"""The identity of 1688's whole collection contract (ADR-0010 §12, ADR-0040 C2).

One revision covers everything this package decides — the source identity rule, the facts parser
over the captured page and its embedded-data member, and the image-role rules — because they are
read from one capture and accepted together. A semantic
change to any of them advances this string, and the repository pin refuses a package whose files
changed without it.

``ali1688-1`` (2026-10-14, ADR-0040 slice C2): the first reading, made on five captures the owner's
signed-in browser showed: three per-SKU priced products (20, 4 and 2 SKUs) and two tier-priced
ones (2 and 11 SKUs). The purchase price is the SKU's original price (the owner's price basis, Issue
#219 ``6164685916``); a promotion price is recorded only. A tier price states a unit price, so
quantity tiers wait for review with every tier quoted; the description is not captured, so
``detail_description`` waits for review and there are no detail images.
"""

EXTRACTION_REVISION = "ali1688-1"
