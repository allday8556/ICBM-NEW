"""Extraction identity of the Adaptive engine (ADR-0010 §12, ADR-0017 §5.1, §5.6).

``EXTRACTOR_REVISION`` is the engine's semantic revision: it enters the semantic tuple, and the
engine goldens force it to advance when behaviour changes. ``EXTRACTOR_FINGERPRINT`` is the
implementation identity over ``EXTRACTOR_INPUTS``: provenance and validation freshness only. The
hashed set is every other module of this package; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "adaptive-engine-1"
EXTRACTOR_INPUTS = (
    "app/collect/adaptive/__init__.py",
    "app/collect/adaptive/canonical.py",
    "app/collect/adaptive/capture.py",
    "app/collect/adaptive/document.py",
    "app/collect/adaptive/engine.py",
    "app/collect/adaptive/hooks.py",
    "app/collect/adaptive/lint.py",
    "app/collect/adaptive/locator.py",
    "app/collect/adaptive/profiles.py",
    "app/collect/adaptive/validation.py",
)
EXTRACTOR_FINGERPRINT = "3345acb0ad90b5cce1bdb89b34e57d150b90c7a546309cc6d13d643a0f2fab64"
