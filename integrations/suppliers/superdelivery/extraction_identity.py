"""Extraction identity of 슈퍼딜리버리's collection knowledge (ADR-0010 §12, ADR-0040 C1).

The hashed set is every module of ``collect/``; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "superdelivery-2"
EXTRACTOR_INPUTS = (
    "integrations/suppliers/superdelivery/collect/__init__.py",
    "integrations/suppliers/superdelivery/collect/dom.py",
    "integrations/suppliers/superdelivery/collect/facts.py",
    "integrations/suppliers/superdelivery/collect/identity.py",
    "integrations/suppliers/superdelivery/collect/images.py",
    "integrations/suppliers/superdelivery/collect/revision.py",
    "integrations/suppliers/superdelivery/collect/tax.py",
)
# A reader cannot recompute a SHA-256; the proof is mechanical:
# tests/contracts/test_repository_rules.py recomputes this digest from EXTRACTOR_INPUTS, and the
# merge guard requires that test green in the FULL CI of the exact HEAD.
EXTRACTOR_FINGERPRINT = "fba71bbdccbef6a01fe4600fd5af812f909740f8aa8a2f2c91508999db43c5e9"
