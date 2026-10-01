<!-- Moved verbatim from CLAUDE.md §9 (Definition of Done) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §9` references resolve here (PATH_MIGRATION_MAP). -->

## 9. Definition of Done

A feature or milestone closeout is not done because a function exists or a test is green. The
end-to-end conditions below apply when closing a user-visible feature or milestone, not to every
small documentation, infrastructure, refactoring or internal-only PR.

1. UI action reaches the intended service.
2. Service uses the canonical contract.
3. Integration performs the real read/write where permitted.
4. Result is read back from the external system when applicable.
5. Canonical DB reflects the read-back.
6. UI reflects canonical state after reload.
7. The same flow succeeds again in a fresh session.
8. No unrelated flow regresses.

For a small internal PR, done means the scoped change is complete, its focused tests and applicable
lint/type checks pass, and the CI scope selected by §14.2 is green. It does not independently owe an
external read-back or a fresh-session replay unless it changes that flow or is the feature/milestone
closeout that claims the flow works.

Acceptance evidence belongs in `documents/acceptance/`, not chat. At feature or milestone closeout
it records the applicable correlation IDs, external IDs, timestamps, read-back evidence, and
fresh-session condition. An item that cannot apply to the closeout is recorded as not applicable;
it is not mechanically demanded from an unrelated internal PR.
