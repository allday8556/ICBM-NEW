"""Extraction identity of KM통상's collection knowledge (ADR-0010 §12).

The hashed set is every module of ``collect/``; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "kmretail-images-1"
EXTRACTOR_INPUTS = (
    "integrations/suppliers/kmretail/collect/__init__.py",
    "integrations/suppliers/kmretail/collect/images.py",
)
EXTRACTOR_FINGERPRINT = "dff8469178e100d49b3b25e2e349df88b89fff2ea4c351879125e1a6f55005d9"
