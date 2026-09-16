"""The identity of KM통상's whole collection contract (Issue #52 ruling 5702780630).

One revision covers everything this package decides — the source identity rule, the product-facts
parser and the image-role rules — because they are read from one document and accepted together.
A semantic change to any of them advances this string, and the repository pin refuses a package
whose files changed without it.

It advances from ``kmretail-images-1``, which covered image-role classification alone.
"""

EXTRACTION_REVISION = "kmretail-1"
