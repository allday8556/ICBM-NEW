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
(``sales_channels`` is ``ABSENT``); a fee cell that is exactly a range is read as its highest amount
(the owner's rule, Issue #219 6086421199); and the profile's per-image bound is 5 MiB (the owner's
decision for every supplier, Issue #219 6086299406).

``kmretail-5`` (2026-10-11, ADR-0010 §7 fail closed): a minimum-price cell is read only when it
states exactly one amount (``N``, ``N원``, ``N원 이상``). The parser used to take the first number
of any cell: ``1개 13,900원 이상/ 2개 …`` read as 1 won and ``12,000원 / 15,000원`` as 12,000, a
confidently wrong minimum. Such a cell is now ``REVIEW_REQUIRED``. Found while proving the
``cafe24`` template against this parser (ADR-0035 U1).
"""

EXTRACTION_REVISION = "kmretail-5"
