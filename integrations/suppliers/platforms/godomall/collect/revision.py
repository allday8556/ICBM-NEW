"""The identity of the Godomall template's collection contract (ADR-0030 §4, ADR-0034).

One revision covers everything the template decides: the source identity rule, the product-facts
parser, the image-role rules and its profile values. A semantic change to any of them advances it,
and a site's extraction revision joins it with the site's own (``godomall-1+<site revision>``).

``godomall-1`` (2026-10-10): written from 건강산's reconnaissance captures (Issue #219 6086058056),
with ADR-0034's description minimum and free-over shipping readings.
"""

EXTRACTION_REVISION = "godomall-1"
