"""Extraction identity of KM통상's collection knowledge (ADR-0010 §12).

The hashed set is every module of ``collect/``; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "kmretail-1"
EXTRACTOR_INPUTS = (
    "integrations/suppliers/kmretail/collect/__init__.py",
    "integrations/suppliers/kmretail/collect/dom.py",
    "integrations/suppliers/kmretail/collect/facts.py",
    "integrations/suppliers/kmretail/collect/identity.py",
    "integrations/suppliers/kmretail/collect/images.py",
    "integrations/suppliers/kmretail/collect/revision.py",
)
# Re-pinned for the Issue #151 path-only move (ADR-0021 section 9): collect/facts.py and
# ../collection.py changed only the import app.collect.facts -> app.stages.collect.facts.
# EXTRACTOR_REVISION is unchanged. tests/contracts/test_repository_rules.py recomputes this
# digest from EXTRACTOR_INPUTS on every CI run.
EXTRACTOR_FINGERPRINT = "7055eac566872047afbfb4e961605915ea96fb5e56e5e77eb90f0d0fb1684ffa"
