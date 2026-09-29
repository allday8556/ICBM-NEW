<!-- Moved verbatim from CLAUDE.md §9 (Definition of Done) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §9` references resolve here (PATH_MIGRATION_MAP). -->

## 9. Definition of Done

A feature is not done because a function exists or a test is green.

1. UI action reaches the intended service.
2. Service uses the canonical contract.
3. Integration performs the real read/write where permitted.
4. Result is read back from the external system when applicable.
5. Canonical DB reflects the read-back.
6. UI reflects canonical state after reload.
7. The same flow succeeds again in a fresh session.
8. No unrelated flow regresses.

Acceptance evidence belongs in `documents/acceptance/`, not chat. It records correlation IDs, external IDs, timestamps, read-back evidence, and the fresh-session condition.
