"""Extraction identity of KM통상's collection knowledge (ADR-0010 §12).

The hashed set is every module of ``collect/``; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "kmretail-3"
EXTRACTOR_INPUTS = (
    "integrations/suppliers/kmretail/collect/__init__.py",
    "integrations/suppliers/kmretail/collect/dom.py",
    "integrations/suppliers/kmretail/collect/facts.py",
    "integrations/suppliers/kmretail/collect/identity.py",
    "integrations/suppliers/kmretail/collect/images.py",
    "integrations/suppliers/kmretail/collect/revision.py",
)
# Re-pinned for kmretail-3 (2026-10-03): collect/facts.py reads a 자율 minimum-price row as no
# minimum, and collect/revision.py advances the revision with it.
# A reader cannot recompute a SHA-256; the proof is mechanical:
# tests/contracts/test_repository_rules.py recomputes this digest from EXTRACTOR_INPUTS, and the
# merge guard requires that test green in the FULL CI of the exact HEAD.
EXTRACTOR_FINGERPRINT = "d885d839161412a9071052b0a058d9b5d4d2454040e3a750c905a5202823e24f"
