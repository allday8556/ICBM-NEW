# UI Prototypes

Authority: [`documents/contracts/ui/UI_SOURCE_OF_TRUTH.md`](../../documents/contracts/ui/UI_SOURCE_OF_TRUTH.md) decides which prototype is current. This file mirrors that record and must change in the same commit whenever the record changes. It decides nothing itself: it is a test-pinned mirror (`tests/contracts/test_repository_rules.py::test_prototype_readme_mirrors_the_canonical_record`), as it was at `ui/prototypes/README.md` before Issue #151.

## Canonical prototype

`icbm_redesign_test_v29_final.html` — v29, APPROVED — CANONICAL

- SHA-256: `896ad87011b8615b8a6a9cd3e790ca04f52e908e4ff7b6a26ea4bf5372dfeb82`
- Size: `323751` bytes
- Authority: `documents/contracts/ui/UI_SOURCE_OF_TRUTH.md`

Prototype files are stored byte-exact (`.gitattributes`: `-text`), and `tests/contracts/test_repository_rules.py` verifies the file against the record on every CI run.

## Extension Collector prototype

`icbm_extension_collector.html` — the Chrome extension side panel's approved prototype (the user's
`ICBM 확장 수집기.html`)

- SHA-256: `5eec99911aca06a857ea5b5460c77ac25f0270a4384e7257a81b5466b6bdb879`
- Size: `7949739` bytes
- Authority: `documents/contracts/ui/UI_SOURCE_OF_TRUTH.md` (Extension Collector visual source)

## Superseded

`icbm_redesign_test_v28_icbm_new_gaps.html` (v28) is kept for history only and is not an implementation source.

Before implementation switches to a new prototype revision, `documents/contracts/ui/UI_SOURCE_OF_TRUTH.md` must record its filename, SHA-256, size, review decision and repository-copy verification.
