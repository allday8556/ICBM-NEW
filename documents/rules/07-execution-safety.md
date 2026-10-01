<!-- Moved verbatim from CLAUDE.md §7 (Execution safety) by the repository restructure (Issue #151, ADR-0021 §3). The section number is kept, so `CLAUDE.md §7` references resolve here (PATH_MIGRATION_MAP). -->

## 7. Execution safety

This section is the single active owner of protected execution actions and approval boundaries for
the whole repository. Other rules, ADRs, agents, CI and manual procedures reference §7.2; they do
not create a broader parallel approval list.

### 7.1 Execution mode

Global external-write mode is:

```text
DRY_RUN | LIVE
```

Default during development is `DRY_RUN`.

Marketplace sandbox/test accounts, where available, are environment/account configuration — not a third global execution mode.

`LIVE` requires the intended verification scope to be explicit in GitHub and user approval when the action is protected/destructive.

A routine read-only provider call, read-back, health check or lookup inside an already-approved
integration is not a LIVE write and does not need separate user approval when it creates no remote
side effect, exports no non-public sensitive data and adds no material new cost. Reading with an
already configured credential is not a credential change.

### 7.2 Requires user approval before execution

- opening or widening a bounded LIVE mutation scope; work inside an already approved scope does not
  ask again per item
- any destructive marketplace action: delete/deactivate or equivalent
- any real supplier order (발주)
- bulk live writes or bulk destructive operations
- destructive migrations, table/row drops, irreversible rewrites
- compliance gate override
- credential changes that replace/delete active credentials
- accepting a material residual risk that cannot be removed inside the approved scope
- a new or material unbudgeted payment or cost
- transferring credentials, real customer/order data or other non-public sensitive data to an
  external service beyond an already approved integration scope
- deleting branches or force-pushing

Routine reversible local writes, ordinary reversible migrations and usage already inside an
approved scope/budget are not protected merely because they write data or incur normal service
usage. They use the applicable §14.2 validation tier.

For first-time real marketplace CREATE/UPDATE verification, use the explicit LIVE acceptance scope defined in GitHub and obtain user approval before the run.

Approval is per bounded scope, not necessarily per item. A scope may cover one canary, campaign or
batch when it fixes the account, action type, maximum quantity and safety preconditions in GitHub.
Work inside that approved boundary does not ask again for each item. A different account or action
type, a higher quantity ceiling, an expired scope or a changed safety precondition requires a new
approval; approval never generalizes to an unspecified future action.

### 7.3 Never do

- commit credentials, tokens, cookies, or session state
- commit real customer/order personal data in tests or fixtures
- spin repeated failed supplier logins
- retry an unknown-result marketplace CREATE without reconciliation
- bypass the compliance gate
- turn `UNKNOWN` or `REVIEW_REQUIRED` into PASS through a silent fallback
