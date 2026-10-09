"""The identity of the Cafe24 template's collection contract (ADR-0030 §4).

One revision covers everything the template decides: the source identity rule, the product-facts
parser and the image-role rules. A semantic change to any of them advances this string, and the
repository pin refuses a package whose files changed without it. A site's extraction revision is
this revision joined with the site's own (``cafe24-1+<site revision>``), so a template change
advances every site on it.

``cafe24-1`` (2026-10-10): KM통상's ``kmretail-3`` rules, with the words and regions a site may vary
read from its vocabulary, its profile values (paths and the owner's 5 MiB per image, Issue #219
``6086299406``) in ``profile.py``, and two stated differences:
- a shipping-fee cell that is not exactly one amount of won is ``REVIEW_REQUIRED``, never an amount
  it names (ADR-0010 §7);
- an active purchase control decides ``ON_SALE`` even beside sold-out words (ADR-0010 §10).
"""

EXTRACTION_REVISION = "cafe24-1"
