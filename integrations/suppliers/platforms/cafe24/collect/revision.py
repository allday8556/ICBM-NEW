"""The identity of the Cafe24 template's collection contract (ADR-0030 §4).

One revision covers everything the template decides: the source identity rule, the product-facts
parser and the image-role rules. A semantic change to any of them advances this string, and the
repository pin refuses a package whose files changed without it. A site's extraction revision is
this revision joined with the site's own (``cafe24-1+<site revision>``), so a template change
advances every site on it.

``cafe24-1`` (2026-10-10): KM통상's ``kmretail-3`` rules, with the words and regions a site may vary
read from its vocabulary, and one stated difference: a shipping-fee cell that states more than
one amount is ``REVIEW_REQUIRED``, never its first amount (ADR-0010 §7).
"""

EXTRACTION_REVISION = "cafe24-1"
