<!-- Moved verbatim from CLAUDE.md §2 (Absolute no-legacy rule) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §2` references resolve here (PATH_MIGRATION_MAP). -->

## 2. Absolute no-legacy rule

**No legacy code, owner, DB schema, patch chain, test harness, marketplace ID, runtime behavior, or previously wired UI functionality is inherited from `allday8556/ICBM-PROJECT` or from the #86 UI rebuild.**

This is unconditional. It is not relaxed because copying appears faster or because a legacy component already works.

Do not copy or transplant:

- old Python/JS owners
- old DB/schema/migrations
- old Collector runtime/extension code
- old API handlers/contracts
- old patch/hotfix chains
- old tests as implementation truth
- old marketplace IDs/runtime state
- #86 functional implementation

Legacy may be inspected only when the architect explicitly authorizes a narrow reference case in GitHub. Default: **do not inspect and do not reuse**.
