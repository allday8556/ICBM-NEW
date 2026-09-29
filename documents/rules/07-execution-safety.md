<!-- Moved verbatim from CLAUDE.md §7 (Execution safety) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §7` references resolve here (PATH_MIGRATION_MAP). -->

## 7. Execution safety

### 7.1 Execution mode

Global external-write mode is:

```text
DRY_RUN | LIVE
```

Default during development is `DRY_RUN`.

Marketplace sandbox/test accounts, where available, are environment/account configuration — not a third global execution mode.

`LIVE` requires the intended verification scope to be explicit in GitHub and user approval when the action is protected/destructive.

### 7.2 Requires user approval before execution

- any destructive marketplace action: delete/deactivate or equivalent
- any real supplier order (발주)
- bulk live writes or bulk destructive operations
- destructive migrations, table/row drops, irreversible rewrites
- compliance gate override
- credential changes that replace/delete active credentials
- deleting branches or force-pushing

For first-time real marketplace CREATE/UPDATE verification, use the explicit LIVE acceptance scope defined in GitHub and obtain user approval before the run.

Approval is per action/scope and does not automatically generalize to future actions.

### 7.3 Never do

- commit credentials, tokens, cookies, or session state
- commit real customer/order personal data in tests or fixtures
- spin repeated failed supplier logins
- retry an unknown-result marketplace CREATE without reconciliation
- bypass the compliance gate
- turn `UNKNOWN` or `REVIEW_REQUIRED` into PASS through a silent fallback
