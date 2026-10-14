"""The identity of Super Delivery's whole collection contract (ADR-0010 §12, ADR-0040 C1).

One revision covers everything this package decides — the source identity rule, the facts parser
and the image-role rules — because they are read from one captured page and accepted together. A
semantic change to any of them advances this string, and the repository pin refuses a package
whose files changed without it.

``superdelivery-1`` (2026-10-14, ADR-0040 slice C1): the first reading, made on three captures the
owner's signed-in browser showed (two of one company with a price disclosed, one of another company
with the price undisclosed and two sets).
"""

EXTRACTION_REVISION = "superdelivery-1"
