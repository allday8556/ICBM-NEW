"""Extraction identity of the Adaptive engine (ADR-0010 §12, ADR-0017 §5.1, §5.6).

``EXTRACTOR_REVISION`` is the engine's semantic revision: it enters the semantic tuple, and the
engine goldens force it to advance when behaviour changes. ``EXTRACTOR_FINGERPRINT`` is the
implementation identity over ``EXTRACTOR_INPUTS``: provenance and validation freshness only. The
hashed set is every other module of this package; this manifest is never part of it.
"""

EXTRACTOR_REVISION = "adaptive-engine-3"
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
# adaptive-engine-3 (2026-10-10, owner decision Issue #219 6097082224; ADR-0013 ruling B): a page
# that proves it has no option control now reads ``options = ABSENT`` with its control-state
# evidence instead of CONFIRMED with zero axes, so the goldens were recorded again and, the
# engine code having changed, EXTRACTOR_FINGERPRINT was re-pinned.
# adaptive-engine-2 (2026-10-10, ADR-0031 §2): FIELD_REGISTRY gained the COVERAGE field
# ``sales_channels``, so every extraction reports one field more and the goldens were recorded
# again. Its lint revision advanced to adaptive-lint-2 for the same field, so the fingerprint was
# re-pinned.
# Re-pinned for the Issue #151 path-only move (ADR-0021 section 9): the hashed inputs moved from
# app/collect/adaptive/ to app/stages/collect/adaptive/engine/ and their imports followed.
# EXTRACTOR_REVISION is unchanged and the engine goldens pass unchanged. A reader cannot recompute
# a SHA-256; the proof is mechanical: tests/unit/collect/adaptive/engine/test_identity.py
# recomputes this digest from EXTRACTOR_INPUTS, and the merge guard requires that test green in
# the FULL CI of the exact HEAD.
EXTRACTOR_FINGERPRINT = "12b1eddc999a366cec05cc8cbd7e122c2e62553b141452e014122afc0a52fba4"
