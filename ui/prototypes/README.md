# UI Prototypes

Authority: [`docs/UI_SOURCE_OF_TRUTH.md`](../../docs/UI_SOURCE_OF_TRUTH.md) decides which prototype is current. This file mirrors that record and must change in the same commit whenever the record changes.

## Canonical prototype

`icbm_redesign_test_v29_final.html` — v29, APPROVED — CANONICAL

- SHA-256: `896ad87011b8615b8a6a9cd3e790ca04f52e908e4ff7b6a26ea4bf5372dfeb82`
- Size: `323751` bytes
- Authority: `docs/UI_SOURCE_OF_TRUTH.md`

Prototype files are stored byte-exact (`.gitattributes`: `-text`), and `tests/unit/test_repository_rules.py` verifies the file against the record on every CI run.

## Superseded

`icbm_redesign_test_v28_icbm_new_gaps.html` (v28) is kept for history only and is not an implementation source.

Before implementation switches to a new prototype revision, `docs/UI_SOURCE_OF_TRUTH.md` must record its filename, SHA-256, size, review decision and repository-copy verification.
