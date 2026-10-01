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

Every PR states what changed, how it was verified and what it deliberately does not do. A BASIC
documentation, infrastructure or simple internal PR may stop there.

Add the applicable contract, schema/migration and external-write details when the PR changes them,
and identify the §14.2 validation tier. A PR that changes shared canonical behaviour, an externally
consumed canonical contract or product policy links the accepted ADR or canonical decision that
authorizes it. An internal schema, endpoint implementation or adapter mapping for already approved
behaviour is an implementation decision: explain and test it in the PR, but do not create an ADR
only to satisfy a template.

Unit/mock PASS alone cannot close a user feature or milestone whose acceptance criteria require
real E2E/read-back evidence. That closeout rule does not turn every intermediate provider-zero PR
into a LIVE verification run.
