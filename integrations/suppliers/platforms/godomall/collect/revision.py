"""The identity of the Godomall template's collection contract (ADR-0030 §4, ADR-0034).

One revision covers everything the template decides: the source identity rule, the product-facts
parser, the image-role rules and its profile values. A semantic change to any of them advances it,
and a site's extraction revision joins it with the site's own (``godomall-1+<site revision>``).

``godomall-1`` (2026-10-10): written from 건강산's reconnaissance captures (Issue #219 6086058056),
with ADR-0034's description minimum and free-over shipping readings.

``godomall-2`` (2026-10-10, ADR-0035 after U-PICK acceptance run 1):
- a restricting sales-channel phrase is read in the product's name too, never an allowed one;
- a channel row that is not wholly allowed reads as its restrictions (NR-03) only when every word
  of it is a configured phrase or a separator;
- a region layer's words are kept in the shipping policy text and never priced, replacing
  ADR-0034 §2's hold of a layer that states an amount; an image-only layer or words over 200
  characters are held;
- a minimum row that is exactly a per-quantity list reads its ``1개`` amount, which must be above
  zero;
- text is cleaned of the byte-order mark, the zero-width space and the word joiner, and a no-break
  space is a space, so a text made only of them is empty.
"""

EXTRACTION_REVISION = "godomall-2"
