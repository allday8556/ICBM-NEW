"""The identity of the Cafe24 template's collection contract (ADR-0030 §4).

One revision covers everything the template decides: the source identity rule, the product-facts
parser and the image-role rules. A semantic change to any of them advances this string, and the
repository pin refuses a package whose files changed without it. A site's extraction revision is
this revision joined with the site's own (``cafe24-1+<site revision>``), so a template change
advances every site on it.

``cafe24-1`` (2026-10-10): KM통상's ``kmretail-3`` rules, with the words and regions a site may vary
read from its vocabulary, its profile values (paths, and the owner's 5 MiB per image, Issue #219
``6086299406``) in ``profile.py``, and the stated differences the ``facts`` module lists: wherever
KM통상's parser reads a first value, a guess or nothing, the template fails closed.

``cafe24-2`` (2026-10-10, ADR-0031 and ADR-0032 after the U-PICK reconnaissance):
- the sales-channel field, read from the site's own phrases;
- price roles from the site's purchase-price and list-price words;
- a shipping-fee range read as its highest amount;
- a declared title that only appends a suffix to the 상품명 row reads as the row;
- the smart-design purchase controls ``actionBuy`` and ``actionCart``;
- the representative image's container as a vocabulary region.

``cafe24-3`` (2026-10-10, ADR-0035 after U-PICK acceptance run 1):
- a restricting sales-channel phrase is read in the product's name too, never an allowed one;
- a channel row that is not wholly allowed reads as its restrictions (NR-03) only when every word
  of it is a configured phrase or a separator;
- the region-surcharge row (label slot ``shipping_region_fee``, ``추가배송비``) is kept as words in
  the shipping policy text and never priced; an image-only value or words over 200 characters
  are held;
- a minimum row that is exactly a per-quantity list reads its ``1개`` amount, which must be above
  zero;
- text is cleaned of the byte-order mark, the zero-width space and the word joiner, and a no-break
  space is a space, in page text and in the declared title, so a text made only of them is empty,
  and a title with a doubled space still matches its row plus the site's suffix.
"""

EXTRACTION_REVISION = "cafe24-3"
