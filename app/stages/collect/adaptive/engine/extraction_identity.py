"""Extraction identity of the Adaptive engine (ADR-0010 §12, ADR-0017 §5.1, §5.6).

``EXTRACTOR_REVISION`` is the engine's semantic revision: it enters the semantic tuple, and the
engine goldens force it to advance when behaviour changes. ``EXTRACTOR_FINGERPRINT`` is the
implementation identity over ``EXTRACTOR_INPUTS``: provenance and validation freshness only. The
hashed set is every other module of this package; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "adaptive-engine-1"
EXTRACTOR_INPUTS = (
    "app/stages/collect/adaptive/engine/__init__.py",
    "app/stages/collect/adaptive/engine/canonical.py",
    "app/stages/collect/adaptive/engine/capture.py",
    "app/stages/collect/adaptive/engine/document.py",
    "app/stages/collect/adaptive/engine/engine.py",
    "app/stages/collect/adaptive/engine/hooks.py",
    "app/stages/collect/adaptive/engine/lint.py",
    "app/stages/collect/adaptive/engine/locator.py",
    "app/stages/collect/adaptive/engine/profiles.py",
    "app/stages/collect/adaptive/engine/validation.py",
)
EXTRACTOR_FINGERPRINT = "c44f0007a990062141b69272895a91560c85d69bb2538ee5fdd73ed88cf544ed"
