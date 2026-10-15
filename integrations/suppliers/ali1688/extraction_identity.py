"""Extraction identity of 1688's collection knowledge (ADR-0010 §12, ADR-0040 C2).

The hashed set is every module of ``collect/``; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "ali1688-4"
EXTRACTOR_INPUTS = (
    "integrations/suppliers/ali1688/collect/__init__.py",
    "integrations/suppliers/ali1688/collect/dom.py",
    "integrations/suppliers/ali1688/collect/embedded.py",
    "integrations/suppliers/ali1688/collect/facts.py",
    "integrations/suppliers/ali1688/collect/identity.py",
    "integrations/suppliers/ali1688/collect/images.py",
    "integrations/suppliers/ali1688/collect/revision.py",
)
# A reader cannot recompute a SHA-256; the proof is mechanical:
# tests/contracts/test_repository_rules.py recomputes this digest from EXTRACTOR_INPUTS, and the
# merge guard requires that test green in the FULL CI of the exact HEAD.
EXTRACTOR_FINGERPRINT = "043b78281b9a60532c6090c0f250ac9d0970066b0196e988425e031278cc4260"
