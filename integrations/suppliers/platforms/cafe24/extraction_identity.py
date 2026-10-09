"""Extraction identity of the Cafe24 template's collection knowledge (ADR-0010 §12, ADR-0030 §4).

The hashed set is every module of ``collect/``; this manifest is never part of it. A site's
extraction identity joins this revision with its own and hashes this fingerprint with its file.
"""

EXTRACTOR_REVISION = "cafe24-2"
EXTRACTOR_INPUTS = (
    "integrations/suppliers/platforms/cafe24/collect/__init__.py",
    "integrations/suppliers/platforms/cafe24/collect/dom.py",
    "integrations/suppliers/platforms/cafe24/collect/facts.py",
    "integrations/suppliers/platforms/cafe24/collect/identity.py",
    "integrations/suppliers/platforms/cafe24/collect/images.py",
    "integrations/suppliers/platforms/cafe24/collect/revision.py",
)
# A reader cannot recompute a SHA-256; the proof is mechanical:
# tests/contracts/test_repository_rules.py recomputes this digest from EXTRACTOR_INPUTS, and the
# merge guard requires that test green in the FULL CI of the exact HEAD.
EXTRACTOR_FINGERPRINT = "b001fa4c53dcd89beb1e576280e9b5e92323b993cf3d7eb1b73fafd4d6c95a71"
