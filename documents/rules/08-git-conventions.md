<!-- Moved verbatim from CLAUDE.md §8 (Git conventions) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §8` references resolve here (PATH_MIGRATION_MAP). -->

## 8. Git conventions

### 8.1 Branches

```text
main                         always releasable / green
feat/<area>-<short-desc>
fix/<area>-<short-desc>
docs/<short-desc>
chore/<short-desc>
```

`<area>` is one of:

```text
connect | collect | products | register | operate | ui | infra
```

Fulfillment work uses the `operate` area because fulfillment belongs inside OPERATE.

### 8.2 Commits

Use conventional-commit style and one logical change per commit.

Examples:

```text
feat(collect): add KM통상 detail fact extraction
fix(register): reconcile unknown create before retry
docs(adr): record database choice
```

Do not mix broad refactoring with unrelated behavior changes in the same commit.

### 8.3 Pull requests

Every PR states:

1. what changed
2. which contract it implements or modifies
3. whether schema changed
4. whether external writes are involved
5. how it was verified — commands and observed evidence
6. what it explicitly does not do

A PR that changes an approved contract links the ADR authorizing it. No ADR, no contract change.

No merge based only on unit/mock PASS when the roadmap requires real E2E/read-back evidence.
