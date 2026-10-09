"""The identity of the Cafe24 template's collection contract (ADR-0030 §4).

One revision covers everything the template decides: the source identity rule, the product-facts
parser and the image-role rules. A semantic change to any of them advances this string, and the
repository pin refuses a package whose files changed without it. A site's extraction revision is
this revision joined with the site's own (``cafe24-1+<site revision>``), so a template change
advances every site on it.

``cafe24-1`` (2026-10-10): KM통상's ``kmretail-3`` rules, with the words and regions a site may vary
read from its vocabulary.


``cafe24-2`` (2026-10-10, ADR-0031 and ADR-0032 after the U-PICK reconnaissance):
- the sales-channel field, read from the site's own phrases;
- price roles from the site's purchase-price and list-price words;
- a shipping-fee range read as its highest amount;
- a declared title that only appends a suffix to the 상품명 row reads as the row;
- the smart-design purchase controls ``actionBuy`` and ``actionCart``;
- the representative image's container as a vocabulary region.
"""

EXTRACTION_REVISION = "cafe24-2"
