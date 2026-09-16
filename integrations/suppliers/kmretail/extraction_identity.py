"""Extraction identity of KM통상's collection knowledge (ADR-0010 §12).

The hashed set is every module of ``collect/``; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "kmretail-images-1"
EXTRACTOR_INPUTS = (
    "integrations/suppliers/kmretail/collect/__init__.py",
    "integrations/suppliers/kmretail/collect/images.py",
)
EXTRACTOR_FINGERPRINT = "7f363dcec5f352c99cf9620121568cba0624b6c804a821102e8e2c332b584a82"
