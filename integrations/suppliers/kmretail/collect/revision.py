"""The identity of KM통상's whole collection contract (Issue #52 ruling 5702780630).

One revision covers everything this package decides — the source identity rule, the product-facts
parser and the image-role rules — because they are read from one document and accepted together.
A semantic change to any of them advances this string, and the repository pin refuses a package
whose files changed without it.

It advances from ``kmretail-images-1``, which covered image-role classification alone.

``kmretail-2`` (2026-10-03, the user's decision after the E3 real run): only the product module's
``BigImage`` inside ``keyImg`` is the representative image, and the rest of that container is
layout furniture; and the profile's per-image bound is 4 MiB.

``kmretail-3`` (2026-10-03, the user's rule): a minimum-price row that says the price is free
(``자율``) states that there is no minimum, so the field is ``ABSENT`` like an unstated one; any
other row without an amount stays ``REVIEW_REQUIRED``.

``kmretail-4`` (2026-10-10, ADR-0031 and ADR-0032): the page states no sales-channel restriction
(``sales_channels`` is ``ABSENT``), and a shipping fee stated as a range is read as its highest
amount (the owner's rule, Issue #219 6086421199).
"""

EXTRACTION_REVISION = "kmretail-4"
