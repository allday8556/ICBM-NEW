"""Extraction identity of KM통상's collection knowledge (ADR-0010 §12).

The hashed set is every module of ``collect/``; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "kmretail-5"
EXTRACTOR_INPUTS = (
    "integrations/suppliers/kmretail/collect/__init__.py",
    "integrations/suppliers/kmretail/collect/dom.py",
    "integrations/suppliers/kmretail/collect/facts.py",
    "integrations/suppliers/kmretail/collect/identity.py",
    "integrations/suppliers/kmretail/collect/images.py",
    "integrations/suppliers/kmretail/collect/revision.py",
)
# Re-pinned for kmretail-4 (2026-10-10, ADR-0031, ADR-0032): collect/facts.py reports no
# sales-channel restriction and reads a shipping range as its highest amount; collect/revision.py
# advances the revision with it.
# Re-pinned for kmretail-5 (2026-10-11, ADR-0010 §7): a minimum-price cell is read only when it
# states exactly one amount.
# A reader cannot recompute a SHA-256; the proof is mechanical:
# tests/contracts/test_repository_rules.py recomputes this digest from EXTRACTOR_INPUTS, and the
# merge guard requires that test green in the FULL CI of the exact HEAD.
EXTRACTOR_FINGERPRINT = "9dcad434199d1a6f95079cee9b00464a69493175d3677c714ecb1843e48095cb"
