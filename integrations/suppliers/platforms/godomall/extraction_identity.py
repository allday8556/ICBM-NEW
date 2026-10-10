"""Extraction identity of the Godomall template's collection knowledge (ADR-0010 §12, ADR-0030 §4).

The hashed set is every module of ``collect/``; this manifest is never part of it. A site's
extraction identity joins this revision with its own and hashes this fingerprint with its file.
"""

EXTRACTOR_REVISION = "godomall-2"
EXTRACTOR_INPUTS = (
    "integrations/suppliers/platforms/godomall/collect/__init__.py",
    "integrations/suppliers/platforms/godomall/collect/dom.py",
    "integrations/suppliers/platforms/godomall/collect/facts.py",
    "integrations/suppliers/platforms/godomall/collect/identity.py",
    "integrations/suppliers/platforms/godomall/collect/images.py",
    "integrations/suppliers/platforms/godomall/collect/profile.py",
    "integrations/suppliers/platforms/godomall/collect/revision.py",
)
# A reader cannot recompute a SHA-256; the proof is mechanical:
# tests/contracts/test_repository_rules.py recomputes this digest from EXTRACTOR_INPUTS, and the
# merge guard requires that test green in the FULL CI of the exact HEAD.
EXTRACTOR_FINGERPRINT = "0a02292d74e15654818b6b5efd63f9766b52b7356d9ba815bb179859b95c530c"
