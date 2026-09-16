"""Extraction identity of KM통상's collection knowledge (ADR-0010 §12).

The hashed set is every module of ``collect/``; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "kmretail-images-1"
EXTRACTOR_INPUTS = (
    "integrations/suppliers/kmretail/collect/__init__.py",
    "integrations/suppliers/kmretail/collect/images.py",
)
EXTRACTOR_FINGERPRINT = "8f13d3f6477a17bf0b3fc092f055e13c39aba4ef53cd63d8405f1796becf7dcf"
